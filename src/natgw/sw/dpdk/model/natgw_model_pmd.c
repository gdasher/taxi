/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * net_natgw_model: the natgw shim software model as a DPDK virtual device.
 * See rte_pmd_natgw_model.h.
 */

#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <linux/if_tun.h>

#include <bus_vdev_driver.h>
#include <eal_export.h>
#include <ethdev_driver.h>
#include <rte_alarm.h>
#include <rte_cycles.h>
#include <rte_kvargs.h>
#include <rte_malloc.h>
#include <rte_ring.h>
#include <rte_spinlock.h>

#include "natgw.h"
#include "natgw_flow.h"
#include "natgw_model.h"
#include "rte_pmd_natgw_model.h"

RTE_LOG_REGISTER_DEFAULT(natgw_model_logtype, NOTICE);
#define LOG(level, fmt, ...) \
	rte_log(RTE_LOG_##level, natgw_model_logtype, "natgw_model: " fmt "\n", ##__VA_ARGS__)

#define MAX_FRAME   9600
#define RING_SIZE   4096
#define POLL_US     100000
#define TICK_HZ     1000

enum wire_mode { WIRE_QUEUE, WIRE_TAP };

struct wire_frame {
	uint16_t len;
	uint8_t  fwd;
	uint8_t  data[];
};

struct model_dev;

struct lane {
	struct model_dev *md;
	unsigned idx;
	uint16_t port_id;
	struct rte_mempool *mp;      /* rx queue mempool (punts) */
	struct rte_ring *wire_in;    /* wire=queue: frames arriving from the wire */
	struct rte_ring *wire_out;   /* wire=queue: frames leaving on the wire */
	struct rte_ring *punt;       /* mbufs for this lane's rx queue */
	int tap_fd;
	uint64_t rx_pkts, rx_bytes, tx_pkts, tx_bytes, rx_nombuf, wire_drops;
	bool started;
};

struct model_dev {
	char name[RTE_DEV_NAME_MAX_LEN];
	struct natgw_model *model;
	rte_spinlock_t mlock;
	struct natgw_flow_ctx *flow;
	enum wire_mode wire;
	bool manual_clock;
	bool punt_hdr;
	unsigned lanes;
	struct lane lane[NATGW_LANES];
	uint64_t last_cycles;
	uint64_t cycles_per_tick;
	unsigned started;
	uint8_t scratch[MAX_FRAME + NATGW_PUNT_HDR_LEN];
};

struct port_priv {
	struct lane *lane;
};

static const struct rte_eth_link link_up = {
	.link_speed = RTE_ETH_SPEED_NUM_25G,
	.link_duplex = RTE_ETH_LINK_FULL_DUPLEX,
	.link_status = RTE_ETH_LINK_UP,
	.link_autoneg = RTE_ETH_LINK_FIXED,
};

/* ------------------------------------------------------------------ */
/* register access through the model, serialised with the datapath */

static uint32_t io_rd(void *ctx, uint32_t off)
{
	struct model_dev *md = ctx;
	uint32_t v;

	rte_spinlock_lock(&md->mlock);
	v = natgw_model_rd(md->model, off);
	rte_spinlock_unlock(&md->mlock);
	return v;
}

static void io_wr(void *ctx, uint32_t off, uint32_t val)
{
	struct model_dev *md = ctx;

	rte_spinlock_lock(&md->mlock);
	natgw_model_wr(md->model, off, val);
	rte_spinlock_unlock(&md->mlock);
}

/* ------------------------------------------------------------------ */
/* wire */

static void wire_send(struct model_dev *md, unsigned lane, const uint8_t *data, size_t len, bool fwd)
{
	struct lane *l = &md->lane[lane];

	if (md->wire == WIRE_TAP) {
		if (l->tap_fd < 0 || write(l->tap_fd, data, len) != (ssize_t)len)
			l->wire_drops++;
		return;
	}
	struct wire_frame *f = malloc(sizeof(*f) + len);
	if (!f) {
		l->wire_drops++;
		return;
	}
	f->len = (uint16_t)len;
	f->fwd = fwd;
	memcpy(f->data, data, len);
	if (rte_ring_enqueue(l->wire_out, f) != 0) {
		free(f);
		l->wire_drops++;
	}
}

/* advance the tick counter from the wall clock */
static void clock_update(struct model_dev *md)
{
	uint64_t now, ticks;

	if (md->manual_clock)
		return;
	now = rte_get_timer_cycles();
	ticks = (now - md->last_cycles) / md->cycles_per_tick;
	if (ticks) {
		md->last_cycles += ticks * md->cycles_per_tick;
		natgw_model_advance(md->model, (uint32_t)RTE_MIN(ticks, (uint64_t)UINT32_MAX));
	}
}

/* run one frame from the wire of <lane> through the model; md->mlock held */
static void process_frame(struct model_dev *md, unsigned lane, const uint8_t *data, size_t len)
{
	struct lane *l = &md->lane[lane];
	struct natgw_model_out out = { .data = md->scratch };
	struct rte_mbuf *m;

	if (len > MAX_FRAME || natgw_model_rx(md->model, lane, data, len, &out) != 0) {
		l->wire_drops++;
		return;
	}
	if (out.kind == NATGW_OUT_FWD) {
		wire_send(md, out.lane, out.data, out.len, true);
		return;
	}
	/* punt: to the ingress lane's rx queue */
	if (!l->started || !l->mp) {
		l->wire_drops++;
		return;
	}
	m = rte_pktmbuf_alloc(l->mp);
	if (!m) {
		l->rx_nombuf++;
		return;
	}
	if (rte_pktmbuf_append(m, (uint16_t)out.len) == NULL) {
		rte_pktmbuf_free(m);
		l->wire_drops++;
		return;
	}
	memcpy(rte_pktmbuf_mtod(m, void *), out.data, out.len);
	m->port = l->port_id;
	if (md->punt_hdr && out.reason != NATGW_RSN_BYPASS && natgw_punt_strip(m) != 0) {
		rte_pktmbuf_free(m);
		l->wire_drops++;
		return;
	}
	if (rte_ring_enqueue(l->punt, m) != 0) {
		rte_pktmbuf_free(m);
		l->wire_drops++;
	}
}

/* service wire input of one lane; returns frames processed */
static unsigned service_lane(struct model_dev *md, unsigned lane, unsigned budget)
{
	struct lane *l = &md->lane[lane];
	unsigned n = 0;

	rte_spinlock_lock(&md->mlock);
	clock_update(md);
	while (n < budget) {
		if (md->wire == WIRE_TAP) {
			ssize_t r;
			if (l->tap_fd < 0)
				break;
			r = read(l->tap_fd, md->scratch, MAX_FRAME);
			if (r <= 0)
				break;
			/* reuse a separate buffer: the model output also uses scratch */
			uint8_t frame[MAX_FRAME];
			memcpy(frame, md->scratch, (size_t)r);
			process_frame(md, lane, frame, (size_t)r);
		} else {
			struct wire_frame *f;
			if (rte_ring_dequeue(l->wire_in, (void **)&f) != 0)
				break;
			process_frame(md, lane, f->data, f->len);
			free(f);
		}
		n++;
	}
	rte_spinlock_unlock(&md->mlock);
	return n;
}

/* ------------------------------------------------------------------ */
/* datapath */

static uint16_t model_rx_burst(void *q, struct rte_mbuf **pkts, uint16_t nb)
{
	struct lane *l = q;
	unsigned n;

	service_lane(l->md, l->idx, 64);
	n = rte_ring_dequeue_burst(l->punt, (void **)pkts, nb, NULL);
	for (unsigned i = 0; i < n; i++) {
		l->rx_pkts++;
		l->rx_bytes += rte_pktmbuf_pkt_len(pkts[i]);
	}
	return (uint16_t)n;
}

static uint16_t model_tx_burst(void *q, struct rte_mbuf **pkts, uint16_t nb)
{
	struct lane *l = q;
	uint8_t buf[MAX_FRAME];

	for (uint16_t i = 0; i < nb; i++) {
		struct rte_mbuf *m = pkts[i];
		uint32_t len = rte_pktmbuf_pkt_len(m);
		if (len <= MAX_FRAME) {
			const void *p = rte_pktmbuf_read(m, 0, len, buf);
			/* host transmit bypasses the shim and goes straight to the MAC */
			wire_send(l->md, l->idx, p, len, false);
			l->tx_pkts++;
			l->tx_bytes += len;
		}
		rte_pktmbuf_free(m);
	}
	return nb;
}

/* ------------------------------------------------------------------ */
/* ethdev ops */

static struct lane *dev_lane(struct rte_eth_dev *dev)
{
	return ((struct port_priv *)dev->data->dev_private)->lane;
}

static int model_configure(struct rte_eth_dev *dev __rte_unused)
{
	return 0;
}

static void poll_alarm(void *arg)
{
	struct model_dev *md = arg;

	if (!md->started)
		return;
	if (!md->manual_clock) {
		rte_spinlock_lock(&md->mlock);
		clock_update(md);
		rte_spinlock_unlock(&md->mlock);
	}
	natgw_flow_poll(md->flow);
	rte_eal_alarm_set(POLL_US, poll_alarm, md);
}

static int model_start(struct rte_eth_dev *dev)
{
	struct lane *l = dev_lane(dev);
	struct model_dev *md = l->md;

	l->started = true;
	dev->data->dev_link = link_up;
	dev->data->rx_queue_state[0] = RTE_ETH_QUEUE_STATE_STARTED;
	dev->data->tx_queue_state[0] = RTE_ETH_QUEUE_STATE_STARTED;
	if (md->started++ == 0) {
		natgw_flow_enable(md->flow, true);
		rte_eal_alarm_set(POLL_US, poll_alarm, md);
	}
	return 0;
}

static int model_stop(struct rte_eth_dev *dev)
{
	struct lane *l = dev_lane(dev);
	struct model_dev *md = l->md;
	struct rte_mbuf *m;

	if (!l->started)
		return 0;
	l->started = false;
	dev->data->dev_link.link_status = RTE_ETH_LINK_DOWN;
	dev->data->rx_queue_state[0] = RTE_ETH_QUEUE_STATE_STOPPED;
	dev->data->tx_queue_state[0] = RTE_ETH_QUEUE_STATE_STOPPED;
	while (rte_ring_dequeue(l->punt, (void **)&m) == 0)
		rte_pktmbuf_free(m);
	if (--md->started == 0) {
		rte_eal_alarm_cancel(poll_alarm, md);
		natgw_flow_enable(md->flow, false);
	}
	return 0;
}

static int model_infos_get(struct rte_eth_dev *dev __rte_unused, struct rte_eth_dev_info *info)
{
	info->max_mac_addrs = 1;
	info->max_rx_pktlen = MAX_FRAME;
	info->min_mtu = RTE_ETHER_MIN_MTU;
	info->max_mtu = MAX_FRAME - RTE_ETHER_HDR_LEN;
	info->max_rx_queues = 1;
	info->max_tx_queues = 1;
	info->rx_offload_capa = 0;
	info->tx_offload_capa = RTE_ETH_TX_OFFLOAD_MULTI_SEGS;
	info->speed_capa = RTE_ETH_LINK_SPEED_25G;
	return 0;
}

static int model_rxq_setup(struct rte_eth_dev *dev, uint16_t qid, uint16_t nb __rte_unused,
			   unsigned int socket __rte_unused, const struct rte_eth_rxconf *conf __rte_unused,
			   struct rte_mempool *mp)
{
	struct lane *l = dev_lane(dev);

	if (qid != 0)
		return -EINVAL;
	l->mp = mp;
	dev->data->rx_queues[0] = l;
	return 0;
}

static int model_txq_setup(struct rte_eth_dev *dev, uint16_t qid, uint16_t nb __rte_unused,
			   unsigned int socket __rte_unused, const struct rte_eth_txconf *conf __rte_unused)
{
	if (qid != 0)
		return -EINVAL;
	dev->data->tx_queues[0] = dev_lane(dev);
	return 0;
}

static void model_queue_release(struct rte_eth_dev *dev __rte_unused, uint16_t qid __rte_unused)
{
}

static int model_link_update(struct rte_eth_dev *dev, int wait __rte_unused)
{
	struct lane *l = dev_lane(dev);
	struct rte_eth_link link = link_up;

	link.link_status = l->started ? RTE_ETH_LINK_UP : RTE_ETH_LINK_DOWN;
	return rte_eth_linkstatus_set(dev, &link);
}

static int model_stats_get(struct rte_eth_dev *dev, struct rte_eth_stats *st,
			   struct eth_queue_stats *qstats __rte_unused)
{
	struct lane *l = dev_lane(dev);

	st->ipackets = l->rx_pkts;
	st->ibytes = l->rx_bytes;
	st->opackets = l->tx_pkts;
	st->obytes = l->tx_bytes;
	st->rx_nombuf = l->rx_nombuf;
	st->imissed = l->wire_drops;
	return 0;
}

static int model_stats_reset(struct rte_eth_dev *dev)
{
	struct lane *l = dev_lane(dev);

	l->rx_pkts = l->rx_bytes = l->tx_pkts = l->tx_bytes = l->rx_nombuf = l->wire_drops = 0;
	return 0;
}

static int model_xstats_get_names(struct rte_eth_dev *dev, struct rte_eth_xstat_name *names, unsigned int size)
{
	return natgw_flow_xstats_get_names(dev_lane(dev)->md->flow, names, size);
}

static int model_xstats_get(struct rte_eth_dev *dev, struct rte_eth_xstat *xstats, unsigned int n)
{
	struct lane *l = dev_lane(dev);

	return natgw_flow_xstats_get(l->md->flow, l->idx, xstats, n);
}

static int model_flow_ops_get(struct rte_eth_dev *dev __rte_unused, const struct rte_flow_ops **ops)
{
	*ops = natgw_flow_ops();
	return 0;
}

static int model_promisc(struct rte_eth_dev *dev __rte_unused)
{
	return 0;
}

static int model_mac_set(struct rte_eth_dev *dev __rte_unused, struct rte_ether_addr *addr __rte_unused)
{
	return 0;
}

static int model_mtu_set(struct rte_eth_dev *dev __rte_unused, uint16_t mtu)
{
	return mtu + RTE_ETHER_HDR_LEN <= MAX_FRAME ? 0 : -EINVAL;
}

static int model_close(struct rte_eth_dev *dev);

static const struct eth_dev_ops model_ops = {
	.dev_configure = model_configure,
	.dev_start = model_start,
	.dev_stop = model_stop,
	.dev_close = model_close,
	.dev_infos_get = model_infos_get,
	.rx_queue_setup = model_rxq_setup,
	.tx_queue_setup = model_txq_setup,
	.rx_queue_release = model_queue_release,
	.tx_queue_release = model_queue_release,
	.link_update = model_link_update,
	.stats_get = model_stats_get,
	.stats_reset = model_stats_reset,
	.xstats_get = model_xstats_get,
	.xstats_get_names = model_xstats_get_names,
	.flow_ops_get = model_flow_ops_get,
	.promiscuous_enable = model_promisc,
	.promiscuous_disable = model_promisc,
	.allmulticast_enable = model_promisc,
	.allmulticast_disable = model_promisc,
	.mac_addr_set = model_mac_set,
	.mtu_set = model_mtu_set,
};

/* ------------------------------------------------------------------ */
/* device create and destroy */

static void model_dev_free(struct model_dev *md)
{
	if (!md)
		return;
	rte_eal_alarm_cancel(poll_alarm, md);
	for (unsigned i = 0; i < md->lanes; i++) {
		struct lane *l = &md->lane[i];
		struct wire_frame *f;
		struct rte_mbuf *m;
		if (l->wire_in) {
			while (rte_ring_dequeue(l->wire_in, (void **)&f) == 0)
				free(f);
			rte_ring_free(l->wire_in);
		}
		if (l->wire_out) {
			while (rte_ring_dequeue(l->wire_out, (void **)&f) == 0)
				free(f);
			rte_ring_free(l->wire_out);
		}
		if (l->punt) {
			while (rte_ring_dequeue(l->punt, (void **)&m) == 0)
				rte_pktmbuf_free(m);
			rte_ring_free(l->punt);
		}
		if (l->tap_fd >= 0)
			close(l->tap_fd);
	}
	natgw_flow_ctx_destroy(md->flow);
	natgw_model_destroy(md->model);
	rte_free(md);
}

static int model_close(struct rte_eth_dev *dev)
{
	struct lane *l = dev_lane(dev);
	struct model_dev *md = l->md;
	bool last = true;

	if (rte_eal_process_type() != RTE_PROC_PRIMARY)
		return 0;
	model_stop(dev);
	natgw_flow_unbind_port(dev->data->port_id);
	l->port_id = RTE_MAX_ETHPORTS;
	for (unsigned i = 0; i < md->lanes; i++)
		if (md->lane[i].port_id != RTE_MAX_ETHPORTS)
			last = false;
	dev->data->mac_addrs = NULL;    /* static storage below */
	if (last)
		model_dev_free(md);
	return 0;
}

static int open_tap(const char *name)
{
	struct ifreq ifr;
	int fd, s;

	fd = open("/dev/net/tun", O_RDWR | O_NONBLOCK);
	if (fd < 0)
		return -errno;
	memset(&ifr, 0, sizeof(ifr));
	ifr.ifr_flags = IFF_TAP | IFF_NO_PI;
	strlcpy(ifr.ifr_name, name, IFNAMSIZ);
	if (ioctl(fd, TUNSETIFF, &ifr) < 0) {
		int e = errno;
		close(fd);
		return -e;
	}
	s = socket(AF_INET, SOCK_DGRAM, 0);
	if (s >= 0) {
		if (ioctl(s, SIOCGIFFLAGS, &ifr) == 0) {
			ifr.ifr_flags |= IFF_UP;
			ioctl(s, SIOCSIFFLAGS, &ifr);
		}
		close(s);
	}
	return fd;
}

struct model_args {
	unsigned lanes;
	unsigned bucket_w;
	enum wire_mode wire;
	char tap_prefix[IFNAMSIZ];
	bool punt_hdr;
	bool manual_clock;
	unsigned ddr_bucket_w;     /* 0: no DDR tier in the "bitstream" */
	bool ddr_calib;            /* false: tier present, no working DIMM */
	bool no_ddr;               /* flow layer told not to use the tier */
};

static int arg_uint(const char *key __rte_unused, const char *val, void *out)
{
	char *end;
	unsigned long v = strtoul(val, &end, 0);

	if (*end || v > 64)
		return -EINVAL;
	*(unsigned *)out = (unsigned)v;
	return 0;
}

static int arg_wire(const char *key __rte_unused, const char *val, void *out)
{
	if (!strcmp(val, "queue"))
		*(enum wire_mode *)out = WIRE_QUEUE;
	else if (!strcmp(val, "tap"))
		*(enum wire_mode *)out = WIRE_TAP;
	else
		return -EINVAL;
	return 0;
}

static int arg_str(const char *key __rte_unused, const char *val, void *out)
{
	if (strlen(val) >= IFNAMSIZ - 2)
		return -EINVAL;
	strlcpy(out, val, IFNAMSIZ);
	return 0;
}

static int arg_bool(const char *key __rte_unused, const char *val, void *out)
{
	*(bool *)out = !strcmp(val, "1") || !strcmp(val, "true");
	return 0;
}

static int arg_clock(const char *key __rte_unused, const char *val, void *out)
{
	if (!strcmp(val, "manual"))
		*(bool *)out = true;
	else if (!strcmp(val, "wall"))
		*(bool *)out = false;
	else
		return -EINVAL;
	return 0;
}

static const char *const valid_args[] = {"lanes", "bucket_w", "wire", "tap_prefix", "punt_hdr", "clock",
					 "ddr_bucket_w", "ddr_calib", "no_ddr", NULL};

static int parse_args(const char *params, struct model_args *a)
{
	struct rte_kvargs *kv;
	int ret = 0;

	a->lanes = NATGW_LANES;
	a->bucket_w = 10;
	a->wire = WIRE_QUEUE;
	strlcpy(a->tap_prefix, "ngw", sizeof(a->tap_prefix));
	a->punt_hdr = true;
	a->manual_clock = false;
	a->ddr_bucket_w = 0;
	a->ddr_calib = true;
	a->no_ddr = false;
	if (!params || !*params)
		return 0;
	kv = rte_kvargs_parse(params, valid_args);
	if (!kv)
		return -EINVAL;
	if (rte_kvargs_process(kv, "lanes", arg_uint, &a->lanes) < 0 ||
	    rte_kvargs_process(kv, "bucket_w", arg_uint, &a->bucket_w) < 0 ||
	    rte_kvargs_process(kv, "wire", arg_wire, &a->wire) < 0 ||
	    rte_kvargs_process(kv, "tap_prefix", arg_str, a->tap_prefix) < 0 ||
	    rte_kvargs_process(kv, "punt_hdr", arg_bool, &a->punt_hdr) < 0 ||
	    rte_kvargs_process(kv, "clock", arg_clock, &a->manual_clock) < 0 ||
	    rte_kvargs_process(kv, "ddr_bucket_w", arg_uint, &a->ddr_bucket_w) < 0 ||
	    rte_kvargs_process(kv, "ddr_calib", arg_bool, &a->ddr_calib) < 0 ||
	    rte_kvargs_process(kv, "no_ddr", arg_bool, &a->no_ddr) < 0)
		ret = -EINVAL;
	rte_kvargs_free(kv);
	if (!ret && (a->lanes < 1 || a->lanes > NATGW_LANES || a->bucket_w < 2 || a->bucket_w > 18 ||
		     (a->ddr_bucket_w && (a->ddr_bucket_w < 5 || a->ddr_bucket_w > 20))))
		ret = -EINVAL;
	return ret;
}

static struct rte_ether_addr lane_macs[RTE_MAX_ETHPORTS];

static int model_probe(struct rte_vdev_device *vdev)
{
	const char *name = rte_vdev_device_name(vdev);
	struct model_args a;
	struct model_dev *md;
	struct natgw_io io;
	struct natgw_flow_cfg fc;
	char rname[RTE_RING_NAMESIZE];
	int ret;

	if (rte_eal_process_type() != RTE_PROC_PRIMARY)
		return -ENOTSUP;
	ret = parse_args(rte_vdev_device_args(vdev), &a);
	if (ret) {
		LOG(ERR, "%s: invalid arguments", name);
		return ret;
	}
	ret = natgw_punt_meta_register();
	if (ret)
		return ret;

	md = rte_zmalloc_socket(name, sizeof(*md), 0, rte_socket_id());
	if (!md)
		return -ENOMEM;
	strlcpy(md->name, name, sizeof(md->name));
	rte_spinlock_init(&md->mlock);
	md->lanes = a.lanes;
	md->wire = a.wire;
	md->punt_hdr = a.punt_hdr;
	md->manual_clock = a.manual_clock;
	md->cycles_per_tick = rte_get_timer_hz() / TICK_HZ;
	md->last_cycles = rte_get_timer_cycles();
	for (unsigned i = 0; i < NATGW_LANES; i++) {
		md->lane[i].tap_fd = -1;
		md->lane[i].port_id = RTE_MAX_ETHPORTS;
	}

	md->model = natgw_model_create(a.bucket_w);
	if (!md->model || (a.ddr_bucket_w && natgw_model_set_ddr(md->model, a.ddr_bucket_w, a.ddr_calib) != 0)) {
		ret = -ENOMEM;
		goto fail;
	}
	io.rd = io_rd;
	io.wr = io_wr;
	io.ctx = md;
	memset(&fc, 0, sizeof(fc));
	fc.lanes = a.lanes;
	fc.ticks_per_sec = TICK_HZ;
	fc.seed0 = 0xffffffff;
	fc.seed1 = 0xffffffff;
	fc.max_depth = 6;
	fc.punt_hdr = a.punt_hdr;
	fc.no_ddr = a.no_ddr;
	md->flow = natgw_flow_ctx_create(&io, &fc, rte_socket_id());
	if (!md->flow) {
		ret = -EIO;
		goto fail;
	}

	for (unsigned i = 0; i < a.lanes; i++) {
		struct lane *l = &md->lane[i];
		char pname[RTE_ETH_NAME_MAX_LEN];
		struct rte_eth_dev *dev;
		struct port_priv *pp;

		l->md = md;
		l->idx = i;
		snprintf(rname, sizeof(rname), "%s_p%u", name, i);
		l->punt = rte_ring_create(rname, RING_SIZE, rte_socket_id(), RING_F_SP_ENQ | RING_F_SC_DEQ);
		if (a.wire == WIRE_QUEUE) {
			snprintf(rname, sizeof(rname), "%s_i%u", name, i);
			l->wire_in = rte_ring_create(rname, RING_SIZE, rte_socket_id(), 0);
			snprintf(rname, sizeof(rname), "%s_o%u", name, i);
			l->wire_out = rte_ring_create(rname, RING_SIZE, rte_socket_id(), 0);
			if (!l->wire_in || !l->wire_out) {
				ret = -ENOMEM;
				goto fail;
			}
		} else {
			char tname[IFNAMSIZ];
			snprintf(tname, sizeof(tname), "%s%u", a.tap_prefix, i);
			l->tap_fd = open_tap(tname);
			if (l->tap_fd < 0) {
				LOG(ERR, "%s: cannot open TAP %s: %s", name, tname, strerror(-l->tap_fd));
				ret = l->tap_fd;
				goto fail;
			}
		}
		if (!l->punt) {
			ret = -ENOMEM;
			goto fail;
		}

		snprintf(pname, sizeof(pname), "%s_l%u", name, i);
		dev = rte_eth_dev_allocate(pname);
		if (!dev) {
			ret = -ENOSPC;
			goto fail;
		}
		pp = rte_zmalloc_socket(pname, sizeof(*pp), 0, rte_socket_id());
		if (!pp) {
			rte_eth_dev_release_port(dev);
			ret = -ENOMEM;
			goto fail;
		}
		pp->lane = l;
		dev->data->dev_private = pp;
		dev->device = &vdev->device;
		dev->dev_ops = &model_ops;
		dev->rx_pkt_burst = model_rx_burst;
		dev->tx_pkt_burst = model_tx_burst;
		dev->data->nb_rx_queues = 1;
		dev->data->nb_tx_queues = 1;
		dev->data->dev_link = link_up;
		dev->data->dev_link.link_status = RTE_ETH_LINK_DOWN;
		dev->data->dev_flags |= RTE_ETH_DEV_AUTOFILL_QUEUE_XSTATS;
		l->port_id = dev->data->port_id;
		lane_macs[l->port_id].addr_bytes[0] = 0x02;
		lane_macs[l->port_id].addr_bytes[1] = 0x4e;
		lane_macs[l->port_id].addr_bytes[2] = 0x47;
		lane_macs[l->port_id].addr_bytes[5] = (uint8_t)l->port_id;
		dev->data->mac_addrs = &lane_macs[l->port_id];
		natgw_flow_bind_port(md->flow, i, l->port_id);
		rte_eth_dev_probing_finish(dev);
	}
	LOG(INFO, "%s: %u lanes, %u entries, wire=%s", name, a.lanes, natgw_flow_capacity(md->flow),
	    a.wire == WIRE_TAP ? "tap" : "queue");
	return 0;
fail:
	for (unsigned i = 0; i < md->lanes; i++) {
		if (md->lane[i].port_id != RTE_MAX_ETHPORTS) {
			struct rte_eth_dev *dev = &rte_eth_devices[md->lane[i].port_id];
			natgw_flow_unbind_port(md->lane[i].port_id);
			dev->data->mac_addrs = NULL;
			rte_eth_dev_release_port(dev);
		}
	}
	model_dev_free(md);
	return ret;
}

static int model_remove(struct rte_vdev_device *vdev)
{
	const char *name = rte_vdev_device_name(vdev);
	uint16_t pid;

	RTE_ETH_FOREACH_DEV(pid) {
		struct rte_eth_dev *dev = &rte_eth_devices[pid];
		if (dev->device == &vdev->device) {
			/* close releases the port (and frees the device with its last lane) */
			rte_eth_dev_stop(pid);
			rte_eth_dev_close(pid);
		}
	}
	LOG(INFO, "%s removed", name);
	return 0;
}

static struct rte_vdev_driver natgw_model_drv = {
	.probe = model_probe,
	.remove = model_remove,
};

RTE_PMD_REGISTER_VDEV(net_natgw_model, natgw_model_drv);
RTE_PMD_REGISTER_PARAM_STRING(net_natgw_model,
	"lanes=<1-8> bucket_w=<2-18> wire=queue|tap tap_prefix=<name> punt_hdr=0|1 clock=wall|manual "
	"ddr_bucket_w=<5-20> ddr_calib=0|1 no_ddr=0|1");

/* ------------------------------------------------------------------ */
/* test API */

static struct lane *port_lane(uint16_t port_id)
{
	struct rte_eth_dev *dev;

	if (!rte_eth_dev_is_valid_port(port_id))
		return NULL;
	dev = &rte_eth_devices[port_id];
	if (dev->dev_ops != &model_ops)
		return NULL;
	return dev_lane(dev);
}

RTE_EXPORT_SYMBOL(rte_pmd_natgw_model_wire_inject)
int rte_pmd_natgw_model_wire_inject(uint16_t port_id, const void *frame, uint16_t len)
{
	struct lane *l = port_lane(port_id);
	struct wire_frame *f;

	if (!l || l->md->wire != WIRE_QUEUE || len > MAX_FRAME)
		return -EINVAL;
	f = malloc(sizeof(*f) + len);
	if (!f)
		return -ENOMEM;
	f->len = len;
	f->fwd = 0;
	memcpy(f->data, frame, len);
	if (rte_ring_enqueue(l->wire_in, f) != 0) {
		free(f);
		return -ENOBUFS;
	}
	return 0;
}

RTE_EXPORT_SYMBOL(rte_pmd_natgw_model_wire_recv)
int rte_pmd_natgw_model_wire_recv(uint16_t port_id, void *buf, uint16_t cap, int *forwarded)
{
	struct lane *l = port_lane(port_id);
	struct wire_frame *f;
	int len;

	if (!l || l->md->wire != WIRE_QUEUE)
		return -EINVAL;
	if (rte_ring_dequeue(l->wire_out, (void **)&f) != 0)
		return 0;
	if (f->len > cap) {
		free(f);
		return -EMSGSIZE;
	}
	memcpy(buf, f->data, f->len);
	if (forwarded)
		*forwarded = f->fwd;
	len = f->len;
	free(f);
	return len;
}

RTE_EXPORT_SYMBOL(rte_pmd_natgw_model_advance)
int rte_pmd_natgw_model_advance(uint16_t port_id, uint32_t ticks)
{
	struct lane *l = port_lane(port_id);

	if (!l)
		return -EINVAL;
	rte_spinlock_lock(&l->md->mlock);
	natgw_model_advance(l->md->model, ticks);
	rte_spinlock_unlock(&l->md->mlock);
	return 0;
}

RTE_EXPORT_SYMBOL(rte_pmd_natgw_model_service)
int rte_pmd_natgw_model_service(uint16_t port_id)
{
	struct lane *l = port_lane(port_id);
	unsigned n = 0;

	if (!l)
		return -EINVAL;
	for (unsigned i = 0; i < l->md->lanes; i++)
		n += service_lane(l->md, i, RING_SIZE);
	return (int)n;
}

RTE_EXPORT_SYMBOL(rte_pmd_natgw_model_poll)
int rte_pmd_natgw_model_poll(uint16_t port_id)
{
	struct lane *l = port_lane(port_id);

	if (!l)
		return -EINVAL;
	natgw_flow_poll(l->md->flow);
	return 0;
}
