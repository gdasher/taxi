/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * Software model of the natgw shim. Behaviour mirrors tb/natgw_model.py
 * (ShimModel, parse, rewrite) and the register semantics of
 * rtl/natgw_regs.sv; per-entry state and events mirror rtl/natgw_state.sv.
 */

#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include "natgw_model.h"

#define EVT_DEPTH 1024
#define DDR_MAX_OUT 16

struct natgw_model {
	unsigned bucket_w, idx_w;
	uint32_t size;

	/* configuration registers (reset values as in natgw_regs.sv) */
	uint8_t  enable, punt_hdr, bypass, egress_en;
	uint32_t scratch, seed0, seed1, tick_div, tick;
	uint32_t thresh_tcp, thresh_udp;
	uint8_t  scan_en;
	uint16_t scan_interval, bubble;

	/* indirect access registers */
	uint32_t ent_data[NATGW_ENTRY_WORDS];
	uint32_t st_data[NATGW_STATE_WORDS];
	uint32_t index;
	uint32_t nh_index;
	uint32_t nh_data[NATGW_NH_WORDS];

	/* tables */
	struct natgw_entry *ent;
	struct natgw_state *st;
	struct natgw_nh nh[NATGW_NH_COUNT];

	/* events */
	uint64_t evt[EVT_DEPTH];
	unsigned evt_head, evt_count;
	uint32_t evt_drops;
	uint8_t  ovf_pending;

	/* statistics */
	uint64_t stats[NATGW_LANES][NATGW_STAT_COUNT];
	uint32_t stat_hi_shadow;

	/* DDR tier (ddr_present) */
	uint8_t  ddr_present, ddr_calib, ddr_en;
	unsigned ddr_bucket_w;
	uint32_t ddr_size;              /* entries */
	struct natgw_entry *ddr_ent;
	uint64_t *ddr_act;              /* activity bitmap, ddr_size / 64 words */
	uint32_t act_data[2];
	uint32_t ddr_lookups, ddr_hits;
};

static void model_reset_regs(struct natgw_model *m)
{
	m->enable = 0;
	m->punt_hdr = 0;
	m->bypass = 0xff;
	m->egress_en = 0xff;
	m->scratch = 0;
	m->seed0 = 0xffffffff;
	m->seed1 = 0xffffffff;
	m->tick_div = 250000;
	m->tick = 0;
	m->thresh_tcp = 7440000;
	m->thresh_udp = 300000;
	m->scan_en = 1;
	m->scan_interval = 5;
	m->bubble = 16;
}

static void model_clear_tables(struct natgw_model *m)
{
	memset(m->ent, 0, m->size * sizeof(*m->ent));
	memset(m->st, 0, m->size * sizeof(*m->st));
	memset(m->nh, 0, sizeof(m->nh));
}

struct natgw_model *natgw_model_create(unsigned bucket_w)
{
	struct natgw_model *m;

	if (bucket_w < 1 || bucket_w > 21)
		return NULL;
	m = calloc(1, sizeof(*m));
	if (!m)
		return NULL;
	m->bucket_w = bucket_w;
	m->idx_w = bucket_w + 3;
	m->size = 1u << m->idx_w;
	m->ent = calloc(m->size, sizeof(*m->ent));
	m->st = calloc(m->size, sizeof(*m->st));
	if (!m->ent || !m->st) {
		natgw_model_destroy(m);
		return NULL;
	}
	model_reset_regs(m);
	return m;
}

void natgw_model_destroy(struct natgw_model *m)
{
	if (!m)
		return;
	free(m->ent);
	free(m->st);
	free(m->ddr_ent);
	free(m->ddr_act);
	free(m);
}

/* SplitMix64: fills the DDR table with junk, as after power-up */
static uint64_t junk_next(uint64_t *x)
{
	uint64_t z = (*x += 0x9e3779b97f4a7c15ull);
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ull;
	z = (z ^ (z >> 27)) * 0x94d049bb133111ebull;
	return z ^ (z >> 31);
}

int natgw_model_set_ddr(struct natgw_model *m, unsigned ddr_bucket_w, bool calibrated)
{
	uint64_t x = 0x5eed;

	if (ddr_bucket_w < 5 || ddr_bucket_w > 23 || m->ddr_present)
		return -EINVAL;
	m->ddr_size = 4u << ddr_bucket_w;
	m->ddr_ent = calloc(m->ddr_size, sizeof(*m->ddr_ent));
	m->ddr_act = calloc(m->ddr_size / 64, sizeof(*m->ddr_act));
	if (!m->ddr_ent || !m->ddr_act) {
		free(m->ddr_ent);
		free(m->ddr_act);
		m->ddr_ent = NULL;
		m->ddr_act = NULL;
		return -ENOMEM;
	}
	for (uint32_t i = 0; i < m->ddr_size; i++) {
		uint32_t w[NATGW_ENTRY_WORDS];
		for (unsigned k = 0; k < NATGW_ENTRY_WORDS; k++)
			w[k] = (uint32_t)junk_next(&x);
		natgw_entry_unpack(w, &m->ddr_ent[i]);
	}
	for (uint32_t i = 0; i < m->ddr_size / 64; i++)
		m->ddr_act[i] = junk_next(&x);
	m->ddr_present = 1;
	m->ddr_calib = calibrated;
	m->ddr_bucket_w = ddr_bucket_w;
	return 0;
}

static int ddr_active(const struct natgw_model *m)
{
	return m->ddr_present && m->ddr_calib && m->ddr_en;
}

static void ddr_clear(struct natgw_model *m)
{
	if (!m->ddr_calib)
		return;     /* the clear engine's writes never complete; modelled as no-op */
	memset(m->ddr_ent, 0, m->ddr_size * sizeof(*m->ddr_ent));
	memset(m->ddr_act, 0, m->ddr_size / 64 * sizeof(*m->ddr_act));
}

/* ------------------------------------------------------------------ */
/* events */

static int evt_push(struct natgw_model *m, uint8_t type, uint32_t idx)
{
	if (m->evt_count >= EVT_DEPTH)
		return -1;
	m->evt[(m->evt_head + m->evt_count) % EVT_DEPTH] =
		((uint64_t)((uint32_t)type << 28 | (idx & 0xffffff)) << 32) | m->tick;
	m->evt_count++;
	return 0;
}

static void evt_push_finrst(struct natgw_model *m, uint8_t type, uint32_t idx)
{
	if (evt_push(m, type, idx) < 0) {
		m->evt_drops++;
		m->ovf_pending = 1;
	}
}

static void evt_pop(struct natgw_model *m)
{
	if (!m->evt_count)
		return;
	m->evt_head = (m->evt_head + 1) % EVT_DEPTH;
	m->evt_count--;
	if (m->ovf_pending && evt_push(m, NATGW_EVT_OVF, 0) == 0)
		m->ovf_pending = 0;
}

/* ------------------------------------------------------------------ */
/* registers */

uint32_t natgw_model_rd(struct natgw_model *m, uint32_t off)
{
	off &= 0xfffc;

	if (off >= NATGW_REG_STATS && off < NATGW_REG_STATS + 0x800) {
		unsigned lane = (off >> 8) & 7, n = (off >> 3) & 0x1f;
		uint64_t v = n < NATGW_STAT_COUNT ? m->stats[lane][n] : 0;
		if (off & 4)
			return m->stat_hi_shadow;
		m->stat_hi_shadow = (uint32_t)(v >> 32);
		return (uint32_t)v;
	}
	if (off >= NATGW_REG_ENT_DATA && off < NATGW_REG_ENT_DATA + 4 * NATGW_ENTRY_WORDS)
		return m->ent_data[(off - NATGW_REG_ENT_DATA) / 4];
	if (off >= NATGW_REG_ST_DATA && off < NATGW_REG_ST_DATA + 4 * NATGW_STATE_WORDS)
		return m->st_data[(off - NATGW_REG_ST_DATA) / 4];
	if (off >= NATGW_REG_NH_DATA && off < NATGW_REG_NH_DATA + 4 * NATGW_NH_WORDS)
		return m->nh_data[(off - NATGW_REG_NH_DATA) / 4];

	switch (off) {
	case NATGW_REG_ID: return NATGW_ID;
	case NATGW_REG_VERSION: return 0x00010100;
	case NATGW_REG_CAPS: return (16u << 16) | ((uint32_t)NATGW_LANES << 8) | m->idx_w;
	case NATGW_REG_SCRATCH: return m->scratch;
	case NATGW_REG_CTRL:
		return ((uint32_t)m->egress_en << 16) | ((uint32_t)m->bypass << 8) |
		       ((uint32_t)m->punt_hdr << 1) | m->enable;
	case NATGW_REG_CLEAR: return 0;
	case NATGW_REG_SEED0: return m->seed0;
	case NATGW_REG_SEED1: return m->seed1;
	case NATGW_REG_TICK_DIV: return m->tick_div;
	case NATGW_REG_TICK: return m->tick;
	case NATGW_REG_THRESH_TCP: return m->thresh_tcp;
	case NATGW_REG_THRESH_UDP: return m->thresh_udp;
	case NATGW_REG_SCAN: return ((uint32_t)m->scan_en << 31) | m->scan_interval;
	case NATGW_REG_BUBBLE: return m->bubble;
	case NATGW_REG_INDEX: return m->index;
	case NATGW_REG_CMD: return 0;
	case NATGW_REG_NH_INDEX: return m->nh_index;
	case NATGW_REG_NH_CMD: return 0;
	case NATGW_REG_EVT_STATUS: return m->evt_count ? 1 : 0;
	case NATGW_REG_EVT_LO: return m->evt_count ? (uint32_t)m->evt[m->evt_head] : 0;
	case NATGW_REG_EVT_HI: {
		uint32_t v = m->evt_count ? (uint32_t)(m->evt[m->evt_head] >> 32) : 0;
		evt_pop(m);
		return v;
	}
	case NATGW_REG_EVT_DROPS: return m->evt_drops;
	case NATGW_REG_DDR_STATUS:
		if (!m->ddr_present)
			return 0;
		return ((uint32_t)DDR_MAX_OUT << 16) | ((uint32_t)m->ddr_bucket_w << 8) |
		       (ddr_active(m) ? NATGW_DDR_ACTIVE : 0) | (m->ddr_en ? NATGW_DDR_ENABLED : 0) |
		       (m->ddr_calib ? NATGW_DDR_CALIBRATED : 0) | NATGW_DDR_PRESENT;
	case NATGW_REG_DDR_CTRL: return m->ddr_en;
	case NATGW_REG_DDR_LOOKUPS: return m->ddr_lookups;
	case NATGW_REG_DDR_HITS: return m->ddr_hits;
	case NATGW_REG_DDR_SKIPS: return 0;     /* no request cap in the model */
	case NATGW_REG_DDR_RERR: return 0;      /* no read errors in the model */
	case NATGW_REG_ACT_LO: return m->act_data[0];
	case NATGW_REG_ACT_HI: return m->act_data[1];
	default: return 0;
	}
}

static void model_cmd(struct natgw_model *m, uint32_t cmd)
{
	uint32_t idx = m->index & (m->size - 1);
	uint32_t didx = m->index & (m->ddr_size - 1);
	struct natgw_entry e;

	cmd &= 0xf;
	if (cmd >= NATGW_CMD_DDR_WR && cmd <= NATGW_CMD_ACT_RC && !(m->ddr_present && m->ddr_calib))
		return;     /* ignored without a DDR tier; without a DIMM, never completes */
	switch (cmd) {
	case NATGW_CMD_WR_ENT:
		natgw_entry_unpack(m->ent_data, &e);
		m->ent[idx] = e;
		memset(&m->st[idx], 0, sizeof(m->st[idx]));
		m->st[idx].valid = e.valid;
		m->st[idx].tcp = e.key.tcp;
		m->st[idx].ts = m->tick;
		break;
	case NATGW_CMD_WR_ST:
		natgw_state_unpack(m->st_data, &m->st[idx]);
		break;
	case NATGW_CMD_RD_ENT:
		natgw_entry_pack(&m->ent[idx], m->ent_data);
		break;
	case NATGW_CMD_RD_ST:
		natgw_state_pack(&m->st[idx], m->st_data);
		break;
	case NATGW_CMD_CLR:
		memset(&m->ent[idx], 0, sizeof(m->ent[idx]));
		memset(&m->st[idx], 0, sizeof(m->st[idx]));
		break;
	case NATGW_CMD_DDR_WR:
		natgw_entry_unpack(m->ent_data, &m->ddr_ent[didx]);
		break;
	case NATGW_CMD_DDR_CLR:
		memset(&m->ddr_ent[didx], 0, sizeof(m->ddr_ent[didx]));
		break;
	case NATGW_CMD_DDR_RD:
		natgw_entry_pack(&m->ddr_ent[didx], m->ent_data);
		break;
	case NATGW_CMD_ACT_RC: {
		uint32_t w = m->index & (m->ddr_size / 64 - 1);
		m->act_data[0] = (uint32_t)m->ddr_act[w];
		m->act_data[1] = (uint32_t)(m->ddr_act[w] >> 32);
		m->ddr_act[w] = 0;
		break;
	}
	default:
		break;
	}
}

void natgw_model_wr(struct natgw_model *m, uint32_t off, uint32_t v)
{
	off &= 0xfffc;

	if (off >= NATGW_REG_ENT_DATA && off < NATGW_REG_ENT_DATA + 4 * NATGW_ENTRY_WORDS) {
		m->ent_data[(off - NATGW_REG_ENT_DATA) / 4] = v;
		return;
	}
	if (off >= NATGW_REG_ST_DATA && off < NATGW_REG_ST_DATA + 4 * NATGW_STATE_WORDS) {
		m->st_data[(off - NATGW_REG_ST_DATA) / 4] = v;
		return;
	}
	if (off >= NATGW_REG_NH_DATA && off < NATGW_REG_NH_DATA + 4 * NATGW_NH_WORDS) {
		m->nh_data[(off - NATGW_REG_NH_DATA) / 4] = v;
		return;
	}

	switch (off) {
	case NATGW_REG_SCRATCH: m->scratch = v; break;
	case NATGW_REG_CTRL:
		m->enable = v & 1;
		m->punt_hdr = (v >> 1) & 1;
		m->bypass = (uint8_t)(v >> 8);
		m->egress_en = (uint8_t)(v >> 16);
		break;
	case NATGW_REG_CLEAR:
		if (v & 1)
			model_clear_tables(m);
		break;
	case NATGW_REG_SEED0: m->seed0 = v; break;
	case NATGW_REG_SEED1: m->seed1 = v; break;
	case NATGW_REG_TICK_DIV: m->tick_div = v; break;
	case NATGW_REG_THRESH_TCP: m->thresh_tcp = v; break;
	case NATGW_REG_THRESH_UDP: m->thresh_udp = v; break;
	case NATGW_REG_SCAN:
		m->scan_en = (uint8_t)(v >> 31);
		m->scan_interval = (uint16_t)v;
		break;
	case NATGW_REG_BUBBLE: m->bubble = (uint16_t)v; break;
	case NATGW_REG_DDR_CTRL:
		m->ddr_en = v & 1;
		if ((v & 2) && m->ddr_present)
			ddr_clear(m);
		break;
	case NATGW_REG_INDEX: m->index = v; break;
	case NATGW_REG_CMD: model_cmd(m, v); break;
	case NATGW_REG_NH_INDEX: m->nh_index = v & (NATGW_NH_COUNT - 1); break;
	case NATGW_REG_NH_CMD:
		if ((v & 3) == 1)
			natgw_nh_unpack(m->nh_data, &m->nh[m->nh_index]);
		else if ((v & 3) == 2)
			natgw_nh_pack(&m->nh[m->nh_index], m->nh_data);
		break;
	default:
		break;
	}
}

static uint32_t io_rd(void *ctx, uint32_t off)
{
	return natgw_model_rd(ctx, off);
}

static void io_wr(void *ctx, uint32_t off, uint32_t v)
{
	natgw_model_wr(ctx, off, v);
}

struct natgw_io natgw_model_io(struct natgw_model *m)
{
	struct natgw_io io = { io_rd, io_wr, m };
	return io;
}

/* ------------------------------------------------------------------ */
/* frame parsing (natgw_model.parse) */

struct parsed {
	uint8_t  reason;
	uint8_t  lookup;
	uint8_t  l3ok;
	uint8_t  vlan;
	uint16_t tci;
	uint8_t  ttl;
	uint8_t  tcp;
	unsigned ip;
	uint8_t  fin, rst;
	struct natgw_key key;
};

static inline uint8_t b8(const uint8_t *f, size_t len, size_t i)
{
	return i < len ? f[i] : 0;
}

static inline uint16_t b16(const uint8_t *f, size_t len, size_t i)
{
	return (uint16_t)((b8(f, len, i) << 8) | b8(f, len, i + 1));
}

static inline uint32_t b32(const uint8_t *f, size_t len, size_t i)
{
	return ((uint32_t)b16(f, len, i) << 16) | b16(f, len, i + 2);
}

static void parse(const uint8_t *f, size_t L, unsigned lane, int bypass, struct parsed *p)
{
	uint16_t et = b16(f, L, 12), totlen, frag;
	unsigned o = 0, ip;
	uint8_t ver_ihl, proto, flags;
	uint32_t s = 0;
	int is_ipv4, hdr_ok, csum_ok;

	memset(p, 0, sizeof(*p));
	if (et == 0x8100) {
		p->vlan = 1;
		p->tci = b16(f, L, 14);
		et = b16(f, L, 16);
		o = 4;
	}
	ip = 14 + o;
	p->ip = ip;
	ver_ihl = b8(f, L, ip);
	totlen = b16(f, L, ip + 2);
	frag = b16(f, L, ip + 6);
	p->ttl = b8(f, L, ip + 8);
	proto = b8(f, L, ip + 9);
	p->tcp = proto == 6;
	flags = b8(f, L, ip + 33);

	for (unsigned k = 0; k < 20; k += 2)
		s += b16(f, L, ip + k);
	while (s >> 16)
		s = (s & 0xffff) + (s >> 16);
	csum_ok = s == 0xffff;

	is_ipv4 = et == 0x0800;
	hdr_ok = ver_ihl == 0x45 && L >= ip + 20 && totlen >= 20 && ip + totlen <= L;
	p->l3ok = is_ipv4 && hdr_ok && csum_ok;

	p->key.lane = (uint8_t)lane;
	p->key.vid = p->vlan ? (p->tci & 0xfff) : 0;
	p->key.tcp = p->tcp;
	p->key.sip = b32(f, L, ip + 12);
	p->key.dip = b32(f, L, ip + 16);
	p->key.sport = b16(f, L, ip + 20);
	p->key.dport = b16(f, L, ip + 22);

	if (bypass) p->reason = NATGW_RSN_BYPASS;
	else if (b8(f, L, 0) & 1) p->reason = NATGW_RSN_MCAST;
	else if (!is_ipv4) p->reason = NATGW_RSN_NOT_IPV4;
	else if (!hdr_ok) p->reason = NATGW_RSN_IP_HDR;
	else if (!csum_ok) p->reason = NATGW_RSN_CSUM;
	else if (frag & 0x3fff) p->reason = NATGW_RSN_FRAG;
	else if (p->ttl <= 1) p->reason = NATGW_RSN_TTL;
	else if (proto != 6 && proto != 17) p->reason = NATGW_RSN_PROTO;
	else if ((p->tcp && totlen < 40) || (!p->tcp && totlen < 28)) p->reason = NATGW_RSN_IP_HDR;
	else if (p->tcp && (flags & 0x02)) p->reason = NATGW_RSN_SYN;
	else if (p->tcp && (flags & 0x05)) {
		p->reason = NATGW_RSN_FINRST;
		p->lookup = 1;
		p->fin = flags & 1;
		p->rst = (flags >> 2) & 1;
	} else {
		p->reason = NATGW_RSN_MISS;
		p->lookup = 1;
	}
}

/* ------------------------------------------------------------------ */
/* rewrite (ShimModel.rewrite) */

static uint16_t csum_update3(uint16_t hc, uint16_t m0, uint16_t n0, uint16_t m1, uint16_t n1, uint16_t m2, uint16_t n2)
{
	uint32_t s = (uint16_t)~hc + (uint32_t)(uint16_t)~m0 + n0 + (uint16_t)~m1 + n1 + (uint16_t)~m2 + n2;
	uint32_t f = (s & 0xffff) + (s >> 16);
	f = (f & 0xffff) + (f >> 16);
	return (uint16_t)~f;
}

static inline void put16(uint8_t *f, size_t i, uint16_t v)
{
	f[i] = (uint8_t)(v >> 8);
	f[i + 1] = (uint8_t)v;
}

static inline void put32(uint8_t *f, size_t i, uint32_t v)
{
	put16(f, i, (uint16_t)(v >> 16));
	put16(f, i + 2, (uint16_t)v);
}

static void rewrite(uint8_t *f, size_t L, const struct parsed *p, const struct natgw_entry *e, const struct natgw_nh *nh)
{
	unsigned ip = p->ip;
	uint8_t ttl, proto, nttl;
	unsigned ip_field = ip + (e->xlate_dst ? 16 : 12);
	unsigned port_field = ip + (e->xlate_dst ? 22 : 20);
	unsigned l4c_off = ip + 20 + (p->tcp ? 16 : 6);
	uint32_t old_ip;
	uint16_t old_port, hc, l4c;

	(void)L;
	memcpy(f, nh->dst_mac, 6);
	memcpy(f + 6, nh->src_mac, 6);
	if (p->vlan)
		put16(f, 14, (uint16_t)((p->tci & 0xf000) | (nh->vid & 0xfff)));

	ttl = f[ip + 8];
	proto = f[ip + 9];
	nttl = e->dec_ttl ? (uint8_t)(ttl - 1) : ttl;
	old_ip = b32(f, L, ip_field);
	old_port = b16(f, L, port_field);

	hc = b16(f, L, ip + 10);
	hc = csum_update3(hc, (uint16_t)(old_ip >> 16), (uint16_t)(e->new_ip >> 16),
			  (uint16_t)old_ip, (uint16_t)e->new_ip,
			  (uint16_t)((ttl << 8) | proto), (uint16_t)((nttl << 8) | proto));

	l4c = b16(f, L, l4c_off);
	if (p->tcp || l4c != 0) {
		l4c = csum_update3(l4c, (uint16_t)(old_ip >> 16), (uint16_t)(e->new_ip >> 16),
				   (uint16_t)old_ip, (uint16_t)e->new_ip, old_port, e->new_port);
		if (!p->tcp && l4c == 0)
			l4c = 0xffff;
	}

	f[ip + 8] = nttl;
	put16(f, ip + 10, hc);
	put32(f, ip_field, e->new_ip);
	put16(f, port_field, e->new_port);
	put16(f, l4c_off, l4c);
}

/* ------------------------------------------------------------------ */
/* lookup and frame processing (ShimModel.process) */

static int model_lookup(const struct natgw_model *m, const struct natgw_key *k, uint32_t *idx)
{
	uint32_t b0 = natgw_key_crc(k, m->seed0, NATGW_POLY_CRC32C) & ((1u << m->bucket_w) - 1);
	uint32_t b1 = natgw_key_crc(k, m->seed1, NATGW_POLY_CRC32) & ((1u << m->bucket_w) - 1);

	for (unsigned t = 0; t < 2; t++) {
		for (unsigned s = 0; s < 4; s++) {
			uint32_t i = ((uint32_t)t << (m->bucket_w + 2)) | ((t ? b1 : b0) << 2) | s;
			if (m->ent[i].valid && natgw_key_eq(&m->ent[i].key, k)) {
				*idx = i;
				return 0;
			}
		}
	}
	return -ENOENT;
}

/* the DDR tier, after an on-chip miss: index {table, bucket, slot}, buckets from the top bits */
static int model_lookup_ddr(struct natgw_model *m, const struct natgw_key *k, uint32_t *idx)
{
	unsigned bw = m->ddr_bucket_w;
	uint32_t b0 = natgw_key_crc(k, m->seed0, NATGW_POLY_CRC32C) >> (32 - bw);
	uint32_t b1 = natgw_key_crc(k, m->seed1, NATGW_POLY_CRC32) >> (32 - bw);

	m->ddr_lookups++;
	for (unsigned t = 0; t < 2; t++) {
		for (unsigned s = 0; s < 2; s++) {
			uint32_t i = ((uint32_t)t << (bw + 1)) | ((t ? b1 : b0) << 1) | s;
			if (m->ddr_ent[i].valid && natgw_key_eq(&m->ddr_ent[i].key, k)) {
				m->ddr_hits++;
				m->ddr_act[i / 64] |= 1ull << (i % 64);
				*idx = i;
				return 0;
			}
		}
	}
	return -ENOENT;
}

static void count(struct natgw_model *m, unsigned lane, unsigned reason)
{
	m->stats[lane][reason]++;
}

int natgw_model_rx(struct natgw_model *m, unsigned lane, const uint8_t *frame, size_t len,
		   struct natgw_model_out *out)
{
	int bypass = !m->enable || ((m->bypass >> lane) & 1);
	struct parsed p;
	uint32_t idx = 0, h0 = 0;
	int hit = 0, ddr_hit = 0;
	const struct natgw_entry *e = NULL;
	uint8_t reason;

	if (lane >= NATGW_LANES || !out || !out->data)
		return -EINVAL;

	parse(frame, len, lane, bypass, &p);
	out->hit_idx = 0xffffffff;

	if (p.reason == NATGW_RSN_BYPASS) {
		count(m, lane, NATGW_RSN_BYPASS);
		out->kind = NATGW_OUT_PUNT;
		out->lane = (uint8_t)lane;
		out->reason = NATGW_RSN_BYPASS;
		out->len = len;
		memcpy(out->data, frame, len);
		return 0;
	}

	if (p.lookup) {
		h0 = natgw_key_crc(&p.key, m->seed0, NATGW_POLY_CRC32C);
		hit = model_lookup(m, &p.key, &idx) == 0;
		if (hit) {
			e = &m->ent[idx];
		} else if (ddr_active(m) && model_lookup_ddr(m, &p.key, &idx) == 0) {
			hit = ddr_hit = 1;
			e = &m->ddr_ent[idx];
		}
	}

	if (ddr_hit) {
		/* no per-entry state or events for the DDR tier: the activity bit only */
		idx |= NATGW_DDR_IDX_FLAG;
		out->hit_idx = idx;
	} else if (hit) {
		/* per-entry state: every looked-up hit updates it (natgw_state) */
		struct natgw_state *s = &m->st[idx];
		s->ts = m->tick;
		s->pkts++;
		s->bytes += len;
		s->evp = 0;
		s->fin |= p.fin;
		s->rst |= p.rst;
		if (p.fin)
			evt_push_finrst(m, NATGW_EVT_FIN, idx);
		else if (p.rst)
			evt_push_finrst(m, NATGW_EVT_RST, idx);
		out->hit_idx = idx;
	}

	reason = p.reason;
	if (reason == NATGW_RSN_MISS && hit) {
		const struct natgw_nh *nh = &m->nh[e->nh_idx];
		if (!nh->valid || !((m->egress_en >> nh->lane) & 1)) {
			reason = NATGW_RSN_NH;
		} else if (nh->vlan != p.vlan) {
			reason = NATGW_RSN_VLAN;
		} else {
			count(m, lane, NATGW_RSN_FWD);
			memcpy(out->data, frame, len);
			rewrite(out->data, len, &p, e, nh);
			out->kind = NATGW_OUT_FWD;
			out->lane = nh->lane;
			out->reason = NATGW_RSN_FWD;
			out->len = len;
			return 0;
		}
	}

	count(m, lane, reason);
	out->kind = NATGW_OUT_PUNT;
	out->lane = (uint8_t)lane;
	out->reason = reason;
	if (m->punt_hdr) {
		uint8_t *h = out->data;
		uint32_t hidx = hit ? idx : 0xffffffff;
		uint8_t flags = (p.l3ok ? NATGW_PUNT_F_L3OK : 0) | (p.vlan ? NATGW_PUNT_F_VLAN : 0) |
				(hit ? NATGW_PUNT_F_HIT : 0);
		put16(h, 0, NATGW_PUNT_MAGIC);
		h[2] = NATGW_PUNT_VER;
		h[3] = reason;
		h[4] = (uint8_t)lane;
		h[5] = flags;
		put16(h, 6, p.tci);
		put32(h, 8, p.lookup ? h0 : 0);
		put32(h, 12, hidx);
		memcpy(h + NATGW_PUNT_HDR_LEN, frame, len);
		out->len = len + NATGW_PUNT_HDR_LEN;
	} else {
		memcpy(out->data, frame, len);
		out->len = len;
	}
	return 0;
}

void natgw_model_advance(struct natgw_model *m, uint32_t ticks)
{
	m->tick += ticks;
	if (!m->scan_en)
		return;
	for (uint32_t i = 0; i < m->size; i++) {
		struct natgw_state *s = &m->st[i];
		uint32_t thresh = s->tcp ? m->thresh_tcp : m->thresh_udp;
		if (!s->valid || s->evp || (uint32_t)(m->tick - s->ts) <= thresh)
			continue;
		if (evt_push(m, NATGW_EVT_IDLE, i) < 0)
			break;     /* the hardware scanner pauses while the FIFO is full */
		s->evp = 1;
	}
}
