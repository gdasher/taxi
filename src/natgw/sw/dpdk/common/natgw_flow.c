/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * rte_flow backend for the natgw NAT shim. See natgw_flow.h.
 */

#include <errno.h>
#include <string.h>
#include <sys/queue.h>

#include <ethdev_driver.h>
#include <rte_byteorder.h>
#include <rte_cycles.h>
#include <rte_flow_driver.h>
#include <rte_hash.h>
#include <rte_jhash.h>
#include <rte_malloc.h>
#include <rte_mbuf_dyn.h>
#include <rte_spinlock.h>

#include "natgw_flow.h"

#define NO_THRESH 0xffffffffu

struct hkey {
	uint32_t sip, dip;
	uint16_t sport, dport, vid;
	uint8_t  lane, tcp;
};

struct rte_flow {
	TAILQ_ENTRY(rte_flow) next;
	struct natgw_flow_ctx *ctx;
	struct natgw_entry e;
	uint16_t port_id;          /* ingress port */
	uint16_t nh_idx;
	bool     has_age;
	bool     aged;
	bool     idle_pending;     /* idle event seen, flow timeout not yet reached */
	uint32_t age_timeout;      /* seconds */
	void    *age_ctx;
	uint32_t pending_ts;
	uint64_t cnt_base_pkts, cnt_base_bytes;
};

TAILQ_HEAD(natgw_flow_list, rte_flow);

struct natgw_flow_ctx {
	rte_spinlock_t lock;
	struct natgw_dev dev;
	struct natgw_flow_cfg cfg;
	struct natgw_table *t;
	struct natgw_nh_table *nht;
	struct natgw_flow_list flows;
	unsigned nflows;
	struct rte_flow **by_idx;
	struct rte_hash *by_key;
	struct natgw_write *ops;
	unsigned max_ops;
	uint16_t lane_port[NATGW_LANES];
	bool lane_bound[NATGW_LANES];
	uint32_t thresh[2];        /* hardware idle thresholds (ticks): [udp, tcp] */
	bool resync;
	uint64_t ev_fin, ev_rst, ev_ovf;
};

static struct {
	struct natgw_flow_ctx *ctx;
	unsigned lane;
} port_map[RTE_MAX_ETHPORTS];

static int ctx_seq;

/* ------------------------------------------------------------------ */
/* context */

static struct hkey mk_hkey(const struct natgw_key *k)
{
	struct hkey h;

	memset(&h, 0, sizeof(h));
	h.sip = k->sip;
	h.dip = k->dip;
	h.sport = k->sport;
	h.dport = k->dport;
	h.vid = k->vid;
	h.lane = k->lane;
	h.tcp = k->tcp;
	return h;
}

static void write_thresholds(struct natgw_flow_ctx *ctx)
{
	natgw_dev_set_thresholds(&ctx->dev, ctx->thresh[1], ctx->thresh[0]);
}

struct natgw_flow_ctx *natgw_flow_ctx_create(const struct natgw_io *io, const struct natgw_flow_cfg *cfg,
					     int socket_id)
{
	struct natgw_flow_ctx *ctx;
	char name[RTE_HASH_NAMESIZE];
	struct rte_hash_parameters hp = {
		.key_len = sizeof(struct hkey),
		.hash_func = rte_jhash,
		.socket_id = socket_id,
	};

	if (cfg->lanes == 0 || cfg->lanes > NATGW_LANES || cfg->ticks_per_sec == 0)
		return NULL;
	ctx = rte_zmalloc_socket("natgw_flow_ctx", sizeof(*ctx), 0, socket_id);
	if (!ctx)
		return NULL;
	rte_spinlock_init(&ctx->lock);
	TAILQ_INIT(&ctx->flows);
	ctx->cfg = *cfg;
	if (!ctx->cfg.max_depth)
		ctx->cfg.max_depth = 6;

	if (natgw_dev_init(&ctx->dev, io) != 0)
		goto fail;
	if (natgw_dev_clear(&ctx->dev, 1000000) != 0)
		goto fail;
	natgw_dev_set_ctrl(&ctx->dev, false, cfg->punt_hdr, 0xff, 0xff);
	natgw_dev_set_seeds(&ctx->dev, cfg->seed0, cfg->seed1);
	if (cfg->tick_div)
		natgw_dev_set_tick_div(&ctx->dev, cfg->tick_div);
	ctx->thresh[0] = ctx->thresh[1] = NO_THRESH;
	write_thresholds(ctx);

	ctx->t = natgw_table_create(ctx->dev.bucket_w, cfg->seed0, cfg->seed1, ctx->cfg.max_depth);
	ctx->nht = rte_zmalloc_socket("natgw_nh", sizeof(*ctx->nht), 0, socket_id);
	ctx->max_ops = NATGW_MAX_INSERT_OPS(ctx->cfg.max_depth);
	ctx->ops = rte_zmalloc_socket("natgw_ops", ctx->max_ops * sizeof(*ctx->ops), 0, socket_id);
	if (!ctx->t || !ctx->nht || !ctx->ops)
		goto fail;
	natgw_nh_table_init(ctx->nht);
	ctx->by_idx = rte_zmalloc_socket("natgw_by_idx", natgw_table_size(ctx->t) * sizeof(*ctx->by_idx), 0,
					 socket_id);
	snprintf(name, sizeof(name), "natgw_flows_%d", __atomic_fetch_add(&ctx_seq, 1, __ATOMIC_RELAXED));
	hp.name = name;
	/* headroom: a nearly full rte_hash relocates keys (a deep, stack-hungry
	 * search) and can refuse keys the hardware table still has room for */
	hp.entries = 2 * natgw_table_size(ctx->t) + 64;
	ctx->by_key = rte_hash_create(&hp);
	if (!ctx->by_idx || !ctx->by_key)
		goto fail;
	return ctx;
fail:
	natgw_flow_ctx_destroy(ctx);
	return NULL;
}

void natgw_flow_ctx_destroy(struct natgw_flow_ctx *ctx)
{
	struct rte_flow *f;

	if (!ctx)
		return;
	while ((f = TAILQ_FIRST(&ctx->flows)) != NULL) {
		TAILQ_REMOVE(&ctx->flows, f, next);
		rte_free(f);
	}
	if (ctx->dev.io.rd)
		natgw_dev_set_ctrl(&ctx->dev, false, false, 0xff, 0xff);
	for (unsigned p = 0; p < RTE_MAX_ETHPORTS; p++)
		if (port_map[p].ctx == ctx)
			port_map[p].ctx = NULL;
	if (ctx->by_key)
		rte_hash_free(ctx->by_key);
	rte_free(ctx->by_idx);
	rte_free(ctx->ops);
	rte_free(ctx->nht);
	natgw_table_destroy(ctx->t);
	rte_free(ctx);
}

int natgw_flow_bind_port(struct natgw_flow_ctx *ctx, unsigned lane, uint16_t port_id)
{
	if (lane >= ctx->cfg.lanes || port_id >= RTE_MAX_ETHPORTS)
		return -EINVAL;
	port_map[port_id].ctx = ctx;
	port_map[port_id].lane = lane;
	ctx->lane_port[lane] = port_id;
	ctx->lane_bound[lane] = true;
	return 0;
}

void natgw_flow_unbind_port(uint16_t port_id)
{
	if (port_id < RTE_MAX_ETHPORTS)
		port_map[port_id].ctx = NULL;
}

void natgw_flow_enable(struct natgw_flow_ctx *ctx, bool enable)
{
	rte_spinlock_lock(&ctx->lock);
	natgw_dev_set_ctrl(&ctx->dev, enable, ctx->cfg.punt_hdr, enable ? 0x00 : 0xff, 0xff);
	rte_spinlock_unlock(&ctx->lock);
}

unsigned natgw_flow_count(struct natgw_flow_ctx *ctx)
{
	return ctx->nflows;
}

unsigned natgw_flow_capacity(struct natgw_flow_ctx *ctx)
{
	return natgw_table_size(ctx->t);
}

/* ------------------------------------------------------------------ */
/* flow parsing */

struct parsed_flow {
	struct natgw_entry e;
	struct natgw_nh nh;
	bool has_age;
	uint32_t age_timeout;
	void *age_ctx;
};

#define FAIL(err, code, type, obj, msg) \
	rte_flow_error_set(err, code, RTE_FLOW_ERROR_TYPE_##type, obj, msg)

static bool all_zero(const void *p, size_t n)
{
	const uint8_t *b = p;
	if (!p)
		return true;
	for (size_t i = 0; i < n; i++)
		if (b[i])
			return false;
	return true;
}

static int parse_pattern(const struct rte_flow_item *item, struct parsed_flow *pf, bool *vlan, struct rte_flow_error *err)
{
	bool have_ip = false, have_l4 = false;
	int proto = -1;

	for (; item->type != RTE_FLOW_ITEM_TYPE_END; item++) {
		if (item->last)
			return FAIL(err, ENOTSUP, ITEM_LAST, item, "ranges are not supported");
		switch (item->type) {
		case RTE_FLOW_ITEM_TYPE_VOID:
			break;
		case RTE_FLOW_ITEM_TYPE_ETH: {
			const struct rte_flow_item_eth *m = item->mask;
			if (have_ip || *vlan)
				return FAIL(err, EINVAL, ITEM, item, "ETH must come first");
			if (item->spec && m && !all_zero(m, sizeof(*m)))
				return FAIL(err, ENOTSUP, ITEM_MASK, item, "MAC addresses cannot be matched");
			break;
		}
		case RTE_FLOW_ITEM_TYPE_VLAN: {
			const struct rte_flow_item_vlan *s = item->spec, *m = item->mask;
			if (!s || !m || rte_be_to_cpu_16(m->hdr.vlan_tci) != 0x0fff ||
			    m->hdr.eth_proto || m->has_more_vlan)
				return FAIL(err, ENOTSUP, ITEM_MASK, item, "VLAN must match exactly the VID");
			if (*vlan || have_ip)
				return FAIL(err, ENOTSUP, ITEM, item, "one VLAN tag, before IPV4");
			*vlan = true;
			pf->e.key.vid = rte_be_to_cpu_16(s->hdr.vlan_tci) & 0xfff;
			break;
		}
		case RTE_FLOW_ITEM_TYPE_IPV4: {
			const struct rte_flow_item_ipv4 *s = item->spec, *m = item->mask;
			struct rte_ipv4_hdr hm;
			if (!s || !m)
				return FAIL(err, EINVAL, ITEM, item, "IPV4 needs spec and mask");
			if (m->hdr.src_addr != RTE_BE32(0xffffffff) || m->hdr.dst_addr != RTE_BE32(0xffffffff))
				return FAIL(err, ENOTSUP, ITEM_MASK, item, "IPv4 source and destination must match exactly");
			hm = m->hdr;
			hm.src_addr = 0;
			hm.dst_addr = 0;
			if (hm.next_proto_id == 0xff) {
				proto = s->hdr.next_proto_id;
				hm.next_proto_id = 0;
			}
			if (!all_zero(&hm, sizeof(hm)))
				return FAIL(err, ENOTSUP, ITEM_MASK, item, "only addresses and protocol can be matched");
			pf->e.key.sip = rte_be_to_cpu_32(s->hdr.src_addr);
			pf->e.key.dip = rte_be_to_cpu_32(s->hdr.dst_addr);
			have_ip = true;
			break;
		}
		case RTE_FLOW_ITEM_TYPE_TCP:
		case RTE_FLOW_ITEM_TYPE_UDP: {
			bool tcp = item->type == RTE_FLOW_ITEM_TYPE_TCP;
			rte_be16_t sp, dp, msp, mdp;
			if (!have_ip || have_l4)
				return FAIL(err, ENOTSUP, ITEM, item, "one TCP or UDP item after IPV4");
			if (!item->spec || !item->mask)
				return FAIL(err, EINVAL, ITEM, item, "L4 needs spec and mask");
			if (tcp) {
				const struct rte_flow_item_tcp *s = item->spec, *m = item->mask;
				struct rte_tcp_hdr hm = m->hdr;
				sp = s->hdr.src_port; dp = s->hdr.dst_port;
				msp = m->hdr.src_port; mdp = m->hdr.dst_port;
				hm.src_port = hm.dst_port = 0;
				if (!all_zero(&hm, sizeof(hm)))
					return FAIL(err, ENOTSUP, ITEM_MASK, item, "only TCP ports can be matched");
			} else {
				const struct rte_flow_item_udp *s = item->spec, *m = item->mask;
				struct rte_udp_hdr hm = m->hdr;
				sp = s->hdr.src_port; dp = s->hdr.dst_port;
				msp = m->hdr.src_port; mdp = m->hdr.dst_port;
				hm.src_port = hm.dst_port = 0;
				if (!all_zero(&hm, sizeof(hm)))
					return FAIL(err, ENOTSUP, ITEM_MASK, item, "only UDP ports can be matched");
			}
			if (msp != RTE_BE16(0xffff) || mdp != RTE_BE16(0xffff))
				return FAIL(err, ENOTSUP, ITEM_MASK, item, "ports must match exactly");
			if (proto >= 0 && proto != (tcp ? 6 : 17))
				return FAIL(err, EINVAL, ITEM, item, "IPv4 protocol disagrees with the L4 item");
			pf->e.key.tcp = tcp;
			pf->e.key.sport = rte_be_to_cpu_16(sp);
			pf->e.key.dport = rte_be_to_cpu_16(dp);
			have_l4 = true;
			break;
		}
		default:
			return FAIL(err, ENOTSUP, ITEM, item, "unsupported pattern item");
		}
	}
	if (!have_ip || !have_l4)
		return FAIL(err, EINVAL, ITEM_NUM, NULL, "pattern must be ETH / [VLAN] / IPV4 / TCP|UDP");
	return 0;
}

static int parse_actions(struct natgw_flow_ctx *ctx, const struct rte_flow_action *a, struct parsed_flow *pf,
			 bool vlan, struct rte_flow_error *err)
{
	bool ip_src = false, ip_dst = false, tp_src = false, tp_dst = false;
	bool mac_s = false, mac_d = false, port = false, set_vid = false;
	uint32_t new_ip = 0;
	uint16_t new_port = 0;

	for (; a->type != RTE_FLOW_ACTION_TYPE_END; a++) {
		switch (a->type) {
		case RTE_FLOW_ACTION_TYPE_VOID:
			break;
		case RTE_FLOW_ACTION_TYPE_SET_IPV4_SRC:
		case RTE_FLOW_ACTION_TYPE_SET_IPV4_DST: {
			const struct rte_flow_action_set_ipv4 *c = a->conf;
			if (!c)
				return FAIL(err, EINVAL, ACTION_CONF, a, "missing address");
			if (ip_src || ip_dst)
				return FAIL(err, ENOTSUP, ACTION, a, "one address rewrite per flow");
			if (a->type == RTE_FLOW_ACTION_TYPE_SET_IPV4_SRC)
				ip_src = true;
			else
				ip_dst = true;
			new_ip = rte_be_to_cpu_32(c->ipv4_addr);
			break;
		}
		case RTE_FLOW_ACTION_TYPE_SET_TP_SRC:
		case RTE_FLOW_ACTION_TYPE_SET_TP_DST: {
			const struct rte_flow_action_set_tp *c = a->conf;
			if (!c)
				return FAIL(err, EINVAL, ACTION_CONF, a, "missing port");
			if (tp_src || tp_dst)
				return FAIL(err, ENOTSUP, ACTION, a, "one port rewrite per flow");
			if (a->type == RTE_FLOW_ACTION_TYPE_SET_TP_SRC)
				tp_src = true;
			else
				tp_dst = true;
			new_port = rte_be_to_cpu_16(c->port);
			break;
		}
		case RTE_FLOW_ACTION_TYPE_DEC_TTL:
			pf->e.dec_ttl = 1;
			break;
		case RTE_FLOW_ACTION_TYPE_SET_MAC_SRC:
		case RTE_FLOW_ACTION_TYPE_SET_MAC_DST: {
			const struct rte_flow_action_set_mac *c = a->conf;
			if (!c)
				return FAIL(err, EINVAL, ACTION_CONF, a, "missing MAC");
			if (a->type == RTE_FLOW_ACTION_TYPE_SET_MAC_SRC) {
				memcpy(pf->nh.src_mac, c->mac_addr, 6);
				mac_s = true;
			} else {
				memcpy(pf->nh.dst_mac, c->mac_addr, 6);
				mac_d = true;
			}
			break;
		}
		case RTE_FLOW_ACTION_TYPE_PORT_ID:
		case RTE_FLOW_ACTION_TYPE_REPRESENTED_PORT: {
			uint32_t pid;
			if (!a->conf)
				return FAIL(err, EINVAL, ACTION_CONF, a, "missing port");
			if (a->type == RTE_FLOW_ACTION_TYPE_PORT_ID) {
				const struct rte_flow_action_port_id *c = a->conf;
				if (c->original)
					return FAIL(err, ENOTSUP, ACTION_CONF, a, "original port is not supported");
				pid = c->id;
			} else {
				const struct rte_flow_action_ethdev *c = a->conf;
				pid = c->port_id;
			}
			if (pid >= RTE_MAX_ETHPORTS || port_map[pid].ctx != ctx)
				return FAIL(err, EINVAL, ACTION_CONF, a, "egress port is not a port of this device");
			pf->nh.lane = (uint8_t)port_map[pid].lane;
			port = true;
			break;
		}
		case RTE_FLOW_ACTION_TYPE_OF_SET_VLAN_VID: {
			const struct rte_flow_action_of_set_vlan_vid *c = a->conf;
			if (!c)
				return FAIL(err, EINVAL, ACTION_CONF, a, "missing VID");
			if (!vlan)
				return FAIL(err, ENOTSUP, ACTION, a, "VLAN tags cannot be added; match a tagged frame");
			pf->nh.vid = rte_be_to_cpu_16(c->vlan_vid) & 0xfff;
			set_vid = true;
			break;
		}
		case RTE_FLOW_ACTION_TYPE_COUNT:
			break;
		case RTE_FLOW_ACTION_TYPE_AGE: {
			const struct rte_flow_action_age *c = a->conf;
			if (!c || c->timeout == 0)
				return FAIL(err, EINVAL, ACTION_CONF, a, "AGE needs a timeout");
			pf->has_age = true;
			pf->age_timeout = c->timeout;
			pf->age_ctx = c->context;
			break;
		}
		default:
			return FAIL(err, ENOTSUP, ACTION, a, "unsupported action");
		}
	}
	if (!mac_s || !mac_d)
		return FAIL(err, EINVAL, ACTION_NUM, NULL, "SET_MAC_SRC and SET_MAC_DST are required");
	if (!port)
		return FAIL(err, EINVAL, ACTION_NUM, NULL, "an egress port action is required");
	if ((ip_src || tp_src) && (ip_dst || tp_dst))
		return FAIL(err, ENOTSUP, ACTION_NUM, NULL, "rewrite either the source or the destination");

	pf->e.xlate_dst = ip_dst || tp_dst;
	if (pf->e.xlate_dst) {
		pf->e.new_ip = ip_dst ? new_ip : pf->e.key.dip;
		pf->e.new_port = tp_dst ? new_port : pf->e.key.dport;
	} else {
		pf->e.new_ip = ip_src ? new_ip : pf->e.key.sip;
		pf->e.new_port = tp_src ? new_port : pf->e.key.sport;
	}
	pf->nh.valid = 1;
	pf->nh.vlan = vlan;
	if (vlan && !set_vid)
		pf->nh.vid = pf->e.key.vid;
	return 0;
}

static int parse_flow(struct rte_eth_dev *dev, const struct rte_flow_attr *attr,
		      const struct rte_flow_item pattern[], const struct rte_flow_action actions[],
		      struct parsed_flow *pf, struct rte_flow_error *err)
{
	uint16_t pid = dev->data->port_id;
	struct natgw_flow_ctx *ctx = port_map[pid].ctx;
	bool vlan = false;
	int ret;

	if (!ctx)
		return FAIL(err, ENODEV, UNSPECIFIED, NULL, "port is not bound to a NAT block");
	if (!attr || !pattern || !actions)
		return FAIL(err, EINVAL, UNSPECIFIED, NULL, "missing attr, pattern or actions");
	if (!attr->ingress || attr->egress || attr->transfer)
		return FAIL(err, ENOTSUP, ATTR, attr, "only ingress flows are supported");
	if (attr->group || attr->priority)
		return FAIL(err, ENOTSUP, ATTR, attr, "group and priority must be 0");
	memset(pf, 0, sizeof(*pf));
	pf->e.key.lane = (uint8_t)port_map[pid].lane;
	ret = parse_pattern(pattern, pf, &vlan, err);
	if (ret)
		return ret;
	return parse_actions(ctx, actions, pf, vlan, err);
}

/* ------------------------------------------------------------------ */
/* flow operations */

static struct natgw_flow_ctx *dev_ctx(struct rte_eth_dev *dev)
{
	return port_map[dev->data->port_id].ctx;
}

static void apply_ops(struct natgw_flow_ctx *ctx, unsigned n)
{
	natgw_dev_apply(&ctx->dev, ctx->ops, n);
	for (unsigned i = 0; i < n; i++) {
		if (ctx->ops[i].clear) {
			ctx->by_idx[ctx->ops[i].idx] = NULL;
		} else {
			struct hkey hk = mk_hkey(&ctx->ops[i].entry.key);
			void *f = NULL;
			rte_hash_lookup_data(ctx->by_key, &hk, &f);
			ctx->by_idx[ctx->ops[i].idx] = f;
		}
	}
}

static int natgw_flow_validate(struct rte_eth_dev *dev, const struct rte_flow_attr *attr,
			       const struct rte_flow_item pattern[], const struct rte_flow_action actions[],
			       struct rte_flow_error *err)
{
	struct parsed_flow pf;

	return parse_flow(dev, attr, pattern, actions, &pf, err);
}

static void update_thresholds(struct natgw_flow_ctx *ctx, const struct rte_flow *f)
{
	uint64_t t;

	if (!f->has_age)
		return;
	t = (uint64_t)f->age_timeout * ctx->cfg.ticks_per_sec;
	if (t >= NO_THRESH)
		t = NO_THRESH - 1;
	if (t < ctx->thresh[f->e.key.tcp]) {
		ctx->thresh[f->e.key.tcp] = (uint32_t)t;
		write_thresholds(ctx);
	}
}

static struct rte_flow *natgw_flow_create(struct rte_eth_dev *dev, const struct rte_flow_attr *attr,
					  const struct rte_flow_item pattern[], const struct rte_flow_action actions[],
					  struct rte_flow_error *err)
{
	struct natgw_flow_ctx *ctx = dev_ctx(dev);
	struct parsed_flow pf;
	struct rte_flow *f;
	struct hkey hk;
	bool nh_new;
	int nh_idx, n;
	uint32_t idx;

	if (parse_flow(dev, attr, pattern, actions, &pf, err))
		return NULL;
	f = rte_zmalloc("natgw_flow", sizeof(*f), 0);
	if (!f) {
		FAIL(err, ENOMEM, UNSPECIFIED, NULL, "out of memory");
		return NULL;
	}
	hk = mk_hkey(&pf.e.key);

	rte_spinlock_lock(&ctx->lock);
	if (rte_hash_lookup(ctx->by_key, &hk) >= 0) {
		rte_spinlock_unlock(&ctx->lock);
		rte_free(f);
		FAIL(err, EEXIST, UNSPECIFIED, NULL, "a flow with this match already exists");
		return NULL;
	}
	nh_idx = natgw_nh_get(ctx->nht, &pf.nh, &nh_new);
	if (nh_idx < 0) {
		rte_spinlock_unlock(&ctx->lock);
		rte_free(f);
		FAIL(err, ENOSPC, UNSPECIFIED, NULL, "next-hop table full");
		return NULL;
	}
	if (nh_new)
		natgw_dev_write_nh(&ctx->dev, (uint16_t)nh_idx, &ctx->nht->nh[nh_idx]);
	pf.e.nh_idx = (uint16_t)nh_idx;
	pf.e.valid = 1;

	f->ctx = ctx;
	f->e = pf.e;
	f->port_id = dev->data->port_id;
	f->nh_idx = (uint16_t)nh_idx;
	f->has_age = pf.has_age;
	f->age_timeout = pf.age_timeout;
	f->age_ctx = pf.age_ctx ? pf.age_ctx : f;

	if (rte_hash_add_key_data(ctx->by_key, &hk, f) < 0) {
		natgw_nh_put(ctx->nht, (uint16_t)nh_idx);
		rte_spinlock_unlock(&ctx->lock);
		rte_free(f);
		FAIL(err, ENOSPC, UNSPECIFIED, NULL, "flow table full");
		return NULL;
	}
	n = natgw_table_insert(ctx->t, &f->e, ctx->ops, ctx->max_ops, &idx);
	if (n < 0) {
		rte_hash_del_key(ctx->by_key, &hk);
		if (natgw_nh_put(ctx->nht, (uint16_t)nh_idx) == 0) {
			struct natgw_nh off = {0};
			natgw_dev_write_nh(&ctx->dev, (uint16_t)nh_idx, &off);
		}
		rte_spinlock_unlock(&ctx->lock);
		rte_free(f);
		FAIL(err, -n == ENOSPC ? ENOSPC : -n, UNSPECIFIED, NULL,
		     n == -ENOSPC ? "hardware flow table full" : "flow table insert failed");
		return NULL;
	}
	apply_ops(ctx, (unsigned)n);
	TAILQ_INSERT_TAIL(&ctx->flows, f, next);
	ctx->nflows++;
	update_thresholds(ctx, f);
	rte_spinlock_unlock(&ctx->lock);
	return f;
}

static void flow_remove_locked(struct natgw_flow_ctx *ctx, struct rte_flow *f)
{
	struct hkey hk = mk_hkey(&f->e.key);
	int n = natgw_table_delete(ctx->t, &f->e.key, ctx->ops, ctx->max_ops);

	if (n > 0)
		apply_ops(ctx, (unsigned)n);
	rte_hash_del_key(ctx->by_key, &hk);
	if (natgw_nh_put(ctx->nht, f->nh_idx) == 0) {
		struct natgw_nh off = {0};
		natgw_dev_write_nh(&ctx->dev, f->nh_idx, &off);
	}
	TAILQ_REMOVE(&ctx->flows, f, next);
	ctx->nflows--;
}

static int natgw_flow_destroy(struct rte_eth_dev *dev, struct rte_flow *f, struct rte_flow_error *err)
{
	struct natgw_flow_ctx *ctx = dev_ctx(dev);

	if (!ctx || !f || f->ctx != ctx || f->port_id != dev->data->port_id)
		return FAIL(err, EINVAL, HANDLE, f, "not a flow of this port");
	rte_spinlock_lock(&ctx->lock);
	flow_remove_locked(ctx, f);
	rte_spinlock_unlock(&ctx->lock);
	rte_free(f);
	return 0;
}

static int natgw_flow_flush(struct rte_eth_dev *dev, struct rte_flow_error *err)
{
	struct natgw_flow_ctx *ctx = dev_ctx(dev);
	struct rte_flow *f, *tmp;

	if (!ctx)
		return FAIL(err, ENODEV, UNSPECIFIED, NULL, "port is not bound to a NAT block");
	rte_spinlock_lock(&ctx->lock);
	for (f = TAILQ_FIRST(&ctx->flows); f != NULL; f = tmp) {
		tmp = TAILQ_NEXT(f, next);
		if (f->port_id != dev->data->port_id)
			continue;
		flow_remove_locked(ctx, f);
		rte_free(f);
	}
	rte_spinlock_unlock(&ctx->lock);
	return 0;
}

static int read_flow_state(struct natgw_flow_ctx *ctx, const struct rte_flow *f, struct natgw_state *s)
{
	uint32_t idx;

	if (natgw_table_find(ctx->t, &f->e.key, &idx) != 0)
		return -ENOENT;
	natgw_dev_read_state(&ctx->dev, idx, s);
	return 0;
}

static int natgw_flow_query(struct rte_eth_dev *dev, struct rte_flow *f, const struct rte_flow_action *a,
			    void *data, struct rte_flow_error *err)
{
	struct natgw_flow_ctx *ctx = dev_ctx(dev);
	struct natgw_state s;
	uint32_t now;
	int ret = 0;

	if (!ctx || !f || f->ctx != ctx)
		return FAIL(err, EINVAL, HANDLE, f, "not a flow of this port");
	rte_spinlock_lock(&ctx->lock);
	if (read_flow_state(ctx, f, &s) != 0) {
		rte_spinlock_unlock(&ctx->lock);
		return FAIL(err, EIO, HANDLE, f, "flow missing from the table");
	}
	now = natgw_dev_tick(&ctx->dev);
	for (; a->type != RTE_FLOW_ACTION_TYPE_END && !ret; a++) {
		switch (a->type) {
		case RTE_FLOW_ACTION_TYPE_VOID:
			break;
		case RTE_FLOW_ACTION_TYPE_COUNT: {
			struct rte_flow_query_count *q = data;
			q->hits_set = 1;
			q->bytes_set = 1;
			q->hits = s.pkts - f->cnt_base_pkts;
			q->bytes = s.bytes - f->cnt_base_bytes;
			if (q->reset) {
				f->cnt_base_pkts = s.pkts;
				f->cnt_base_bytes = s.bytes;
			}
			break;
		}
		case RTE_FLOW_ACTION_TYPE_AGE: {
			struct rte_flow_query_age *q = data;
			if (!f->has_age) {
				ret = FAIL(err, ENOTSUP, ACTION, a, "flow has no AGE action");
				break;
			}
			memset(q, 0, sizeof(*q));
			q->aged = f->aged;
			q->sec_since_last_hit_valid = 1;
			q->sec_since_last_hit = (now - s.ts) / ctx->cfg.ticks_per_sec;
			break;
		}
		default:
			ret = FAIL(err, ENOTSUP, ACTION, a, "query supports COUNT and AGE");
		}
	}
	rte_spinlock_unlock(&ctx->lock);
	return ret;
}

static int natgw_flow_get_aged(struct rte_eth_dev *dev, void **contexts, uint32_t nb, struct rte_flow_error *err)
{
	struct natgw_flow_ctx *ctx = dev_ctx(dev);
	struct rte_flow *f;
	uint32_t n = 0;

	if (!ctx)
		return FAIL(err, ENODEV, UNSPECIFIED, NULL, "port is not bound to a NAT block");
	rte_spinlock_lock(&ctx->lock);
	TAILQ_FOREACH(f, &ctx->flows, next) {
		if (!f->aged || f->port_id != dev->data->port_id)
			continue;
		if (nb && contexts) {
			if (n >= nb)
				break;
			contexts[n] = f->age_ctx;
		}
		n++;
	}
	rte_spinlock_unlock(&ctx->lock);
	return (int)n;
}

static const struct rte_flow_ops ops = {
	.validate = natgw_flow_validate,
	.create = natgw_flow_create,
	.destroy = natgw_flow_destroy,
	.flush = natgw_flow_flush,
	.query = natgw_flow_query,
	.get_aged_flows = natgw_flow_get_aged,
};

const struct rte_flow_ops *natgw_flow_ops(void)
{
	return &ops;
}

/* ------------------------------------------------------------------ */
/* events and aging */

/* returns true when the flow became aged */
static bool check_age_locked(struct natgw_flow_ctx *ctx, struct rte_flow *f, uint32_t now)
{
	struct natgw_state s;

	if (!f->has_age || f->aged || read_flow_state(ctx, f, &s) != 0)
		return false;
	if ((uint64_t)(uint32_t)(now - s.ts) >= (uint64_t)f->age_timeout * ctx->cfg.ticks_per_sec) {
		f->aged = true;
		f->idle_pending = false;
		return true;
	}
	if (f->idle_pending && s.ts != f->pending_ts)
		f->idle_pending = false;           /* traffic resumed; a new idle event will follow */
	return false;
}

void natgw_flow_poll(struct natgw_flow_ctx *ctx)
{
	bool notify[NATGW_LANES] = {false};
	struct natgw_event ev;
	struct rte_flow *f;
	uint32_t now;

	rte_spinlock_lock(&ctx->lock);
	now = natgw_dev_tick(&ctx->dev);
	while (natgw_dev_pop_event(&ctx->dev, &ev)) {
		switch (ev.type) {
		case NATGW_EVT_IDLE:
			if (ev.idx < natgw_table_size(ctx->t) && (f = ctx->by_idx[ev.idx]) != NULL && f->has_age) {
				struct natgw_state s;
				if (check_age_locked(ctx, f, now)) {
					notify[f->e.key.lane] = true;
				} else if (!f->aged && read_flow_state(ctx, f, &s) == 0) {
					f->idle_pending = true;
					f->pending_ts = s.ts;
				}
			}
			break;
		case NATGW_EVT_FIN:
			ctx->ev_fin++;
			break;
		case NATGW_EVT_RST:
			ctx->ev_rst++;
			break;
		case NATGW_EVT_OVF:
			ctx->ev_ovf++;
			ctx->resync = true;
			break;
		default:
			break;
		}
	}
	TAILQ_FOREACH(f, &ctx->flows, next) {
		if ((f->idle_pending || ctx->resync) && check_age_locked(ctx, f, now))
			notify[f->e.key.lane] = true;
	}
	ctx->resync = false;
	rte_spinlock_unlock(&ctx->lock);

	for (unsigned l = 0; l < ctx->cfg.lanes; l++) {
		if (notify[l] && ctx->lane_bound[l])
			rte_eth_dev_callback_process(&rte_eth_devices[ctx->lane_port[l]], RTE_ETH_EVENT_FLOW_AGED, NULL);
	}
}

/* ------------------------------------------------------------------ */
/* xstats */

static const char *const reason_names[NATGW_STAT_COUNT] = {
	"miss", "bypass", "not_ipv4", "mcast", "ip_hdr", "frag", "ttl", "proto",
	"csum", "syn", "finrst", "vlan", "nh", "rsvd13", "rsvd14", "forwarded",
	"ingress_drop", "bad_fcs",
};

unsigned natgw_flow_xstats_count(void)
{
	return NATGW_STAT_COUNT + 4;
}

int natgw_flow_xstats_get_names(struct natgw_flow_ctx *ctx, struct rte_eth_xstat_name *names, unsigned size)
{
	unsigned n = natgw_flow_xstats_count(), i = 0;

	(void)ctx;
	if (!names || size < n)
		return (int)n;
	for (unsigned r = 0; r < NATGW_STAT_COUNT; r++, i++)
		snprintf(names[i].name, sizeof(names[i].name), "natgw_%s_%s", r == NATGW_RSN_FWD ? "rx" : "punt",
			 reason_names[r]);
	snprintf(names[i++].name, sizeof(names[0].name), "natgw_flows");
	snprintf(names[i++].name, sizeof(names[0].name), "natgw_events_fin");
	snprintf(names[i++].name, sizeof(names[0].name), "natgw_events_rst");
	snprintf(names[i++].name, sizeof(names[0].name), "natgw_events_lost");
	return (int)n;
}

int natgw_flow_xstats_get(struct natgw_flow_ctx *ctx, unsigned lane, struct rte_eth_xstat *xstats, unsigned size)
{
	unsigned n = natgw_flow_xstats_count(), i = 0;

	if (!xstats || size < n)
		return (int)n;
	rte_spinlock_lock(&ctx->lock);
	for (unsigned r = 0; r < NATGW_STAT_COUNT; r++, i++) {
		xstats[i].id = i;
		xstats[i].value = natgw_dev_read_stat(&ctx->dev, lane, r);
	}
	xstats[i].id = i; xstats[i++].value = ctx->nflows;
	xstats[i].id = i; xstats[i++].value = ctx->ev_fin;
	xstats[i].id = i; xstats[i++].value = ctx->ev_rst;
	xstats[i].id = i; xstats[i++].value = natgw_dev_event_drops(&ctx->dev);
	rte_spinlock_unlock(&ctx->lock);
	return (int)n;
}

/* ------------------------------------------------------------------ */
/* punt metadata */

static int punt_off = -1;
static uint64_t punt_flag;

int natgw_punt_meta_register(void)
{
	static const struct rte_mbuf_dynfield fd = {
		.name = NATGW_PUNT_DYNFIELD,
		.size = sizeof(struct natgw_punt_meta),
		.align = __alignof__(struct natgw_punt_meta),
	};
	static const struct rte_mbuf_dynflag fl = { .name = NATGW_PUNT_DYNFLAG };
	int off, bit;

	if (punt_off >= 0)
		return 0;
	off = rte_mbuf_dynfield_register(&fd);
	if (off < 0)
		return -rte_errno;
	bit = rte_mbuf_dynflag_register(&fl);
	if (bit < 0)
		return -rte_errno;
	punt_flag = RTE_BIT64(bit);
	punt_off = off;
	return 0;
}

int natgw_punt_strip(struct rte_mbuf *m)
{
	struct natgw_punt p;
	struct natgw_punt_meta *meta;

	if (punt_off < 0 || rte_pktmbuf_data_len(m) < NATGW_PUNT_HDR_LEN)
		return -EINVAL;
	if (natgw_punt_parse(rte_pktmbuf_mtod(m, const uint8_t *), NATGW_PUNT_HDR_LEN, &p) != 0)
		return -EINVAL;
	meta = RTE_MBUF_DYNFIELD(m, punt_off, struct natgw_punt_meta *);
	meta->idx = p.idx;
	meta->hash = p.hash;
	meta->reason = p.reason;
	meta->flags = p.flags;
	meta->lane = p.lane;
	meta->rsvd = 0;
	m->ol_flags |= punt_flag;
	if (p.hash) {
		m->hash.rss = p.hash;
		m->ol_flags |= RTE_MBUF_F_RX_RSS_HASH;
	}
	rte_pktmbuf_adj(m, NATGW_PUNT_HDR_LEN);
	return 0;
}
