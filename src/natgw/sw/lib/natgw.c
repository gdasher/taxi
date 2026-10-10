/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * libnatgw: layouts, hashes, device access, host cuckoo table, next hops.
 * Layouts follow rtl/natgw_pkg.sv (LSB-first fields of the packed structs).
 */

#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include "natgw.h"

/* ------------------------------------------------------------------ */
/* bit packing over arrays of 32-bit words, LSB first */

static void put_bits(uint32_t *w, unsigned pos, unsigned width, uint64_t v)
{
	for (unsigned i = 0; i < width; i++, pos++) {
		uint32_t bit = (uint32_t)((v >> i) & 1u);
		w[pos / 32] = (w[pos / 32] & ~(1u << (pos % 32))) | (bit << (pos % 32));
	}
}

static uint64_t get_bits(const uint32_t *w, unsigned pos, unsigned width)
{
	uint64_t v = 0;
	for (unsigned i = 0; i < width; i++, pos++)
		v |= (uint64_t)((w[pos / 32] >> (pos % 32)) & 1u) << i;
	return v;
}

static uint64_t mac_to_u64(const uint8_t m[6])
{
	uint64_t v = 0;
	for (int i = 0; i < 6; i++)
		v = (v << 8) | m[i];
	return v;
}

static void u64_to_mac(uint64_t v, uint8_t m[6])
{
	for (int i = 5; i >= 0; i--) {
		m[i] = (uint8_t)v;
		v >>= 8;
	}
}

/* key: lane[2:0] vid[14:3] tcp[15] sip[47:16] dip[79:48] sport[95:80] dport[111:96] */
static void key_put(uint32_t *w, unsigned base, const struct natgw_key *k)
{
	put_bits(w, base + 0, 3, k->lane);
	put_bits(w, base + 3, 12, k->vid);
	put_bits(w, base + 15, 1, k->tcp);
	put_bits(w, base + 16, 32, k->sip);
	put_bits(w, base + 48, 32, k->dip);
	put_bits(w, base + 80, 16, k->sport);
	put_bits(w, base + 96, 16, k->dport);
}

static void key_get(const uint32_t *w, unsigned base, struct natgw_key *k)
{
	k->lane = (uint8_t)get_bits(w, base + 0, 3);
	k->vid = (uint16_t)get_bits(w, base + 3, 12);
	k->tcp = (uint8_t)get_bits(w, base + 15, 1);
	k->sip = (uint32_t)get_bits(w, base + 16, 32);
	k->dip = (uint32_t)get_bits(w, base + 48, 32);
	k->sport = (uint16_t)get_bits(w, base + 80, 16);
	k->dport = (uint16_t)get_bits(w, base + 96, 16);
}

void natgw_entry_pack(const struct natgw_entry *e, uint32_t w[NATGW_ENTRY_WORDS])
{
	memset(w, 0, NATGW_ENTRY_WORDS * sizeof(uint32_t));
	put_bits(w, 0, 1, e->valid);
	key_put(w, 1, &e->key);
	put_bits(w, 113, 1, e->xlate_dst);
	put_bits(w, 114, 32, e->new_ip);
	put_bits(w, 146, 16, e->new_port);
	put_bits(w, 162, 1, e->dec_ttl);
	put_bits(w, 163, 10, e->nh_idx);
}

void natgw_entry_unpack(const uint32_t w[NATGW_ENTRY_WORDS], struct natgw_entry *e)
{
	memset(e, 0, sizeof(*e));
	e->valid = (uint8_t)get_bits(w, 0, 1);
	key_get(w, 1, &e->key);
	e->xlate_dst = (uint8_t)get_bits(w, 113, 1);
	e->new_ip = (uint32_t)get_bits(w, 114, 32);
	e->new_port = (uint16_t)get_bits(w, 146, 16);
	e->dec_ttl = (uint8_t)get_bits(w, 162, 1);
	e->nh_idx = (uint16_t)get_bits(w, 163, 10);
}

void natgw_state_pack(const struct natgw_state *s, uint32_t w[NATGW_STATE_WORDS])
{
	memset(w, 0, NATGW_STATE_WORDS * sizeof(uint32_t));
	put_bits(w, 0, 1, s->valid);
	put_bits(w, 1, 1, s->fin);
	put_bits(w, 2, 1, s->rst);
	put_bits(w, 3, 1, s->evp);
	put_bits(w, 4, 1, s->tcp);
	put_bits(w, 8, 32, s->ts);
	put_bits(w, 40, 48, s->pkts);
	put_bits(w, 88, 56, s->bytes);
}

void natgw_state_unpack(const uint32_t w[NATGW_STATE_WORDS], struct natgw_state *s)
{
	memset(s, 0, sizeof(*s));
	s->valid = (uint8_t)get_bits(w, 0, 1);
	s->fin = (uint8_t)get_bits(w, 1, 1);
	s->rst = (uint8_t)get_bits(w, 2, 1);
	s->evp = (uint8_t)get_bits(w, 3, 1);
	s->tcp = (uint8_t)get_bits(w, 4, 1);
	s->ts = (uint32_t)get_bits(w, 8, 32);
	s->pkts = get_bits(w, 40, 48);
	s->bytes = get_bits(w, 88, 56);
}

void natgw_nh_pack(const struct natgw_nh *nh, uint32_t w[NATGW_NH_WORDS])
{
	memset(w, 0, NATGW_NH_WORDS * sizeof(uint32_t));
	put_bits(w, 0, 1, nh->valid);
	put_bits(w, 1, 48, mac_to_u64(nh->dst_mac));
	put_bits(w, 49, 48, mac_to_u64(nh->src_mac));
	put_bits(w, 97, 3, nh->lane);
	put_bits(w, 100, 1, nh->vlan);
	put_bits(w, 101, 12, nh->vid);
}

void natgw_nh_unpack(const uint32_t w[NATGW_NH_WORDS], struct natgw_nh *nh)
{
	memset(nh, 0, sizeof(*nh));
	nh->valid = (uint8_t)get_bits(w, 0, 1);
	u64_to_mac(get_bits(w, 1, 48), nh->dst_mac);
	u64_to_mac(get_bits(w, 49, 48), nh->src_mac);
	nh->lane = (uint8_t)get_bits(w, 97, 3);
	nh->vlan = (uint8_t)get_bits(w, 100, 1);
	nh->vid = (uint16_t)get_bits(w, 101, 12);
}

uint32_t natgw_key_crc(const struct natgw_key *k, uint32_t seed, uint32_t poly)
{
	uint32_t w[4] = {0};
	uint32_t crc = seed;

	key_put(w, 0, k);
	for (unsigned i = 0; i < 112; i++) {
		uint32_t b = (w[i / 32] >> (i % 32)) & 1u;
		if ((crc ^ b) & 1u)
			crc = (crc >> 1) ^ poly;
		else
			crc >>= 1;
	}
	return crc;
}

bool natgw_key_eq(const struct natgw_key *a, const struct natgw_key *b)
{
	return a->lane == b->lane && a->vid == b->vid && a->tcp == b->tcp &&
	       a->sip == b->sip && a->dip == b->dip &&
	       a->sport == b->sport && a->dport == b->dport;
}

int natgw_punt_parse(const uint8_t *b, size_t len, struct natgw_punt *p)
{
	if (len < NATGW_PUNT_HDR_LEN)
		return -EINVAL;
	if (((b[0] << 8) | b[1]) != NATGW_PUNT_MAGIC || b[2] != NATGW_PUNT_VER)
		return -EINVAL;
	p->reason = b[3];
	p->lane = b[4];
	p->flags = b[5];
	p->tci = (uint16_t)((b[6] << 8) | b[7]);
	p->hash = ((uint32_t)b[8] << 24) | ((uint32_t)b[9] << 16) | ((uint32_t)b[10] << 8) | b[11];
	p->idx = ((uint32_t)b[12] << 24) | ((uint32_t)b[13] << 16) | ((uint32_t)b[14] << 8) | b[15];
	return 0;
}

/* ------------------------------------------------------------------ */
/* device */

static inline uint32_t rd(struct natgw_dev *d, uint32_t off)
{
	return d->io.rd(d->io.ctx, off);
}

static inline void wr(struct natgw_dev *d, uint32_t off, uint32_t v)
{
	d->io.wr(d->io.ctx, off, v);
}

int natgw_dev_init(struct natgw_dev *d, const struct natgw_io *io)
{
	uint32_t caps;

	memset(d, 0, sizeof(*d));
	d->io = *io;
	if (rd(d, NATGW_REG_ID) != NATGW_ID)
		return -ENODEV;
	d->version = rd(d, NATGW_REG_VERSION);
	caps = rd(d, NATGW_REG_CAPS);
	d->idx_w = caps & 0xff;
	d->lanes = (caps >> 8) & 0xff;
	d->punt_hdr_len = (caps >> 16) & 0xff;
	if (d->idx_w < 4 || d->idx_w > 24 || d->lanes == 0 || d->lanes > NATGW_LANES)
		return -EIO;
	d->bucket_w = d->idx_w - 3;
	return 0;
}

void natgw_dev_flush(struct natgw_dev *d)
{
	(void)rd(d, NATGW_REG_ID);
}

int natgw_dev_clear(struct natgw_dev *d, unsigned max_polls)
{
	wr(d, NATGW_REG_CLEAR, 1);
	for (unsigned i = 0; i < max_polls; i++) {
		if (!(rd(d, NATGW_REG_CLEAR) & 1))
			return 0;
	}
	return -ETIMEDOUT;
}

void natgw_dev_set_ctrl(struct natgw_dev *d, bool enable, bool punt_hdr, uint8_t bypass_mask, uint8_t egress_en)
{
	wr(d, NATGW_REG_CTRL, (enable ? NATGW_CTRL_ENABLE : 0) | (punt_hdr ? NATGW_CTRL_PUNT_HDR : 0) |
	   ((uint32_t)bypass_mask << 8) | ((uint32_t)egress_en << 16));
	natgw_dev_flush(d);
}

void natgw_dev_set_seeds(struct natgw_dev *d, uint32_t seed0, uint32_t seed1)
{
	wr(d, NATGW_REG_SEED0, seed0);
	wr(d, NATGW_REG_SEED1, seed1);
	natgw_dev_flush(d);
}

void natgw_dev_set_tick_div(struct natgw_dev *d, uint32_t cycles)
{
	wr(d, NATGW_REG_TICK_DIV, cycles);
	natgw_dev_flush(d);
}

void natgw_dev_set_thresholds(struct natgw_dev *d, uint32_t tcp_ticks, uint32_t udp_ticks)
{
	wr(d, NATGW_REG_THRESH_TCP, tcp_ticks);
	wr(d, NATGW_REG_THRESH_UDP, udp_ticks);
	natgw_dev_flush(d);
}

uint32_t natgw_dev_tick(struct natgw_dev *d)
{
	return rd(d, NATGW_REG_TICK);
}

static void write_entry_nf(struct natgw_dev *d, uint32_t idx, const struct natgw_entry *e)
{
	uint32_t w[NATGW_ENTRY_WORDS];

	natgw_entry_pack(e, w);
	for (unsigned k = 0; k < NATGW_ENTRY_WORDS; k++)
		wr(d, NATGW_REG_ENT_DATA + 4 * k, w[k]);
	wr(d, NATGW_REG_INDEX, idx);
	wr(d, NATGW_REG_CMD, NATGW_CMD_WR_ENT);
}

static void clear_entry_nf(struct natgw_dev *d, uint32_t idx)
{
	wr(d, NATGW_REG_INDEX, idx);
	wr(d, NATGW_REG_CMD, NATGW_CMD_CLR);
}

void natgw_dev_write_entry(struct natgw_dev *d, uint32_t idx, const struct natgw_entry *e)
{
	write_entry_nf(d, idx, e);
	natgw_dev_flush(d);
}

void natgw_dev_clear_entry(struct natgw_dev *d, uint32_t idx)
{
	clear_entry_nf(d, idx);
	natgw_dev_flush(d);
}

void natgw_dev_read_entry(struct natgw_dev *d, uint32_t idx, struct natgw_entry *e)
{
	uint32_t w[NATGW_ENTRY_WORDS];

	wr(d, NATGW_REG_INDEX, idx);
	wr(d, NATGW_REG_CMD, NATGW_CMD_RD_ENT);
	for (unsigned k = 0; k < NATGW_ENTRY_WORDS; k++)
		w[k] = rd(d, NATGW_REG_ENT_DATA + 4 * k);
	natgw_entry_unpack(w, e);
}

void natgw_dev_read_state(struct natgw_dev *d, uint32_t idx, struct natgw_state *s)
{
	uint32_t w[NATGW_STATE_WORDS];

	wr(d, NATGW_REG_INDEX, idx);
	wr(d, NATGW_REG_CMD, NATGW_CMD_RD_ST);
	for (unsigned k = 0; k < NATGW_STATE_WORDS; k++)
		w[k] = rd(d, NATGW_REG_ST_DATA + 4 * k);
	natgw_state_unpack(w, s);
}

void natgw_dev_write_nh(struct natgw_dev *d, uint16_t idx, const struct natgw_nh *nh)
{
	uint32_t w[NATGW_NH_WORDS];

	natgw_nh_pack(nh, w);
	for (unsigned k = 0; k < NATGW_NH_WORDS; k++)
		wr(d, NATGW_REG_NH_DATA + 4 * k, w[k]);
	wr(d, NATGW_REG_NH_INDEX, idx);
	wr(d, NATGW_REG_NH_CMD, 1);
	natgw_dev_flush(d);
}

void natgw_dev_read_nh(struct natgw_dev *d, uint16_t idx, struct natgw_nh *nh)
{
	uint32_t w[NATGW_NH_WORDS];

	wr(d, NATGW_REG_NH_INDEX, idx);
	wr(d, NATGW_REG_NH_CMD, 2);
	for (unsigned k = 0; k < NATGW_NH_WORDS; k++)
		w[k] = rd(d, NATGW_REG_NH_DATA + 4 * k);
	natgw_nh_unpack(w, nh);
}

int natgw_dev_pop_event(struct natgw_dev *d, struct natgw_event *ev)
{
	uint32_t lo, hi;

	if (!(rd(d, NATGW_REG_EVT_STATUS) & 1))
		return 0;
	lo = rd(d, NATGW_REG_EVT_LO);
	hi = rd(d, NATGW_REG_EVT_HI);
	ev->type = (uint8_t)(hi >> 28);
	ev->idx = hi & 0xffffff;
	ev->tick = lo;
	return 1;
}

uint32_t natgw_dev_event_drops(struct natgw_dev *d)
{
	return rd(d, NATGW_REG_EVT_DROPS);
}

uint64_t natgw_dev_read_stat(struct natgw_dev *d, unsigned lane, unsigned n)
{
	uint32_t a = NATGW_REG_STATS + lane * 0x100 + n * 8;
	uint32_t lo = rd(d, a);
	uint32_t hi = rd(d, a + 4);

	return ((uint64_t)hi << 32) | lo;
}

void natgw_dev_apply(struct natgw_dev *d, const struct natgw_write *ops, unsigned n)
{
	for (unsigned i = 0; i < n; i++) {
		if (ops[i].clear)
			clear_entry_nf(d, ops[i].idx);
		else
			write_entry_nf(d, ops[i].idx, &ops[i].entry);
	}
	natgw_dev_flush(d);
}

/* ------------------------------------------------------------------ */
/* DDR tier */

void natgw_dev_ddr_status(struct natgw_dev *d, struct natgw_ddr_status *st)
{
	uint32_t v = rd(d, NATGW_REG_DDR_STATUS);

	st->present = v & NATGW_DDR_PRESENT;
	st->calibrated = v & NATGW_DDR_CALIBRATED;
	st->enabled = v & NATGW_DDR_ENABLED;
	st->clearing = v & NATGW_DDR_CLEARING;
	st->active = v & NATGW_DDR_ACTIVE;
	st->bucket_w = (v >> 8) & 0xff;
	st->max_out = (v >> 16) & 0xff;
}

int natgw_dev_ddr_clear(struct natgw_dev *d, unsigned max_polls)
{
	uint32_t en = rd(d, NATGW_REG_DDR_CTRL) & 1;

	wr(d, NATGW_REG_DDR_CTRL, en | 2);
	for (unsigned i = 0; i < max_polls; i++) {
		if (!(rd(d, NATGW_REG_DDR_STATUS) & NATGW_DDR_CLEARING))
			return 0;
	}
	return -ETIMEDOUT;
}

void natgw_dev_ddr_enable(struct natgw_dev *d, bool enable)
{
	wr(d, NATGW_REG_DDR_CTRL, enable ? 1 : 0);
	natgw_dev_flush(d);
}

static void write_ddr_entry_nf(struct natgw_dev *d, uint32_t idx, const struct natgw_entry *e)
{
	uint32_t w[NATGW_ENTRY_WORDS];

	natgw_entry_pack(e, w);
	for (unsigned k = 0; k < NATGW_ENTRY_WORDS; k++)
		wr(d, NATGW_REG_ENT_DATA + 4 * k, w[k]);
	wr(d, NATGW_REG_INDEX, idx);
	wr(d, NATGW_REG_CMD, NATGW_CMD_DDR_WR);
}

static void clear_ddr_entry_nf(struct natgw_dev *d, uint32_t idx)
{
	wr(d, NATGW_REG_INDEX, idx);
	wr(d, NATGW_REG_CMD, NATGW_CMD_DDR_CLR);
}

void natgw_dev_write_ddr_entry(struct natgw_dev *d, uint32_t idx, const struct natgw_entry *e)
{
	write_ddr_entry_nf(d, idx, e);
	natgw_dev_flush(d);
}

void natgw_dev_clear_ddr_entry(struct natgw_dev *d, uint32_t idx)
{
	clear_ddr_entry_nf(d, idx);
	natgw_dev_flush(d);
}

void natgw_dev_read_ddr_entry(struct natgw_dev *d, uint32_t idx, struct natgw_entry *e)
{
	uint32_t w[NATGW_ENTRY_WORDS];

	wr(d, NATGW_REG_INDEX, idx);
	wr(d, NATGW_REG_CMD, NATGW_CMD_DDR_RD);
	for (unsigned k = 0; k < NATGW_ENTRY_WORDS; k++)
		w[k] = rd(d, NATGW_REG_ENT_DATA + 4 * k);
	natgw_entry_unpack(w, e);
}

uint64_t natgw_dev_read_activity(struct natgw_dev *d, uint32_t word)
{
	uint32_t lo, hi;

	wr(d, NATGW_REG_INDEX, word);
	wr(d, NATGW_REG_CMD, NATGW_CMD_ACT_RC);
	lo = rd(d, NATGW_REG_ACT_LO);
	hi = rd(d, NATGW_REG_ACT_HI);
	return ((uint64_t)hi << 32) | lo;
}

void natgw_dev_ddr_stats(struct natgw_dev *d, uint32_t *lookups, uint32_t *hits, uint32_t *skips)
{
	*lookups = rd(d, NATGW_REG_DDR_LOOKUPS);
	*hits = rd(d, NATGW_REG_DDR_HITS);
	*skips = rd(d, NATGW_REG_DDR_SKIPS);
}

uint32_t natgw_dev_ddr_read_errors(struct natgw_dev *d)
{
	return rd(d, NATGW_REG_DDR_RERR);
}

void natgw_dev_apply_ddr(struct natgw_dev *d, const struct natgw_write *ops, unsigned n)
{
	for (unsigned i = 0; i < n; i++) {
		if (ops[i].clear)
			clear_ddr_entry_nf(d, ops[i].idx);
		else
			write_ddr_entry_nf(d, ops[i].idx, &ops[i].entry);
	}
	natgw_dev_flush(d);
}

/* ------------------------------------------------------------------ */
/* host cuckoo table: two tables of 2^bucket_w buckets of 2^slot_bits slots.
 * UltraRAM tier: 4 slots, buckets from the low hash bits.
 * DDR tier: 2 slots (one 64-byte line), buckets from the top hash bits. */

#define MAX_SLOTS 4
#define NO_PREV 0xffffffffu

struct natgw_table {
	unsigned bucket_w;
	unsigned slot_bits;
	unsigned slots;
	bool top;                     /* buckets from the top hash bits */
	uint32_t buckets;
	uint32_t size;
	uint32_t seed0, seed1;
	unsigned max_depth;
	uint32_t count;
	struct natgw_entry *slot;     /* slot[i].valid marks occupancy */
	/* BFS scratch */
	uint32_t *prev;               /* NO_PREV when unvisited */
	uint32_t *touched;
	uint32_t *queue;
	uint8_t *qdepth;
};

static inline uint32_t mk_idx(const struct natgw_table *t, unsigned tbl, uint32_t bucket, unsigned slot)
{
	return ((uint32_t)tbl << (t->bucket_w + t->slot_bits)) | (bucket << t->slot_bits) | slot;
}

static inline unsigned idx_tbl(const struct natgw_table *t, uint32_t idx)
{
	return idx >> (t->bucket_w + t->slot_bits);
}

static uint32_t bucket_of(const struct natgw_table *t, const struct natgw_key *k, unsigned tbl)
{
	uint32_t h = tbl ? natgw_key_crc(k, t->seed1, NATGW_POLY_CRC32) : natgw_key_crc(k, t->seed0, NATGW_POLY_CRC32C);
	return t->top ? h >> (32 - t->bucket_w) : h & (t->buckets - 1);
}

/* the 2 * slots candidate indices of a key: table 0's bucket, then table 1's */
static unsigned candidates(const struct natgw_table *t, const struct natgw_key *k, uint32_t c[2 * MAX_SLOTS])
{
	uint32_t b0 = bucket_of(t, k, 0);
	uint32_t b1 = bucket_of(t, k, 1);

	for (unsigned s = 0; s < t->slots; s++) {
		c[s] = mk_idx(t, 0, b0, s);
		c[t->slots + s] = mk_idx(t, 1, b1, s);
	}
	return 2 * t->slots;
}

static struct natgw_table *table_create(unsigned bucket_w, unsigned slot_bits, bool top,
					uint32_t seed0, uint32_t seed1, unsigned max_depth)
{
	struct natgw_table *t;

	if (max_depth < 1 || max_depth > 16)
		return NULL;
	t = calloc(1, sizeof(*t));
	if (!t)
		return NULL;
	t->bucket_w = bucket_w;
	t->slot_bits = slot_bits;
	t->slots = 1u << slot_bits;
	t->top = top;
	t->buckets = 1u << bucket_w;
	t->size = 2 * t->buckets * t->slots;
	t->seed0 = seed0;
	t->seed1 = seed1;
	t->max_depth = max_depth;
	t->slot = calloc(t->size, sizeof(*t->slot));
	t->prev = malloc(t->size * sizeof(*t->prev));
	t->touched = malloc(t->size * sizeof(*t->touched));
	t->queue = malloc(t->size * sizeof(*t->queue));
	t->qdepth = malloc(t->size * sizeof(*t->qdepth));
	if (!t->slot || !t->prev || !t->touched || !t->queue || !t->qdepth) {
		natgw_table_destroy(t);
		return NULL;
	}
	for (uint32_t i = 0; i < t->size; i++)
		t->prev[i] = NO_PREV;
	return t;
}

struct natgw_table *natgw_table_create(unsigned bucket_w, uint32_t seed0, uint32_t seed1, unsigned max_depth)
{
	if (bucket_w < 1 || bucket_w > 21)
		return NULL;
	return table_create(bucket_w, 2, false, seed0, seed1, max_depth);
}

struct natgw_table *natgw_table_create_ddr(unsigned bucket_w, uint32_t seed0, uint32_t seed1, unsigned max_depth)
{
	/* at least two activity words (128 entries); 2^23 buckets = 32M entries */
	if (bucket_w < 5 || bucket_w > 23)
		return NULL;
	return table_create(bucket_w, 1, true, seed0, seed1, max_depth);
}

void natgw_table_destroy(struct natgw_table *t)
{
	if (!t)
		return;
	free(t->slot);
	free(t->prev);
	free(t->touched);
	free(t->queue);
	free(t->qdepth);
	free(t);
}

unsigned natgw_table_size(const struct natgw_table *t)
{
	return t->size;
}

unsigned natgw_table_count(const struct natgw_table *t)
{
	return t->count;
}

void natgw_table_clear(struct natgw_table *t)
{
	memset(t->slot, 0, t->size * sizeof(*t->slot));
	t->count = 0;
}

const struct natgw_entry *natgw_table_slot(const struct natgw_table *t, uint32_t idx)
{
	if (idx >= t->size || !t->slot[idx].valid)
		return NULL;
	return &t->slot[idx];
}

int natgw_table_lookup(const struct natgw_table *t, const struct natgw_key *k, uint32_t *idx)
{
	uint32_t c[2 * MAX_SLOTS];
	unsigned nc = candidates(t, k, c);

	for (unsigned i = 0; i < nc; i++) {
		if (t->slot[c[i]].valid && natgw_key_eq(&t->slot[c[i]].key, k)) {
			if (idx)
				*idx = c[i];
			return 0;
		}
	}
	return -ENOENT;
}

int natgw_table_find(const struct natgw_table *t, const struct natgw_key *k, uint32_t *idx)
{
	return natgw_table_lookup(t, k, idx);
}

static int key_ok(const struct natgw_table *t, const struct natgw_entry *e)
{
	(void)t;
	return e->key.lane < NATGW_LANES && e->key.vid < 4096 && e->key.tcp <= 1 &&
	       e->xlate_dst <= 1 && e->dec_ttl <= 1 && e->nh_idx < NATGW_NH_COUNT;
}

int natgw_table_insert(struct natgw_table *t, const struct natgw_entry *e,
		       struct natgw_write *ops, unsigned max_ops, uint32_t *idx_out)
{
	uint32_t c[2 * MAX_SLOTS];
	unsigned nc;
	uint32_t found = NO_PREV;
	unsigned qh = 0, qt = 0, nt = 0;
	struct natgw_entry ne = *e;
	int ret;

	if (!key_ok(t, e))
		return -EINVAL;
	ne.valid = 1;

	/* in-place update */
	if (natgw_table_find(t, &e->key, &c[0]) == 0) {
		if (max_ops < 1)
			return -E2BIG;
		t->slot[c[0]] = ne;
		ops[0].idx = c[0];
		ops[0].clear = false;
		ops[0].entry = ne;
		if (idx_out)
			*idx_out = c[0];
		return 1;
	}

	nc = candidates(t, &e->key, c);
	for (unsigned i = 0; i < nc; i++) {
		if (!t->slot[c[i]].valid) {
			if (max_ops < 1)
				return -E2BIG;
			t->slot[c[i]] = ne;
			t->count++;
			ops[0].idx = c[i];
			ops[0].clear = false;
			ops[0].entry = ne;
			if (idx_out)
				*idx_out = c[i];
			return 1;
		}
	}

	/* breadth-first search for a relocation path ending in a free slot */
	for (unsigned i = 0; i < nc; i++) {
		if (t->prev[c[i]] != NO_PREV)
			continue;
		t->prev[c[i]] = c[i];         /* root marker: points at itself */
		t->touched[nt++] = c[i];
		t->queue[qt] = c[i];
		t->qdepth[qt++] = 1;
	}
	while (qh < qt && found == NO_PREV) {
		uint32_t i = t->queue[qh];
		unsigned depth = t->qdepth[qh++];
		const struct natgw_entry *occ = &t->slot[i];
		unsigned alt = 1 - idx_tbl(t, i);
		uint32_t ab = bucket_of(t, &occ->key, alt);

		for (unsigned s = 0; s < t->slots; s++) {
			uint32_t j = mk_idx(t, alt, ab, s);
			if (t->prev[j] != NO_PREV)
				continue;
			t->prev[j] = i;
			t->touched[nt++] = j;
			if (!t->slot[j].valid) {
				found = j;
				break;
			}
			if (depth < t->max_depth) {
				t->queue[qt] = j;
				t->qdepth[qt++] = (uint8_t)(depth + 1);
			}
		}
	}

	if (found == NO_PREV) {
		ret = -ENOSPC;
	} else {
		/* path: found <- p1 <- ... <- root (a candidate of the new key) */
		unsigned len = 1;
		for (uint32_t j = found; t->prev[j] != j; j = t->prev[j])
			len++;
		if (len > max_ops) {
			ret = -E2BIG;
		} else {
			unsigned n = 0;
			uint32_t dst = found;
			while (t->prev[dst] != dst) {
				uint32_t src = t->prev[dst];
				t->slot[dst] = t->slot[src];
				ops[n].idx = dst;
				ops[n].clear = false;
				ops[n].entry = t->slot[dst];
				n++;
				dst = src;
			}
			t->slot[dst] = ne;
			ops[n].idx = dst;
			ops[n].clear = false;
			ops[n].entry = ne;
			n++;
			t->count++;
			if (idx_out)
				*idx_out = dst;
			ret = (int)n;
		}
	}

	for (unsigned i = 0; i < nt; i++)
		t->prev[t->touched[i]] = NO_PREV;
	return ret;
}

int natgw_table_delete(struct natgw_table *t, const struct natgw_key *k,
		       struct natgw_write *ops, unsigned max_ops)
{
	uint32_t idx;

	if (natgw_table_find(t, k, &idx) != 0)
		return -ENOENT;
	if (max_ops < 1)
		return -E2BIG;
	memset(&t->slot[idx], 0, sizeof(t->slot[idx]));
	t->count--;
	ops[0].idx = idx;
	ops[0].clear = true;
	memset(&ops[0].entry, 0, sizeof(ops[0].entry));
	return 1;
}

/* ------------------------------------------------------------------ */
/* next hops */

void natgw_nh_table_init(struct natgw_nh_table *nt)
{
	memset(nt, 0, sizeof(*nt));
}

static bool nh_eq(const struct natgw_nh *a, const struct natgw_nh *b)
{
	return memcmp(a->dst_mac, b->dst_mac, 6) == 0 && memcmp(a->src_mac, b->src_mac, 6) == 0 &&
	       a->lane == b->lane && a->vlan == b->vlan && (!a->vlan || a->vid == b->vid);
}

int natgw_nh_get(struct natgw_nh_table *nt, const struct natgw_nh *nh, bool *is_new)
{
	int free_idx = -1;

	for (int i = 1; i < NATGW_NH_COUNT; i++) {
		if (nt->refs[i] && nh_eq(&nt->nh[i], nh)) {
			nt->refs[i]++;
			*is_new = false;
			return i;
		}
		if (!nt->refs[i] && free_idx < 0)
			free_idx = i;
	}
	if (free_idx < 0)
		return -ENOSPC;
	nt->nh[free_idx] = *nh;
	nt->nh[free_idx].valid = 1;
	if (!nt->nh[free_idx].vlan)
		nt->nh[free_idx].vid = 0;
	nt->refs[free_idx] = 1;
	*is_new = true;
	return free_idx;
}

int natgw_nh_put(struct natgw_nh_table *nt, uint16_t idx)
{
	if (idx == 0 || idx >= NATGW_NH_COUNT || nt->refs[idx] == 0)
		return -EINVAL;
	return (int)--nt->refs[idx];
}
