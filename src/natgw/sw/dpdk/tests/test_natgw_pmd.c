/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * DPDK-level tests of the natgw rte_flow offload, using the net_natgw_model
 * virtual device (the shim software model behind ethdev ports).
 *
 * Two devices are created:
 *   dev A: net_natgw_modelA, 3 lanes (ports 0-2), 128 entries, manual clock, punt headers
 *   dev B: net_natgw_modelB, 2 lanes (ports 3-4), punt_hdr=0
 *   dev C: net_natgw_modelC, 2 lanes (ports 5-6), 32 entries on chip, DDR tier of 256
 *   dev D: net_natgw_modelD, 2 lanes (ports 7-8), DDR tier present, memory not calibrated
 *   dev E: net_natgw_modelE, 2 lanes (ports 9-10), DDR tier present, no_ddr=1
 *   dev F: net_natgw_modelF, 2 lanes (ports 11-12), DDR tier, balanced policy with
 *          short timings (on chip 50%/25%, demote after 2 s idle, promote 2 of 3)
 *   dev G: net_natgw_modelG, 2 lanes (ports 13-14), DDR tier, tier_policy=fill
 * Each test prints PASS/FAIL; the exit status is the number of failed tests.
 */

#include <errno.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <rte_byteorder.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_flow.h>
#include <rte_ip.h>
#include <rte_launch.h>
#include <rte_lcore.h>
#include <rte_mbuf.h>
#include <rte_mbuf_dyn.h>
#include <rte_tcp.h>
#include <rte_udp.h>

#include <rte_pmd_natgw_model.h>

/* mirrored from natgw.h / natgw_flow.h (an application sees only these) */
#define RSN_MISS   0
#define RSN_FINRST 10
#define RSN_VLAN   11
#define PUNT_F_HIT 0x08
struct punt_meta {
	uint32_t idx, hash;
	uint8_t reason, flags, lane, rsvd;
};

#define PA0 0
#define PA1 1
#define PA2 2
#define PB0 3
#define PB1 4
#define PC0 5
#define PC1 6
#define PD0 7
#define PE0 9
#define PF0 11
#define PF1 12
#define PG0 13
#define IDX_DDR 0x80000000u

static struct rte_mempool *mp;
static int punt_off = -1;
static uint64_t punt_flag;
static int failures, test_failed;
static const char *test_name;

#define CHECK(cond, ...) do { \
	if (!(cond)) { \
		printf("  FAIL %s:%d: %s: ", __FILE__, __LINE__, #cond); \
		printf(__VA_ARGS__); \
		printf("\n"); \
		test_failed = 1; \
	} \
} while (0)

/* ------------------------------------------------------------------ */
/* frames */

struct tuple {
	uint32_t sip, dip;
	uint16_t sport, dport;
	int tcp;
	uint8_t flags;      /* TCP flags */
	int vid;            /* -1: untagged */
};

static const uint8_t gw_mac[6] = {0x02, 0x00, 0x00, 0x00, 0xaa, 0x00};
static const uint8_t host_mac[6] = {0x02, 0x00, 0x00, 0x00, 0x11, 0x00};

/* build an Ethernet/[VLAN]/IPv4/TCP|UDP frame with valid checksums */
static uint16_t build(uint8_t *buf, const struct tuple *t, uint16_t payload, uint8_t ttl)
{
	struct rte_ether_hdr *eh = (void *)buf;
	uint16_t off = sizeof(*eh), l4len;
	struct rte_ipv4_hdr *ip;

	memcpy(eh->dst_addr.addr_bytes, gw_mac, 6);
	memcpy(eh->src_addr.addr_bytes, host_mac, 6);
	if (t->vid >= 0) {
		struct rte_vlan_hdr *vh = (void *)(buf + off);
		eh->ether_type = rte_cpu_to_be_16(RTE_ETHER_TYPE_VLAN);
		vh->vlan_tci = rte_cpu_to_be_16((uint16_t)(0x2000 | t->vid));
		vh->eth_proto = rte_cpu_to_be_16(RTE_ETHER_TYPE_IPV4);
		off += sizeof(*vh);
	} else {
		eh->ether_type = rte_cpu_to_be_16(RTE_ETHER_TYPE_IPV4);
	}
	ip = (void *)(buf + off);
	l4len = (uint16_t)((t->tcp ? sizeof(struct rte_tcp_hdr) : sizeof(struct rte_udp_hdr)) + payload);
	memset(ip, 0, sizeof(*ip));
	ip->version_ihl = RTE_IPV4_VHL_DEF;
	ip->total_length = rte_cpu_to_be_16((uint16_t)(sizeof(*ip) + l4len));
	ip->time_to_live = ttl;
	ip->next_proto_id = t->tcp ? IPPROTO_TCP : IPPROTO_UDP;
	ip->src_addr = rte_cpu_to_be_32(t->sip);
	ip->dst_addr = rte_cpu_to_be_32(t->dip);
	ip->packet_id = rte_cpu_to_be_16(0x1234);
	for (uint16_t i = 0; i < payload; i++)
		buf[off + sizeof(*ip) + l4len - payload + i] = (uint8_t)(i * 7 + 3);
	if (t->tcp) {
		struct rte_tcp_hdr *th = (void *)(ip + 1);
		memset(th, 0, sizeof(*th));
		th->src_port = rte_cpu_to_be_16(t->sport);
		th->dst_port = rte_cpu_to_be_16(t->dport);
		th->data_off = 0x50;
		th->tcp_flags = t->flags ? t->flags : RTE_TCP_ACK_FLAG;
		th->rx_win = rte_cpu_to_be_16(1024);
		th->cksum = rte_ipv4_udptcp_cksum(ip, th);
	} else {
		struct rte_udp_hdr *uh = (void *)(ip + 1);
		uh->src_port = rte_cpu_to_be_16(t->sport);
		uh->dst_port = rte_cpu_to_be_16(t->dport);
		uh->dgram_len = rte_cpu_to_be_16(l4len);
		uh->dgram_cksum = 0;
		uh->dgram_cksum = rte_ipv4_udptcp_cksum(ip, uh);
	}
	ip->hdr_checksum = rte_ipv4_cksum(ip);
	return (uint16_t)(off + sizeof(*ip) + l4len);
}

static struct rte_ipv4_hdr *ip_of(uint8_t *buf)
{
	struct rte_ether_hdr *eh = (void *)buf;
	return (void *)(buf + sizeof(*eh) + (eh->ether_type == rte_cpu_to_be_16(RTE_ETHER_TYPE_VLAN) ? 4 : 0));
}

static int ip_csum_ok(struct rte_ipv4_hdr *ip)
{
	return rte_ipv4_cksum(ip) == 0; /* the sum includes the checksum field */
}

static int l4_csum_ok(struct rte_ipv4_hdr *ip)
{
	return rte_ipv4_udptcp_cksum_verify(ip, ip + 1) == 0;
}

/* ------------------------------------------------------------------ */
/* flows */

struct nat {
	int dnat;            /* 0: rewrite source, 1: rewrite destination */
	uint32_t new_ip;
	uint16_t new_port;
	uint16_t egress;
	uint8_t dst_mac[6], src_mac[6];
	int dec_ttl;
	int age;             /* seconds, 0: none */
	void *age_ctx;
	int set_vid;         /* -1: keep */
};

static uint32_t create_priority;    /* 1: bulk (DDR) */

static struct rte_flow *create(uint16_t port, const struct tuple *t, const struct nat *n, struct rte_flow_error *err)
{
	struct rte_flow_attr attr = { .ingress = 1, .priority = create_priority };
	struct rte_flow_item_vlan vs = {0}, vm = {0};
	struct rte_flow_item_ipv4 ips = {0}, ipm = {0};
	struct rte_flow_item_tcp ts = {0}, tm = {0};
	struct rte_flow_item_udp us = {0}, um = {0};
	struct rte_flow_item pat[5];
	int i = 0;

	pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_ETH };
	if (t->vid >= 0) {
		vs.hdr.vlan_tci = rte_cpu_to_be_16((uint16_t)t->vid);
		vm.hdr.vlan_tci = rte_cpu_to_be_16(0x0fff);
		pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_VLAN, .spec = &vs, .mask = &vm };
	}
	ips.hdr.src_addr = rte_cpu_to_be_32(t->sip);
	ips.hdr.dst_addr = rte_cpu_to_be_32(t->dip);
	ipm.hdr.src_addr = RTE_BE32(0xffffffff);
	ipm.hdr.dst_addr = RTE_BE32(0xffffffff);
	pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_IPV4, .spec = &ips, .mask = &ipm };
	if (t->tcp) {
		ts.hdr.src_port = rte_cpu_to_be_16(t->sport);
		ts.hdr.dst_port = rte_cpu_to_be_16(t->dport);
		tm.hdr.src_port = tm.hdr.dst_port = RTE_BE16(0xffff);
		pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_TCP, .spec = &ts, .mask = &tm };
	} else {
		us.hdr.src_port = rte_cpu_to_be_16(t->sport);
		us.hdr.dst_port = rte_cpu_to_be_16(t->dport);
		um.hdr.src_port = um.hdr.dst_port = RTE_BE16(0xffff);
		pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_UDP, .spec = &us, .mask = &um };
	}
	pat[i] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_END };

	struct rte_flow_action_set_ipv4 sip = { .ipv4_addr = rte_cpu_to_be_32(n->new_ip) };
	struct rte_flow_action_set_tp stp = { .port = rte_cpu_to_be_16(n->new_port) };
	struct rte_flow_action_set_mac smac, dmac;
	struct rte_flow_action_port_id pid = { .id = n->egress };
	struct rte_flow_action_count cnt = {0};
	struct rte_flow_action_age age = { .timeout = (uint32_t)n->age, .context = n->age_ctx };
	struct rte_flow_action_of_set_vlan_vid vid = { .vlan_vid = rte_cpu_to_be_16((uint16_t)(n->set_vid < 0 ? 0 : n->set_vid)) };
	struct rte_flow_action act[12];
	int j = 0;

	memcpy(smac.mac_addr, n->src_mac, 6);
	memcpy(dmac.mac_addr, n->dst_mac, 6);
	act[j++] = (struct rte_flow_action){ .type = n->dnat ? RTE_FLOW_ACTION_TYPE_SET_IPV4_DST : RTE_FLOW_ACTION_TYPE_SET_IPV4_SRC, .conf = &sip };
	act[j++] = (struct rte_flow_action){ .type = n->dnat ? RTE_FLOW_ACTION_TYPE_SET_TP_DST : RTE_FLOW_ACTION_TYPE_SET_TP_SRC, .conf = &stp };
	if (n->dec_ttl)
		act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_DEC_TTL };
	act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_SET_MAC_SRC, .conf = &smac };
	act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_SET_MAC_DST, .conf = &dmac };
	if (n->set_vid >= 0)
		act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_OF_SET_VLAN_VID, .conf = &vid };
	act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_PORT_ID, .conf = &pid };
	act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_COUNT, .conf = &cnt };
	if (n->age)
		act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_AGE, .conf = &age };
	act[j] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_END };
	return rte_flow_create(port, &attr, pat, act, err);
}

static struct nat snat_to(uint16_t egress, uint32_t ip, uint16_t port)
{
	struct nat n = { .dnat = 0, .new_ip = ip, .new_port = port, .egress = egress, .dec_ttl = 1, .set_vid = -1,
			 .dst_mac = {0x02, 0, 0, 0, 0x22, 0}, .src_mac = {0x02, 0, 0, 0, 0xaa, 0x01} };
	return n;
}

/* inject on a port's wire, let the model process it */
static void inject(uint16_t port, const uint8_t *frame, uint16_t len)
{
	CHECK(rte_pmd_natgw_model_wire_inject(port, frame, len) == 0, "inject failed");
	rte_pmd_natgw_model_service(port);
}

static int wire_take(uint16_t port, uint8_t *buf, int *fwd)
{
	return rte_pmd_natgw_model_wire_recv(port, buf, 9600, fwd);
}

static void drain(void)
{
	uint8_t buf[9600];
	struct rte_mbuf *m[32];
	for (uint16_t p = 0; p < rte_eth_dev_count_avail(); p++) {
		while (wire_take(p, buf, NULL) > 0)
			;
		uint16_t n;
		while ((n = rte_eth_rx_burst(p, 0, m, 32)) > 0)
			rte_pktmbuf_free_bulk(m, n);
	}
}

static struct punt_meta *meta_of(struct rte_mbuf *m)
{
	return RTE_MBUF_DYNFIELD(m, punt_off, struct punt_meta *);
}

/* ------------------------------------------------------------------ */
/* tests */

static void test_ports(void)
{
	struct rte_eth_link link;
	struct rte_eth_dev_info info;

	CHECK(rte_eth_dev_count_avail() == 15, "ports: %u", rte_eth_dev_count_avail());
	for (uint16_t p = 0; p < 15; p++) {
		CHECK(rte_eth_link_get_nowait(p, &link) == 0 && link.link_status == RTE_ETH_LINK_UP, "port %u down", p);
		CHECK(rte_eth_dev_info_get(p, &info) == 0 && strcmp(info.driver_name, "net_natgw_model") == 0,
		      "driver %s", info.driver_name);
	}
	CHECK(rte_eth_xstats_get_names(PA0, NULL, 0) >= 22, "xstats count");
}

static void test_validate_rejects(void)
{
	struct rte_flow_attr ing = { .ingress = 1 }, egr = { .egress = 1 }, grp = { .ingress = 1, .group = 1 };
	struct rte_flow_item_eth es = {0}, em = {0};
	struct rte_flow_item_ipv4 ips = {0}, ipm = {0}, ipm_half = {0};
	struct rte_flow_item_udp us = {0}, um = {0};
	struct rte_flow_item_tcp tm_flags = {0}, ts = {0};
	struct rte_flow_action_set_mac mac = {{0}};
	struct rte_flow_action_port_id pid = { .id = PA1 }, pid_other = { .id = PB0 };
	struct rte_flow_error err;

	ipm.hdr.src_addr = ipm.hdr.dst_addr = RTE_BE32(0xffffffff);
	ipm_half.hdr.src_addr = RTE_BE32(0xffffffff);
	um.hdr.src_port = um.hdr.dst_port = RTE_BE16(0xffff);
	tm_flags.hdr.src_port = tm_flags.hdr.dst_port = RTE_BE16(0xffff);
	tm_flags.hdr.tcp_flags = 0xff;
	memset(em.hdr.dst_addr.addr_bytes, 0xff, 6);

	struct rte_flow_item good[] = {
		{ .type = RTE_FLOW_ITEM_TYPE_ETH },
		{ .type = RTE_FLOW_ITEM_TYPE_IPV4, .spec = &ips, .mask = &ipm },
		{ .type = RTE_FLOW_ITEM_TYPE_UDP, .spec = &us, .mask = &um },
		{ .type = RTE_FLOW_ITEM_TYPE_END } };
	struct rte_flow_action ok_act[] = {
		{ .type = RTE_FLOW_ACTION_TYPE_SET_MAC_SRC, .conf = &mac },
		{ .type = RTE_FLOW_ACTION_TYPE_SET_MAC_DST, .conf = &mac },
		{ .type = RTE_FLOW_ACTION_TYPE_PORT_ID, .conf = &pid },
		{ .type = RTE_FLOW_ACTION_TYPE_END } };

	CHECK(rte_flow_validate(PA0, &ing, good, ok_act, &err) == 0, "minimal valid flow rejected: %s", err.message);

	struct { const char *what; const struct rte_flow_attr *attr; struct rte_flow_item pat[5];
		 struct rte_flow_action act[5]; int code; } cases[] = {
		{ "egress attr", &egr, {{0}}, {{0}}, ENOTSUP },
		{ "group", &grp, {{0}}, {{0}}, ENOTSUP },
		{ "MAC match", &ing, { { .type = RTE_FLOW_ITEM_TYPE_ETH, .spec = &es, .mask = &em },
			{ .type = RTE_FLOW_ITEM_TYPE_IPV4, .spec = &ips, .mask = &ipm },
			{ .type = RTE_FLOW_ITEM_TYPE_UDP, .spec = &us, .mask = &um }, { .type = RTE_FLOW_ITEM_TYPE_END } },
			{{0}}, ENOTSUP },
		{ "partial IPv4 mask", &ing, { { .type = RTE_FLOW_ITEM_TYPE_ETH },
			{ .type = RTE_FLOW_ITEM_TYPE_IPV4, .spec = &ips, .mask = &ipm_half },
			{ .type = RTE_FLOW_ITEM_TYPE_UDP, .spec = &us, .mask = &um }, { .type = RTE_FLOW_ITEM_TYPE_END } },
			{{0}}, ENOTSUP },
		{ "TCP flags match", &ing, { { .type = RTE_FLOW_ITEM_TYPE_ETH },
			{ .type = RTE_FLOW_ITEM_TYPE_IPV4, .spec = &ips, .mask = &ipm },
			{ .type = RTE_FLOW_ITEM_TYPE_TCP, .spec = &ts, .mask = &tm_flags }, { .type = RTE_FLOW_ITEM_TYPE_END } },
			{{0}}, ENOTSUP },
		{ "no L4", &ing, { { .type = RTE_FLOW_ITEM_TYPE_ETH },
			{ .type = RTE_FLOW_ITEM_TYPE_IPV4, .spec = &ips, .mask = &ipm }, { .type = RTE_FLOW_ITEM_TYPE_END } },
			{{0}}, EINVAL },
		{ "drop action", &ing, {{0}}, { { .type = RTE_FLOW_ACTION_TYPE_DROP }, { .type = RTE_FLOW_ACTION_TYPE_END } }, ENOTSUP },
		{ "no egress port", &ing, {{0}}, { { .type = RTE_FLOW_ACTION_TYPE_SET_MAC_SRC, .conf = &mac },
			{ .type = RTE_FLOW_ACTION_TYPE_SET_MAC_DST, .conf = &mac }, { .type = RTE_FLOW_ACTION_TYPE_END } }, EINVAL },
		{ "egress on another device", &ing, {{0}}, { { .type = RTE_FLOW_ACTION_TYPE_SET_MAC_SRC, .conf = &mac },
			{ .type = RTE_FLOW_ACTION_TYPE_SET_MAC_DST, .conf = &mac },
			{ .type = RTE_FLOW_ACTION_TYPE_PORT_ID, .conf = &pid_other }, { .type = RTE_FLOW_ACTION_TYPE_END } }, EINVAL },
		{ "missing MAC", &ing, {{0}}, { { .type = RTE_FLOW_ACTION_TYPE_PORT_ID, .conf = &pid },
			{ .type = RTE_FLOW_ACTION_TYPE_END } }, EINVAL },
	};
	for (size_t c = 0; c < RTE_DIM(cases); c++) {
		const struct rte_flow_item *p = cases[c].pat[0].type || cases[c].pat[1].type ? cases[c].pat : good;
		const struct rte_flow_action *a = cases[c].act[0].type || cases[c].act[1].type ? cases[c].act : ok_act;
		memset(&err, 0, sizeof(err));
		int rc = rte_flow_validate(PA0, cases[c].attr, p, a, &err);
		CHECK(rc == -cases[c].code, "%s: rc %d (%s)", cases[c].what, rc, err.message ? err.message : "");
		CHECK(err.message != NULL, "%s: no error message", cases[c].what);
	}
}

static void test_snat_dnat_forwarding(void)
{
	struct tuple out = { .sip = 0xc0a8010a, .dip = 0x08080808, .sport = 40000, .dport = 53, .tcp = 0, .vid = -1 };
	struct tuple in = { .sip = 0x08080808, .dip = 0xc6336401, .sport = 53, .dport = 20000, .tcp = 0, .vid = -1 };
	struct nat sn = snat_to(PA1, 0xc6336401, 20000);
	struct nat dn = snat_to(PA0, 0xc0a8010a, 40000);
	struct rte_flow_error err;
	struct rte_flow *f1, *f2;
	uint8_t frame[1600], got[9600];
	int fwd = 0;

	dn.dnat = 1;
	memcpy(dn.dst_mac, host_mac, 6);
	memcpy(dn.src_mac, gw_mac, 6);
	f1 = create(PA0, &out, &sn, &err);
	f2 = create(PA1, &in, &dn, &err);
	CHECK(f1 && f2, "create: %s", err.message);

	for (int tcp = 0; tcp < 2; tcp++) {
		out.tcp = in.tcp = tcp;
		if (tcp) {
			struct rte_flow *t1 = create(PA0, &out, &sn, &err), *t2 = create(PA1, &in, &dn, &err);
			CHECK(t1 && t2, "tcp create: %s", err.message);
		}
		uint16_t len = build(frame, &out, 100, 64);
		inject(PA0, frame, len);
		int n = wire_take(PA1, got, &fwd);
		CHECK(n == len && fwd, "SNAT: %d bytes on WAN wire, fwd %d", n, fwd);
		struct rte_ipv4_hdr *ip = ip_of(got);
		CHECK(rte_be_to_cpu_32(ip->src_addr) == 0xc6336401 && rte_be_to_cpu_32(ip->dst_addr) == 0x08080808, "SNAT addresses");
		CHECK(ip->time_to_live == 63, "TTL %u", ip->time_to_live);
		CHECK(ip_csum_ok(ip), "IPv4 checksum");
		CHECK(l4_csum_ok(ip), "L4 checksum (tcp=%d)", tcp);
		CHECK(rte_be_to_cpu_16(((struct rte_udp_hdr *)(ip + 1))->src_port) == 20000, "SNAT port");
		CHECK(memcmp(got, sn.dst_mac, 6) == 0 && memcmp(got + 6, sn.src_mac, 6) == 0, "SNAT MACs");
		CHECK(memcmp(got + 34 + (tcp ? 20 : 8), frame + 34 + (tcp ? 20 : 8), 100) == 0, "payload unchanged");

		len = build(frame, &in, 33, 50);
		inject(PA1, frame, len);
		n = wire_take(PA0, got, &fwd);
		CHECK(n == len && fwd, "DNAT: %d bytes on LAN wire", n);
		ip = ip_of(got);
		CHECK(rte_be_to_cpu_32(ip->dst_addr) == 0xc0a8010a && rte_be_to_cpu_32(ip->src_addr) == 0x08080808, "DNAT addresses");
		CHECK(rte_be_to_cpu_16(((struct rte_udp_hdr *)(ip + 1))->dst_port) == 40000, "DNAT port");
		CHECK(ip->time_to_live == 49 && ip_csum_ok(ip) && l4_csum_ok(ip), "DNAT TTL/checksums");
		CHECK(memcmp(got, host_mac, 6) == 0, "DNAT dst MAC");
	}
	/* nothing reached the host */
	struct rte_mbuf *m[8];
	CHECK(rte_eth_rx_burst(PA0, 0, m, 8) == 0 && rte_eth_rx_burst(PA1, 0, m, 8) == 0, "host saw forwarded frames");
	CHECK(rte_flow_flush(PA0, &err) == 0 && rte_flow_flush(PA1, &err) == 0, "flush");
}

static void test_udp_zero_checksum(void)
{
	struct tuple t = { .sip = 1, .dip = 2, .sport = 3, .dport = 4, .tcp = 0, .vid = -1 };
	struct nat n = snat_to(PA2, 0x0a000001, 5000);
	struct rte_flow_error err;
	uint8_t frame[256], got[9600];
	int fwd;

	CHECK(create(PA0, &t, &n, &err) != NULL, "create: %s", err.message);
	uint16_t len = build(frame, &t, 20, 64);
	struct rte_ipv4_hdr *ip = ip_of(frame);
	((struct rte_udp_hdr *)(ip + 1))->dgram_cksum = 0;
	inject(PA0, frame, len);
	CHECK(wire_take(PA2, got, &fwd) == len && fwd, "forwarded");
	CHECK(((struct rte_udp_hdr *)(ip_of(got) + 1))->dgram_cksum == 0, "zero UDP checksum must stay zero");
	rte_flow_flush(PA0, &err);
}

static void test_punts(void)
{
	struct tuple t = { .sip = 0xc0a80114, .dip = 0x01010101, .sport = 1111, .dport = 80, .tcp = 1, .vid = -1 };
	struct nat n = snat_to(PA1, 0xc6336401, 21000);
	struct rte_flow_error err;
	struct rte_mbuf *m[4];
	uint8_t frame[256];
	uint16_t len;

	/* miss */
	len = build(frame, &t, 10, 64);
	inject(PA0, frame, len);
	CHECK(rte_eth_rx_burst(PA0, 0, m, 4) == 1, "miss not punted");
	CHECK(rte_pktmbuf_pkt_len(m[0]) == len && memcmp(rte_pktmbuf_mtod(m[0], void *), frame, len) == 0,
	      "punted frame must be the original frame with the header stripped (%u vs %u)", rte_pktmbuf_pkt_len(m[0]), len);
	CHECK(m[0]->ol_flags & punt_flag, "punt flag missing");
	CHECK(meta_of(m[0])->reason == RSN_MISS && meta_of(m[0])->lane == 0 && meta_of(m[0])->idx == 0xffffffff,
	      "meta reason %u lane %u idx %u", meta_of(m[0])->reason, meta_of(m[0])->lane, meta_of(m[0])->idx);
	CHECK(m[0]->port == PA0, "mbuf port");
	rte_pktmbuf_free(m[0]);

	/* FIN on an offloaded flow: punted with the hit entry */
	CHECK(create(PA0, &t, &n, &err) != NULL, "create: %s", err.message);
	t.flags = RTE_TCP_FIN_FLAG | RTE_TCP_ACK_FLAG;
	len = build(frame, &t, 0, 64);
	inject(PA0, frame, len);
	CHECK(rte_eth_rx_burst(PA0, 0, m, 4) == 1, "FIN not punted");
	CHECK(meta_of(m[0])->reason == RSN_FINRST && (meta_of(m[0])->flags & PUNT_F_HIT) && meta_of(m[0])->idx < 128,
	      "FIN meta reason %u flags %#x idx %u", meta_of(m[0])->reason, meta_of(m[0])->flags, meta_of(m[0])->idx);
	rte_pktmbuf_free(m[0]);
	uint8_t got[9600];
	CHECK(wire_take(PA1, got, NULL) == 0, "FIN must not be forwarded");

	/* VLAN mismatch: tagged ingress, untagged egress next hop is not possible to express; tagged frame
	 * matching an untagged flow is a miss (key includes the VID) */
	t.flags = 0;
	t.vid = 7;
	len = build(frame, &t, 0, 64);
	inject(PA0, frame, len);
	CHECK(rte_eth_rx_burst(PA0, 0, m, 4) == 1 && meta_of(m[0])->reason == RSN_MISS, "tagged frame should miss");
	rte_pktmbuf_free(m[0]);
	rte_flow_flush(PA0, &err);
}

static void test_count(void)
{
	struct tuple t = { .sip = 10, .dip = 20, .sport = 30, .dport = 40, .tcp = 0, .vid = -1 };
	struct nat n = snat_to(PA1, 0x0b000001, 6000);
	struct rte_flow_action q[] = { { .type = RTE_FLOW_ACTION_TYPE_COUNT }, { .type = RTE_FLOW_ACTION_TYPE_END } };
	struct rte_flow_query_count c = {0};
	struct rte_flow_error err;
	uint8_t frame[1600];
	uint64_t bytes = 0;
	struct rte_flow *f = create(PA0, &t, &n, &err);

	CHECK(f, "create: %s", err.message);
	for (int i = 0; i < 25; i++) {
		uint16_t len = build(frame, &t, (uint16_t)(i * 13), 64);
		bytes += len;
		inject(PA0, frame, len);
	}
	drain();
	CHECK(rte_flow_query(PA0, f, q, &c, &err) == 0, "query: %s", err.message);
	CHECK(c.hits_set && c.bytes_set && c.hits == 25 && c.bytes == bytes, "count %" PRIu64 "/%" PRIu64, c.hits, c.bytes);
	c.reset = 1;
	rte_flow_query(PA0, f, q, &c, &err);
	c.reset = 0;
	inject(PA0, frame, build(frame, &t, 0, 64));
	drain();
	rte_flow_query(PA0, f, q, &c, &err);
	CHECK(c.hits == 1, "count after reset %" PRIu64, c.hits);
	rte_flow_flush(PA0, &err);
}

static int aged_events;

static int aged_cb(uint16_t port __rte_unused, enum rte_eth_event_type ev __rte_unused, void *a __rte_unused,
		   void *r __rte_unused)
{
	aged_events++;
	return 0;
}

static void test_age(void)
{
	struct tuple t1 = { .sip = 100, .dip = 200, .sport = 300, .dport = 400, .tcp = 0, .vid = -1 };
	struct tuple t2 = t1;
	struct nat n = snat_to(PA1, 0x0c000001, 7000);
	struct rte_flow_action q[] = { { .type = RTE_FLOW_ACTION_TYPE_AGE }, { .type = RTE_FLOW_ACTION_TYPE_END } };
	struct rte_flow_query_age qa;
	struct rte_flow_error err;
	void *ctx[4];
	uint8_t frame[256];
	int cookie1, cookie2;

	t2.sport = 301;
	rte_eth_dev_callback_register(PA0, RTE_ETH_EVENT_FLOW_AGED, aged_cb, NULL);
	n.age = 2;
	n.age_ctx = &cookie1;
	struct rte_flow *f1 = create(PA0, &t1, &n, &err);
	n.age = 5;
	n.age_ctx = &cookie2;
	struct rte_flow *f2 = create(PA0, &t2, &n, &err);
	CHECK(f1 && f2, "create: %s", err.message);

	rte_pmd_natgw_model_advance(PA0, 1500);
	rte_pmd_natgw_model_poll(PA0);
	CHECK(rte_flow_get_aged_flows(PA0, NULL, 0, &err) == 0, "nothing aged at 1.5 s");
	/* traffic on flow 2 keeps it alive */
	inject(PA0, frame, build(frame, &t2, 0, 64));
	drain();
	rte_pmd_natgw_model_advance(PA0, 600);
	rte_pmd_natgw_model_poll(PA0);
	CHECK(rte_flow_get_aged_flows(PA0, ctx, 4, &err) == 1 && ctx[0] == &cookie1, "flow 1 aged at 2.1 s");
	CHECK(aged_events == 1, "FLOW_AGED events %d", aged_events);
	CHECK(rte_flow_query(PA0, f1, q, &qa, &err) == 0 && qa.aged && qa.sec_since_last_hit_valid &&
	      qa.sec_since_last_hit == 2, "age query aged %u since %u", qa.aged, qa.sec_since_last_hit);
	/* flow 2: idle past the hardware threshold (2 s) but not its own 5 s */
	rte_pmd_natgw_model_advance(PA0, 2500);
	rte_pmd_natgw_model_poll(PA0);
	CHECK(rte_flow_get_aged_flows(PA0, NULL, 0, &err) == 1, "flow 2 must not age before 5 s");
	rte_pmd_natgw_model_advance(PA0, 2600);
	rte_pmd_natgw_model_poll(PA0);
	CHECK(rte_flow_get_aged_flows(PA0, ctx, 4, &err) == 2, "flow 2 aged after 5 s");
	CHECK(rte_flow_destroy(PA0, f1, &err) == 0 && rte_flow_destroy(PA0, f2, &err) == 0, "destroy");
	CHECK(rte_flow_get_aged_flows(PA0, NULL, 0, &err) == 0, "destroyed flows leave the aged list");
	rte_eth_dev_callback_unregister(PA0, RTE_ETH_EVENT_FLOW_AGED, aged_cb, NULL);
}

static void test_destroy_and_duplicates(void)
{
	struct tuple t = { .sip = 7, .dip = 8, .sport = 9, .dport = 10, .tcp = 1, .vid = -1 };
	struct nat n = snat_to(PA1, 0x0d000001, 8000);
	struct rte_flow_error err;
	struct rte_mbuf *m[4];
	uint8_t frame[256], got[9600];
	struct rte_flow *f = create(PA0, &t, &n, &err);

	CHECK(f, "create: %s", err.message);
	CHECK(create(PA0, &t, &n, &err) == NULL && rte_errno == EEXIST, "duplicate match must fail with EEXIST");
	CHECK(rte_flow_destroy(PA0, f, &err) == 0, "destroy");
	inject(PA0, frame, build(frame, &t, 0, 64));
	CHECK(wire_take(PA1, got, NULL) == 0, "destroyed flow still forwards");
	CHECK(rte_eth_rx_burst(PA0, 0, m, 4) == 1 && meta_of(m[0])->reason == RSN_MISS, "destroyed flow should miss");
	rte_pktmbuf_free(m[0]);
	CHECK(create(PA0, &t, &n, &err) != NULL, "recreate after destroy");
	rte_flow_flush(PA0, &err);
}

static void test_fill_table(void)
{
	struct rte_flow_error err;
	struct tuple t[200];
	unsigned created = 0;
	uint8_t frame[256], got[9600];

	for (unsigned i = 0; i < RTE_DIM(t); i++) {
		t[i] = (struct tuple){ .sip = 0x0a000000 + i * 7919, .dip = 0x08080808 ^ (i * 104729), .sport = (uint16_t)(1000 + i),
				       .dport = 443, .tcp = (int)(i & 1), .vid = -1 };
		struct nat n = snat_to(PA1 + (i % 2), 0xc6336401, (uint16_t)(30000 + i));
		if (!create(PA0, &t[i], &n, &err)) {
			CHECK(rte_errno == ENOSPC, "full table: errno %d (%s)", rte_errno, err.message);
			break;
		}
		created++;
	}
	CHECK(created >= 110 && created <= 128 && created < RTE_DIM(t), "created %u of 128 slots", created);
	/* every created flow still forwards correctly after all the relocations */
	unsigned ok = 0;
	for (unsigned i = 0; i < created; i++) {
		int fwd = 0;
		uint16_t len = build(frame, &t[i], 0, 64);
		inject(PA0, frame, len);
		int nn = wire_take(PA1 + (i % 2), got, &fwd);
		struct rte_ipv4_hdr *ip = ip_of(got);
		if (nn == len && fwd && rte_be_to_cpu_16(((struct rte_tcp_hdr *)(ip + 1))->src_port) == 30000 + i &&
		    ip_csum_ok(ip) && l4_csum_ok(ip))
			ok++;
	}
	CHECK(ok == created, "%u of %u flows forwarded correctly", ok, created);
	CHECK(rte_flow_flush(PA0, &err) == 0, "flush");
	struct rte_flow_error e2;
	struct nat n = snat_to(PA1, 1, 1);
	CHECK(create(PA0, &t[0], &n, &e2) != NULL, "create after flush");
	rte_flow_flush(PA0, &err);
	drain();
}

static void test_host_tx(void)
{
	struct tuple t = { .sip = 1, .dip = 2, .sport = 3, .dport = 4, .tcp = 0, .vid = -1 };
	uint8_t frame[256], got[9600];
	uint16_t len = build(frame, &t, 50, 64);
	struct rte_mbuf *m = rte_pktmbuf_alloc(mp);
	int fwd = 1;

	memcpy(rte_pktmbuf_append(m, len), frame, len);
	CHECK(rte_eth_tx_burst(PA2, 0, &m, 1) == 1, "tx");
	CHECK(wire_take(PA2, got, &fwd) == len && !fwd && memcmp(got, frame, len) == 0, "host frame on wire");
}

static void test_no_punt_header(void)
{
	struct tuple t = { .sip = 1, .dip = 2, .sport = 3, .dport = 4, .tcp = 0, .vid = -1 };
	uint8_t frame[256];
	struct rte_mbuf *m[4];
	uint16_t len = build(frame, &t, 10, 64);

	inject(PB0, frame, len);
	CHECK(rte_eth_rx_burst(PB0, 0, m, 4) == 1, "punt");
	CHECK(rte_pktmbuf_pkt_len(m[0]) == len && !(m[0]->ol_flags & punt_flag), "no header, no metadata");
	rte_pktmbuf_free(m[0]);
}

/* ------------------------------------------------------------------ */
/* DDR tier */

static uint64_t xstat(uint16_t port, const char *name)
{
	uint64_t id, v = ~0ull;

	if (rte_eth_xstats_get_id_by_name(port, name, &id) == 0)
		rte_eth_xstats_get_by_id(port, &id, &v, 1);
	return v;
}

static struct tuple ddr_tuple(unsigned i)
{
	return (struct tuple){ .sip = 0x0a100000 + i * 7919, .dip = 0x09090909 ^ (i * 104729),
			       .sport = (uint16_t)(2000 + i), .dport = 443, .tcp = (int)(i & 1), .vid = -1 };
}

/* create flows on <port> until the device refuses one; returns how many */
static unsigned fill(uint16_t port, uint16_t egress, struct tuple *t, unsigned max)
{
	struct rte_flow_error err;
	unsigned created = 0;

	for (unsigned i = 0; i < max; i++) {
		t[i] = ddr_tuple(i);
		struct nat n = snat_to(egress, 0xc6336402, (uint16_t)(40000 + i));
		if (!create(port, &t[i], &n, &err)) {
			CHECK(rte_errno == ENOSPC, "full table: errno %d (%s)", rte_errno, err.message);
			break;
		}
		created++;
	}
	return created;
}

static void test_ddr_spill(void)
{
	struct tuple t[400];
	struct rte_flow_error err;
	uint8_t frame[256], got[9600];
	unsigned created = fill(PC0, PC1, t, RTE_DIM(t)), ok = 0;
	uint64_t ddr_flows = xstat(PC0, "natgw_ddr_flows");

	/* up to 32 on chip, up to 256 in DDR */
	CHECK(created > 26 + 180 && created <= 32 + 256 && created < RTE_DIM(t), "created %u", created);
	CHECK(ddr_flows >= created - 32 && ddr_flows <= created - 26, "DDR flows %" PRIu64 " of %u", ddr_flows,
	      created);
	for (unsigned i = 0; i < created; i++) {
		int fwd = 0;
		uint16_t len = build(frame, &t[i], 0, 64);
		inject(PC0, frame, len);
		int nn = wire_take(PC1, got, &fwd);
		struct rte_ipv4_hdr *ip = ip_of(got);
		if (nn == len && fwd && rte_be_to_cpu_16(((struct rte_tcp_hdr *)(ip + 1))->src_port) == 40000 + i &&
		    ip_csum_ok(ip) && l4_csum_ok(ip))
			ok++;
	}
	CHECK(ok == created, "%u of %u flows forwarded correctly", ok, created);
	CHECK(xstat(PC0, "natgw_ddr_hits") >= ddr_flows, "DDR hits %" PRIu64, xstat(PC0, "natgw_ddr_hits"));
	CHECK(rte_flow_flush(PC0, &err) == 0 && xstat(PC0, "natgw_ddr_flows") == 0, "flush empties the DDR tier");
	/* flushed DDR entries are gone from the hardware too */
	inject(PC0, frame, build(frame, &t[created - 1], 0, 64));
	CHECK(wire_take(PC1, got, NULL) == 0, "flushed DDR flow still forwards");
	drain();
}

static void test_ddr_punt_and_count(void)
{
	struct tuple t[40];
	struct rte_flow_action q[] = { { .type = RTE_FLOW_ACTION_TYPE_COUNT }, { .type = RTE_FLOW_ACTION_TYPE_END } };
	struct rte_flow_query_count c;
	struct rte_flow_error err;
	struct rte_flow *f[40];
	struct rte_mbuf *m[4];
	uint8_t frame[256];
	unsigned hits = 0, ddr_hits = 0, tcp = 0, no_count = 0;

	for (unsigned i = 0; i < RTE_DIM(t); i++) {
		struct nat n = snat_to(PC1, 0xc6336402, (uint16_t)(40000 + i));
		t[i] = ddr_tuple(i);
		f[i] = create(PC0, &t[i], &n, &err);
		CHECK(f[i], "create %u: %s", i, err.message);
		if (!f[i])
			return;
	}
	CHECK(xstat(PC0, "natgw_ddr_flows") >= 8, "%" PRIu64 " in DDR", xstat(PC0, "natgw_ddr_flows"));
	/* a FIN on a hit is punted with the hit index; DDR flows carry the DDR flag */
	for (unsigned i = 0; i < RTE_DIM(t); i++) {
		struct tuple tf = t[i];
		if (!tf.tcp)
			continue;
		tcp++;
		tf.flags = RTE_TCP_FIN_FLAG | RTE_TCP_ACK_FLAG;
		inject(PC0, frame, build(frame, &tf, 0, 64));
		if (rte_eth_rx_burst(PC0, 0, m, 4) == 1) {
			struct punt_meta *pm = meta_of(m[0]);
			hits += pm->reason == RSN_FINRST && (pm->flags & PUNT_F_HIT);
			ddr_hits += (pm->idx & IDX_DDR) != 0;
			rte_pktmbuf_free(m[0]);
		}
	}
	CHECK(hits == tcp && ddr_hits > 0 && ddr_hits < hits, "FIN punts: %u hits of %u, %u in DDR", hits, tcp,
	      ddr_hits);
	/* DDR flows have no counters: COUNT answers without hits */
	for (unsigned i = 0; i < RTE_DIM(t); i++) {
		inject(PC0, frame, build(frame, &t[i], 0, 64));
		memset(&c, 0, sizeof(c));
		CHECK(rte_flow_query(PC0, f[i], q, &c, &err) == 0, "query: %s", err.message);
		if (!c.hits_set && !c.bytes_set)
			no_count++;
		else
			CHECK(c.hits >= 1, "on-chip flow count %" PRIu64, c.hits);
	}
	CHECK(no_count == xstat(PC0, "natgw_ddr_flows"), "%u flows without counters", no_count);
	rte_flow_flush(PC0, &err);
	drain();
}

static void test_ddr_age(void)
{
	struct tuple t[40];
	struct tuple t1 = { .sip = 0x0b0b0001, .dip = 200, .sport = 300, .dport = 400, .tcp = 0, .vid = -1 };
	struct tuple t2 = t1;
	struct nat n = snat_to(PC1, 0x0c000001, 7000);
	struct rte_flow_action q[] = { { .type = RTE_FLOW_ACTION_TYPE_AGE }, { .type = RTE_FLOW_ACTION_TYPE_END } };
	struct rte_flow_query_age qa;
	struct rte_flow_error err;
	void *ctx[4];
	uint8_t frame[256];
	int cookie1, cookie2;

	/* fill the on-chip table so the aged flows land in DDR */
	for (unsigned i = 0; i < RTE_DIM(t) && xstat(PC0, "natgw_ddr_flows") == 0; i++) {
		struct nat nn = snat_to(PC1, 0xc6336402, (uint16_t)(40000 + i));
		t[i] = ddr_tuple(i);
		CHECK(create(PC0, &t[i], &nn, &err), "filler %u", i);
	}
	uint64_t base = xstat(PC0, "natgw_ddr_flows");
	t2.sport = 301;
	aged_events = 0;
	rte_eth_dev_callback_register(PC0, RTE_ETH_EVENT_FLOW_AGED, aged_cb, NULL);
	n.age = 2;
	n.age_ctx = &cookie1;
	struct rte_flow *f1 = create(PC0, &t1, &n, &err);
	n.age = 5;
	n.age_ctx = &cookie2;
	struct rte_flow *f2 = create(PC0, &t2, &n, &err);
	CHECK(f1 && f2, "create: %s", err.message);
	CHECK(xstat(PC0, "natgw_ddr_flows") == base + 2, "aged flows in DDR (%" PRIu64 ")", xstat(PC0, "natgw_ddr_flows"));

	rte_pmd_natgw_model_advance(PC0, 1500);
	rte_pmd_natgw_model_poll(PC0);
	CHECK(rte_flow_get_aged_flows(PC0, NULL, 0, &err) == 0, "nothing aged at 1.5 s");
	inject(PC0, frame, build(frame, &t2, 0, 64));
	drain();
	rte_pmd_natgw_model_advance(PC0, 600);
	rte_pmd_natgw_model_poll(PC0);
	CHECK(rte_flow_get_aged_flows(PC0, ctx, 4, &err) == 1 && ctx[0] == &cookie1, "flow 1 aged at 2.1 s");
	CHECK(aged_events == 1, "FLOW_AGED events %d", aged_events);
	CHECK(rte_flow_query(PC0, f1, q, &qa, &err) == 0 && qa.aged && qa.sec_since_last_hit_valid &&
	      qa.sec_since_last_hit == 2, "age query aged %u since %u", qa.aged, qa.sec_since_last_hit);
	CHECK(rte_flow_query(PC0, f2, q, &qa, &err) == 0 && !qa.aged && qa.sec_since_last_hit == 0,
	      "flow 2 seen in the last sweep: since %u", qa.sec_since_last_hit);
	/* flow 2 last seen by the sweep at 2.1 s */
	rte_pmd_natgw_model_advance(PC0, 2500);
	rte_pmd_natgw_model_poll(PC0);
	CHECK(rte_flow_get_aged_flows(PC0, NULL, 0, &err) == 1, "flow 2 must not age before 5 s idle");
	rte_pmd_natgw_model_advance(PC0, 2600);
	rte_pmd_natgw_model_poll(PC0);
	CHECK(rte_flow_get_aged_flows(PC0, ctx, 4, &err) == 2, "flow 2 aged after 5 s idle");
	rte_eth_dev_callback_unregister(PC0, RTE_ETH_EVENT_FLOW_AGED, aged_cb, NULL);
	rte_flow_flush(PC0, &err);
	drain();
}

/* a DDR tier the host must not use: on-chip capacity only, ENOSPC beyond it */
static void check_no_ddr_use(uint16_t port)
{
	struct tuple t[64];
	struct rte_flow_error err;
	unsigned created = fill(port, (uint16_t)(port + 1), t, RTE_DIM(t));

	CHECK(created >= 26 && created <= 32, "created %u (on-chip table of 32)", created);
	CHECK(xstat(port, "natgw_ddr_flows") == 0, "DDR flows %" PRIu64, xstat(port, "natgw_ddr_flows"));
	CHECK(xstat(port, "natgw_ddr_lookups") == 0, "DDR lookups");
	rte_flow_flush(port, &err);
	drain();
}

static void test_ddr_no_dimm(void)
{
	check_no_ddr_use(PD0);
}

static void test_ddr_disabled(void)
{
	check_no_ddr_use(PE0);
}

/* ------------------------------------------------------------------ */
/* tier policy (dev F: 32 on chip, high 16, low 8; dev G: fill) */

static void step(uint16_t port, uint32_t ms)
{
	rte_pmd_natgw_model_advance(port, ms);
	rte_pmd_natgw_model_poll(port);
}

/* one frame of flow i of ddr_tuple() through dev F; true when forwarded */
static bool send_f(unsigned i)
{
	struct tuple t = ddr_tuple(i);
	uint8_t frame[256], got[9600];
	int fwd = 0;
	uint16_t len = build(frame, &t, 0, 64);

	inject(PF0, frame, len);
	bool ok = wire_take(PF1, got, &fwd) == len && fwd;
	drain();
	return ok;
}

static struct rte_flow *create_f(unsigned i, struct rte_flow_error *err)
{
	struct tuple t = ddr_tuple(i);
	struct nat n = snat_to(PF1, 0xc6336403, (uint16_t)(50000 + i));
	return create(PF0, &t, &n, err);
}

static void test_policy_admission(void)
{
	struct rte_flow_error err;

	for (unsigned i = 0; i < 24; i++)
		CHECK(create_f(i, &err), "create %u: %s", i, err.message);
	CHECK(xstat(PF0, "natgw_onchip_load_pct") == 50 && xstat(PF0, "natgw_ddr_flows") == 8,
	      "on chip %" PRIu64 "%%, DDR %" PRIu64, xstat(PF0, "natgw_onchip_load_pct"), xstat(PF0, "natgw_ddr_flows"));
	rte_flow_flush(PF0, &err);
	/* bulk (priority 1) goes to DDR even with the chip empty */
	create_priority = 1;
	struct rte_flow *b = create_f(100, &err);
	create_priority = 0;
	CHECK(b && xstat(PF0, "natgw_ddr_flows") == 1, "bulk flow in DDR");
	CHECK(send_f(100), "bulk flow forwards");
	rte_flow_flush(PF0, &err);
	drain();
}

static void test_policy_moves(void)
{
	struct rte_flow_action q[] = { { .type = RTE_FLOW_ACTION_TYPE_COUNT }, { .type = RTE_FLOW_ACTION_TYPE_END } };
	struct rte_flow_query_count c;
	struct rte_flow_error err;
	struct rte_flow *hot[4];
	uint8_t frame[256];
	uint64_t fwd, punt, diff;
	unsigned lost = 0;

	/* 16 cold flows fill the chip to its high-water mark; 4 hot ones land in DDR */
	for (unsigned i = 0; i < 16; i++)
		CHECK(create_f(i, &err), "cold %u", i);
	for (unsigned i = 0; i < 4; i++)
		CHECK((hot[i] = create_f(16 + i, &err)) != NULL, "hot %u", i);
	CHECK(xstat(PF0, "natgw_ddr_flows") == 4, "hot flows in DDR");
	/* every table command from here on: hot flow 0 must forward, unchanged */
	struct tuple tp = ddr_tuple(16);
	rte_pmd_natgw_model_probe_set(PF0, frame, build(frame, &tp, 0, 64));

	step(PF0, 1500);
	for (int r = 0; r < 6; r++) {
		for (unsigned i = 0; i < 4; i++)
			lost += !send_f(16 + i);
		step(PF0, 600);
	}
	CHECK(lost == 0, "%u hot frames not forwarded", lost);
	CHECK(xstat(PF0, "natgw_promotions") == 4, "promotions %" PRIu64, xstat(PF0, "natgw_promotions"));
	CHECK(xstat(PF0, "natgw_demotions") >= 8, "demotions %" PRIu64, xstat(PF0, "natgw_demotions"));
	CHECK(xstat(PF0, "natgw_migrate_failures") == 0, "migrate failures");
	for (unsigned i = 0; i < 4; i++) {
		memset(&c, 0, sizeof(c));
		CHECK(rte_flow_query(PF0, hot[i], q, &c, &err) == 0 && c.hits_set, "hot flow %u not on chip", i);
	}
	rte_pmd_natgw_model_probe_get(PF0, &fwd, &punt, &diff);
	CHECK(fwd > 20 && punt == 0 && diff == 0, "probe: %" PRIu64 " forwarded, %" PRIu64 " punted, %" PRIu64
	      " rewritten differently", fwd, punt, diff);
	rte_pmd_natgw_model_probe_set(PF0, NULL, 0);

	/* settled: the hot flows stay on chip, no ping-pong */
	uint64_t prom = xstat(PF0, "natgw_promotions");
	for (int r = 0; r < 6; r++) {
		for (unsigned i = 0; i < 4; i++)
			lost += !send_f(16 + i);
		step(PF0, 600);
	}
	CHECK(lost == 0 && xstat(PF0, "natgw_promotions") == prom, "promotions %" PRIu64 " -> %" PRIu64,
	      prom, xstat(PF0, "natgw_promotions"));
	rte_flow_flush(PF0, &err);
	drain();
}

static void test_policy_counters(void)
{
	struct rte_flow_action q[] = { { .type = RTE_FLOW_ACTION_TYPE_COUNT }, { .type = RTE_FLOW_ACTION_TYPE_END } };
	struct rte_flow_query_count c;
	struct rte_flow_error err;
	struct rte_flow *x;

	/* 8 busy fillers (the low-water mark) and flow x, which goes idle */
	for (unsigned i = 0; i < 8; i++)
		CHECK(create_f(i, &err), "filler %u", i);
	x = create_f(40, &err);
	CHECK(x, "create x");
	for (int k = 0; k < 5; k++)
		CHECK(send_f(40), "x forwards");
	memset(&c, 0, sizeof(c));
	CHECK(rte_flow_query(PF0, x, q, &c, &err) == 0 && c.hits_set && c.hits == 5, "x count %" PRIu64, c.hits);
	for (int r = 0; r < 6; r++) {
		for (unsigned i = 0; i < 8; i++)
			send_f(i);
		step(PF0, 500);
	}
	memset(&c, 0, sizeof(c));
	CHECK(rte_flow_query(PF0, x, q, &c, &err) == 0 && !c.hits_set, "x was not demoted");
	/* busy again: promoted, and its count carries on from 5 */
	for (int r = 0; r < 4; r++) {
		CHECK(send_f(40), "x forwards in DDR / after promotion");
		step(PF0, 600);
	}
	memset(&c, 0, sizeof(c));
	CHECK(rte_flow_query(PF0, x, q, &c, &err) == 0 && c.hits_set && c.hits >= 5,
	      "x after promotion: set %u hits %" PRIu64, c.hits_set, c.hits);
	rte_flow_flush(PF0, &err);
	drain();
}

static void test_policy_fill(void)
{
	struct tuple t[64];
	struct rte_flow_error err;
	unsigned created = fill(PG0, PG0 + 1, t, RTE_DIM(t));

	/* fill ignores the 50% mark: the chip fills first */
	CHECK(created > 32 && xstat(PG0, "natgw_onchip_load_pct") >= 80, "created %u, on chip %" PRIu64 "%%",
	      created, xstat(PG0, "natgw_onchip_load_pct"));
	rte_flow_flush(PG0, &err);
	drain();
}

/* a worker polls the datapath while the main core creates and destroys flows */
static volatile int stop_worker;
static volatile uint64_t worker_frames;

static int worker(void *arg __rte_unused)
{
	struct rte_mbuf *m[32];
	uint8_t buf[9600];
	while (!stop_worker) {
		for (uint16_t p = PA0; p <= PA2; p++) {
			uint16_t n = rte_eth_rx_burst(p, 0, m, 32);
			if (n)
				rte_pktmbuf_free_bulk(m, n);
			while (rte_pmd_natgw_model_wire_recv(p, buf, sizeof(buf), NULL) > 0)
				worker_frames++;
		}
	}
	return 0;
}

static void test_concurrent(void)
{
	unsigned lc = rte_get_next_lcore(-1, 1, 0);
	struct rte_flow_error err;
	uint8_t frame[256];

	if (lc >= RTE_MAX_LCORE) {
		printf("  (skipped: needs two lcores)\n");
		return;
	}
	stop_worker = 0;
	rte_eal_remote_launch(worker, NULL, lc);
	for (int round = 0; round < 200; round++) {
		struct tuple t = { .sip = (uint32_t)round, .dip = 99, .sport = 1, .dport = 2, .tcp = 0, .vid = -1 };
		struct nat n = snat_to(PA1, 0x0e000001, (uint16_t)round);
		struct rte_flow *f = create(PA0, &t, &n, &err);
		CHECK(f != NULL, "create in round %d: %s", round, err.message);
		for (int k = 0; k < 5; k++)
			rte_pmd_natgw_model_wire_inject(PA0, frame, build(frame, &t, 0, 64));
		if (f && (round % 3))
			rte_flow_destroy(PA0, f, &err);
	}
	for (int i = 0; i < 1000 && worker_frames < 200 * 5 / 3; i++)
		rte_delay_ms(1);
	stop_worker = 1;
	rte_eal_wait_lcore(lc);
	CHECK(worker_frames > 0, "worker forwarded nothing");
	rte_flow_flush(PA0, &err);
	drain();
}

/* ------------------------------------------------------------------ */

static void run(const char *name, void (*fn)(void))
{
	test_name = name;
	test_failed = 0;
	drain();
	fn();
	printf("%s %s\n", test_failed ? "FAIL" : "PASS", name);
	failures += test_failed;
}

int main(int argc, char **argv)
{
	struct rte_eth_conf conf = {0};
	int ret = rte_eal_init(argc, argv);

	if (ret < 0) {
		fprintf(stderr, "EAL init failed\n");
		return 100;
	}
	mp = rte_pktmbuf_pool_create("test_mp", 8191, 256, 0, RTE_MBUF_DEFAULT_BUF_SIZE, (int)rte_socket_id());
	if (!mp)
		return 101;
	for (uint16_t p = 0; p < rte_eth_dev_count_avail(); p++) {
		if (rte_eth_dev_configure(p, 1, 1, &conf) || rte_eth_rx_queue_setup(p, 0, 512, 0, NULL, mp) ||
		    rte_eth_tx_queue_setup(p, 0, 512, 0, NULL) || rte_eth_dev_start(p))
			return 102;
	}
	punt_off = rte_mbuf_dynfield_lookup("natgw_dynfield_punt", NULL);
	int bit = rte_mbuf_dynflag_lookup("natgw_dynflag_punt", NULL);
	if (punt_off < 0 || bit < 0)
		return 103;
	punt_flag = RTE_BIT64(bit);

	run("ports", test_ports);
	run("validate_rejects", test_validate_rejects);
	run("snat_dnat_forwarding", test_snat_dnat_forwarding);
	run("udp_zero_checksum", test_udp_zero_checksum);
	run("punts", test_punts);
	run("count", test_count);
	run("age", test_age);
	run("destroy_and_duplicates", test_destroy_and_duplicates);
	run("fill_table", test_fill_table);
	run("host_tx", test_host_tx);
	run("no_punt_header", test_no_punt_header);
	run("concurrent", test_concurrent);
	run("ddr_spill", test_ddr_spill);
	run("ddr_punt_and_count", test_ddr_punt_and_count);
	run("ddr_age", test_ddr_age);
	run("ddr_no_dimm", test_ddr_no_dimm);
	run("ddr_disabled", test_ddr_disabled);
	run("policy_admission", test_policy_admission);
	run("policy_moves", test_policy_moves);
	run("policy_counters", test_policy_counters);
	run("policy_fill", test_policy_fill);

	uint16_t pid;
	RTE_ETH_FOREACH_DEV(pid) {
		rte_eth_dev_stop(pid);
		rte_eth_dev_close(pid);
	}
	rte_eal_cleanup();
	printf("%d failed\n", failures);
	return failures;
}
