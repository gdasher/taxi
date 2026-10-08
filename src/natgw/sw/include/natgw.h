/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * libnatgw: host interface to the NAT gateway shim (natgw) on the FPGA.
 *
 * - register map and bit layouts, identical to rtl/natgw_pkg.sv and
 *   rtl/natgw_regs.sv
 * - device access over a pluggable 32-bit register interface (a PCIe BAR
 *   window in the DPDK driver, or the software model in natgw_model.h)
 * - the host copy of the cuckoo table: slot placement with relocation,
 *   so the hardware needs no insert logic
 * - next-hop table allocation with sharing
 *
 * No dynamic dependencies beyond libc; safe to link into a DPDK PMD.
 */

#ifndef NATGW_H
#define NATGW_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef NATGW_DPDK
#include <rte_compat.h>
#define NATGW_API __rte_internal
#else
#define NATGW_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define NATGW_LANES         8
#define NATGW_NH_COUNT      1024
#define NATGW_PUNT_HDR_LEN  16
#define NATGW_PUNT_MAGIC    0x4E47
#define NATGW_PUNT_VER      1
#define NATGW_ID            0x4E415447u   /* "NATG" */

/* register window offset inside BAR0 */
#define NATGW_BAR_OFFSET    0x800000u

/* registers (byte offsets within the window) */
#define NATGW_REG_ID          0x0000
#define NATGW_REG_VERSION     0x0004
#define NATGW_REG_CAPS        0x0008
#define NATGW_REG_SCRATCH     0x000C
#define NATGW_REG_CTRL        0x0010
#define NATGW_REG_CLEAR       0x0014
#define NATGW_REG_SEED0       0x0020
#define NATGW_REG_SEED1       0x0024
#define NATGW_REG_TICK_DIV    0x0028
#define NATGW_REG_TICK        0x002C
#define NATGW_REG_THRESH_TCP  0x0030
#define NATGW_REG_THRESH_UDP  0x0034
#define NATGW_REG_SCAN        0x0038
#define NATGW_REG_BUBBLE      0x003C
#define NATGW_REG_ENT_DATA    0x0100   /* 7 words */
#define NATGW_REG_ST_DATA     0x0120   /* 5 words */
#define NATGW_REG_INDEX       0x0140
#define NATGW_REG_CMD         0x0144
#define NATGW_REG_NH_INDEX    0x0200
#define NATGW_REG_NH_CMD      0x0204
#define NATGW_REG_NH_DATA     0x0210   /* 4 words */
#define NATGW_REG_EVT_STATUS  0x0300
#define NATGW_REG_EVT_LO      0x0304
#define NATGW_REG_EVT_HI      0x0308   /* read pops */
#define NATGW_REG_EVT_DROPS   0x0310
#define NATGW_REG_STATS       0x1000   /* + lane*0x100 + n*8, 64-bit, low word first */

#define NATGW_CMD_WR_ENT  1
#define NATGW_CMD_WR_ST   2
#define NATGW_CMD_RD_ENT  3
#define NATGW_CMD_RD_ST   4
#define NATGW_CMD_CLR     5

#define NATGW_CTRL_ENABLE     (1u << 0)
#define NATGW_CTRL_PUNT_HDR   (1u << 1)

/* punt reasons (punt header byte 3; statistics index) */
enum natgw_reason {
	NATGW_RSN_MISS = 0,
	NATGW_RSN_BYPASS = 1,
	NATGW_RSN_NOT_IPV4 = 2,
	NATGW_RSN_MCAST = 3,
	NATGW_RSN_IP_HDR = 4,
	NATGW_RSN_FRAG = 5,
	NATGW_RSN_TTL = 6,
	NATGW_RSN_PROTO = 7,
	NATGW_RSN_CSUM = 8,
	NATGW_RSN_SYN = 9,
	NATGW_RSN_FINRST = 10,
	NATGW_RSN_VLAN = 11,
	NATGW_RSN_NH = 12,
	NATGW_RSN_FWD = 15,
};

#define NATGW_STAT_DROP  16
#define NATGW_STAT_BAD   17
#define NATGW_STAT_COUNT 18

enum natgw_event_type {
	NATGW_EVT_IDLE = 1,
	NATGW_EVT_FIN = 2,
	NATGW_EVT_RST = 3,
	NATGW_EVT_OVF = 15,
};

/* ------------------------------------------------------------------ */
/* data types (host byte order) */

struct natgw_key {
	uint8_t  lane;      /* ingress lane */
	uint16_t vid;       /* VLAN ID, 0 when untagged */
	uint8_t  tcp;       /* 1 TCP, 0 UDP */
	uint32_t sip;
	uint32_t dip;
	uint16_t sport;
	uint16_t dport;
};

struct natgw_entry {
	struct natgw_key key;
	uint8_t  valid;
	uint8_t  xlate_dst; /* 0: rewrite source (SNAT), 1: rewrite destination (DNAT) */
	uint32_t new_ip;
	uint16_t new_port;
	uint8_t  dec_ttl;
	uint16_t nh_idx;
};

struct natgw_nh {
	uint8_t  valid;
	uint8_t  dst_mac[6];
	uint8_t  src_mac[6];
	uint8_t  lane;      /* egress lane */
	uint8_t  vlan;      /* egress frame is VLAN tagged (must match ingress) */
	uint16_t vid;
};

struct natgw_state {
	uint8_t  valid;
	uint8_t  fin;
	uint8_t  rst;
	uint8_t  evp;
	uint8_t  tcp;
	uint32_t ts;        /* last-seen tick */
	uint64_t pkts;
	uint64_t bytes;
};

struct natgw_event {
	uint8_t  type;
	uint32_t idx;
	uint32_t tick;
};

/* punt header, decoded */
struct natgw_punt {
	uint8_t  reason;
	uint8_t  lane;
	uint8_t  flags;     /* bit0 l3ok, bit2 vlan, bit3 hit */
	uint16_t tci;
	uint32_t hash;
	uint32_t idx;       /* 0xffffffff when no hit */
};

#define NATGW_PUNT_F_L3OK  0x01
#define NATGW_PUNT_F_VLAN  0x04
#define NATGW_PUNT_F_HIT   0x08

/* ------------------------------------------------------------------ */
/* bit layouts and hashes */

#define NATGW_ENTRY_WORDS 7
#define NATGW_STATE_WORDS 5
#define NATGW_NH_WORDS    4

void natgw_entry_pack(const struct natgw_entry *e, uint32_t w[NATGW_ENTRY_WORDS]);
void natgw_entry_unpack(const uint32_t w[NATGW_ENTRY_WORDS], struct natgw_entry *e);
void natgw_state_pack(const struct natgw_state *s, uint32_t w[NATGW_STATE_WORDS]);
void natgw_state_unpack(const uint32_t w[NATGW_STATE_WORDS], struct natgw_state *s);
void natgw_nh_pack(const struct natgw_nh *nh, uint32_t w[NATGW_NH_WORDS]);
void natgw_nh_unpack(const uint32_t w[NATGW_NH_WORDS], struct natgw_nh *nh);

#define NATGW_POLY_CRC32C 0x82F63B78u
#define NATGW_POLY_CRC32  0xEDB88320u

/* reflected CRC over the 112-bit key, bit 0 first, from seed, no final XOR */
uint32_t natgw_key_crc(const struct natgw_key *k, uint32_t seed, uint32_t poly);

bool natgw_key_eq(const struct natgw_key *a, const struct natgw_key *b);

/* parse a punt header from the first 16 bytes of a punted frame */
NATGW_API
int natgw_punt_parse(const uint8_t *buf, size_t len, struct natgw_punt *p);

/* ------------------------------------------------------------------ */
/* device */

struct natgw_io {
	uint32_t (*rd)(void *ctx, uint32_t off);
	void (*wr)(void *ctx, uint32_t off, uint32_t val);
	void *ctx;
};

struct natgw_dev {
	struct natgw_io io;
	uint32_t version;
	unsigned idx_w;       /* entry index width */
	unsigned bucket_w;    /* idx_w - 3 */
	unsigned lanes;
	unsigned punt_hdr_len;
};

int natgw_dev_init(struct natgw_dev *d, const struct natgw_io *io);
/* posted writes take effect after a read; every write helper below ends with one */
void natgw_dev_flush(struct natgw_dev *d);
int natgw_dev_clear(struct natgw_dev *d, unsigned max_polls);
void natgw_dev_set_ctrl(struct natgw_dev *d, bool enable, bool punt_hdr, uint8_t bypass_mask, uint8_t egress_en);
void natgw_dev_set_seeds(struct natgw_dev *d, uint32_t seed0, uint32_t seed1);
void natgw_dev_set_tick_div(struct natgw_dev *d, uint32_t cycles);
void natgw_dev_set_thresholds(struct natgw_dev *d, uint32_t tcp_ticks, uint32_t udp_ticks);
uint32_t natgw_dev_tick(struct natgw_dev *d);
void natgw_dev_write_entry(struct natgw_dev *d, uint32_t idx, const struct natgw_entry *e);
void natgw_dev_clear_entry(struct natgw_dev *d, uint32_t idx);
void natgw_dev_read_entry(struct natgw_dev *d, uint32_t idx, struct natgw_entry *e);
void natgw_dev_read_state(struct natgw_dev *d, uint32_t idx, struct natgw_state *s);
void natgw_dev_write_nh(struct natgw_dev *d, uint16_t idx, const struct natgw_nh *nh);
void natgw_dev_read_nh(struct natgw_dev *d, uint16_t idx, struct natgw_nh *nh);
/* returns 1 and fills *ev when an event was pending, 0 otherwise */
int natgw_dev_pop_event(struct natgw_dev *d, struct natgw_event *ev);
uint32_t natgw_dev_event_drops(struct natgw_dev *d);
uint64_t natgw_dev_read_stat(struct natgw_dev *d, unsigned lane, unsigned n);

/* ------------------------------------------------------------------ */
/* host copy of the cuckoo table */

struct natgw_write {
	uint32_t idx;
	bool     clear;          /* true: invalidate the slot */
	struct natgw_entry entry;
};

struct natgw_table;

struct natgw_table *natgw_table_create(unsigned bucket_w, uint32_t seed0, uint32_t seed1, unsigned max_depth);
void natgw_table_destroy(struct natgw_table *t);
unsigned natgw_table_size(const struct natgw_table *t);
unsigned natgw_table_count(const struct natgw_table *t);

/*
 * Plan an insert (or an in-place update when the key is present). On success
 * the table copy is updated and the ordered writes needed to reach the new
 * state are stored in ops[0..n-1]; n is returned and *idx_out receives the
 * entry's final slot. The writes keep every present key findable in hardware
 * at every step (a moved entry is written to its new slot before its old
 * slot is reused). Returns -ENOSPC when no slot can be freed within
 * max_depth relocations, -E2BIG when max_ops is too small, -EINVAL on a
 * bad entry; the table is unchanged on error.
 */
int natgw_table_insert(struct natgw_table *t, const struct natgw_entry *e,
		       struct natgw_write *ops, unsigned max_ops, uint32_t *idx_out);
/* plan a delete; returns the number of writes (1) or -ENOENT */
int natgw_table_delete(struct natgw_table *t, const struct natgw_key *k,
		       struct natgw_write *ops, unsigned max_ops);
/* slot of a key, or -ENOENT */
int natgw_table_find(const struct natgw_table *t, const struct natgw_key *k, uint32_t *idx);
/* entry at a slot (NULL when the slot is free) */
const struct natgw_entry *natgw_table_slot(const struct natgw_table *t, uint32_t idx);
/* the slot hardware would hit for a key (same order as hardware); -ENOENT when none */
int natgw_table_lookup(const struct natgw_table *t, const struct natgw_key *k, uint32_t *idx);
void natgw_table_clear(struct natgw_table *t);

/* write planned operations to the device, in order, then flush */
void natgw_dev_apply(struct natgw_dev *d, const struct natgw_write *ops, unsigned n);

/* largest number of writes a single insert can need */
#define NATGW_MAX_INSERT_OPS(max_depth) ((max_depth) + 2)

/* ------------------------------------------------------------------ */
/* next-hop allocation (identical next hops share one index) */

struct natgw_nh_table {
	struct natgw_nh nh[NATGW_NH_COUNT];
	uint32_t refs[NATGW_NH_COUNT];
};

void natgw_nh_table_init(struct natgw_nh_table *nt);
/* index of an identical next hop or a free one, reference taken; -ENOSPC when full.
 * *is_new is set when the caller must write the entry to the device. Index 0 is
 * never used, so a zeroed entry never points at a live next hop by accident. */
int natgw_nh_get(struct natgw_nh_table *nt, const struct natgw_nh *nh, bool *is_new);
/* drop a reference; returns the remaining count, or -EINVAL */
int natgw_nh_put(struct natgw_nh_table *nt, uint16_t idx);

#ifdef __cplusplus
}
#endif

#endif /* NATGW_H */
