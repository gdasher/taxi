/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * Software model of the natgw shim: the same register interface as
 * rtl/natgw_regs.sv (usable through natgw_dev / struct natgw_io) and the
 * same packet behaviour as the RTL (classification, lookup, rewrite, punt
 * header, per-entry state, events, statistics). Cross-checked against
 * tb/natgw_model.py, which is the scoreboard of the RTL tests.
 */

#ifndef NATGW_MODEL_H
#define NATGW_MODEL_H

#include "natgw.h"

#ifdef __cplusplus
extern "C" {
#endif

struct natgw_model;

enum natgw_model_kind {
	NATGW_OUT_FWD = 1,   /* forwarded to an egress lane (rewritten) */
	NATGW_OUT_PUNT = 2,  /* to the host on the ingress lane (with header if enabled) */
};

struct natgw_model_out {
	int      kind;
	uint8_t  lane;       /* egress lane (FWD) or ingress lane (PUNT) */
	uint8_t  reason;     /* RSN_FWD when forwarded */
	uint32_t hit_idx;    /* 0xffffffff when no hit */
	size_t   len;
	uint8_t *data;       /* caller's buffer, at least frame length + NATGW_PUNT_HDR_LEN */
};

/* bucket_w: table geometry (entries = 8 << bucket_w) */
NATGW_API
struct natgw_model *natgw_model_create(unsigned bucket_w);
NATGW_API
void natgw_model_destroy(struct natgw_model *m);

/*
 * Give the model a DDR tier (DDR_STATUS present): 2 x 2^ddr_bucket_w lines of
 * two entries, looked up after an on-chip miss while calibrated, enabled and
 * not clearing. calibrated = false models a build with the tier but no
 * working DIMM. Like real DDR the table starts with random contents until
 * cleared. 0, or -EINVAL / -ENOMEM.
 */
NATGW_API
int natgw_model_set_ddr(struct natgw_model *m, unsigned ddr_bucket_w, bool calibrated);

/* register interface, for natgw_dev_init() */
NATGW_API
struct natgw_io natgw_model_io(struct natgw_model *m);
NATGW_API
uint32_t natgw_model_rd(struct natgw_model *m, uint32_t off);
NATGW_API
void natgw_model_wr(struct natgw_model *m, uint32_t off, uint32_t val);

/* process one frame (no FCS) received on a lane */
NATGW_API
int natgw_model_rx(struct natgw_model *m, unsigned lane, const uint8_t *frame, size_t len,
		   struct natgw_model_out *out);

/* advance the tick counter and run one full aging scan */
NATGW_API
void natgw_model_advance(struct natgw_model *m, uint32_t ticks);

#ifdef __cplusplus
}
#endif

#endif /* NATGW_MODEL_H */
