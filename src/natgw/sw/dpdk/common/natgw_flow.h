/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * rte_flow backend for the natgw NAT shim, shared by the cndm PMD (real
 * hardware, BAR window) and the natgw_model PMD (software model).
 *
 * Supported flow (one fixed template, matching what the shim offloads):
 *   attr:    ingress, group 0, priority 0
 *   pattern: ETH (no MAC match) / [VLAN (exact VID)] / IPV4 (exact src, dst)
 *            / TCP or UDP (exact ports) / END
 *   actions: SET_MAC_DST, SET_MAC_SRC (required)
 *            PORT_ID or REPRESENTED_PORT: egress port of the same device (required)
 *            [SET_IPV4_SRC + SET_TP_SRC] or [SET_IPV4_DST + SET_TP_DST]
 *            [DEC_TTL] [OF_SET_VLAN_VID] [COUNT] [AGE] END
 *
 * Every port of one device shares one context (one table in hardware); the
 * port a flow is created on is its ingress lane.
 */

#ifndef NATGW_FLOW_H
#define NATGW_FLOW_H

#include <stdbool.h>
#include <stdint.h>

#include <rte_compat.h>
#include <rte_flow.h>
#include <rte_mbuf.h>

#include "natgw.h"

struct natgw_flow_ctx;

struct natgw_flow_cfg {
	unsigned lanes;
	uint32_t ticks_per_sec;     /* rate of the hardware tick counter */
	uint32_t tick_div;          /* value programmed into TICK_DIV */
	uint32_t seed0, seed1;
	unsigned max_depth;         /* cuckoo relocation search depth */
	bool     punt_hdr;          /* ask the hardware for punt headers */
};

/* create a context over a device's register window; resets the NAT block */
__rte_internal
struct natgw_flow_ctx *natgw_flow_ctx_create(const struct natgw_io *io, const struct natgw_flow_cfg *cfg,
					     int socket_id);
__rte_internal
void natgw_flow_ctx_destroy(struct natgw_flow_ctx *ctx);

/* bind ethdev port <port_id> to lane <lane> of this context */
__rte_internal
int natgw_flow_bind_port(struct natgw_flow_ctx *ctx, unsigned lane, uint16_t port_id);
__rte_internal
void natgw_flow_unbind_port(uint16_t port_id);

/* enable the fast path (all lanes); false returns every lane to bypass */
__rte_internal
void natgw_flow_enable(struct natgw_flow_ctx *ctx, bool enable);

/* the rte_flow ops for eth_dev_ops.flow_ops_get */
__rte_internal
const struct rte_flow_ops *natgw_flow_ops(void);

/* drain hardware events, update aging; call periodically (e.g. every 100 ms) */
__rte_internal
void natgw_flow_poll(struct natgw_flow_ctx *ctx);

/* xstats for one port (lane): per-reason punts, forwards, drops */
__rte_internal
int natgw_flow_xstats_get_names(struct natgw_flow_ctx *ctx, struct rte_eth_xstat_name *names, unsigned size);
__rte_internal
int natgw_flow_xstats_get(struct natgw_flow_ctx *ctx, unsigned lane, struct rte_eth_xstat *xstats, unsigned n);
__rte_internal
unsigned natgw_flow_xstats_count(void);

/* number of offloaded flows, and table capacity */
__rte_internal
unsigned natgw_flow_count(struct natgw_flow_ctx *ctx);
__rte_internal
unsigned natgw_flow_capacity(struct natgw_flow_ctx *ctx);

/* ------------------------------------------------------------------ */
/* punt metadata on received mbufs */

/* the dynfield name an application uses to find the metadata */
#define NATGW_PUNT_DYNFIELD "natgw_dynfield_punt"
#define NATGW_PUNT_DYNFLAG  "natgw_dynflag_punt"

struct natgw_punt_meta {
	uint32_t idx;      /* hit entry, 0xffffffff when none */
	uint32_t hash;
	uint8_t  reason;   /* enum natgw_reason */
	uint8_t  flags;    /* NATGW_PUNT_F_* */
	uint8_t  lane;
	uint8_t  rsvd;
};

/* register (idempotent); returns 0 or a negative errno */
__rte_internal
int natgw_punt_meta_register(void);
/* strip a punt header from a received mbuf and record its metadata;
 * returns 0, or -EINVAL when the mbuf does not start with a punt header */
__rte_internal
int natgw_punt_strip(struct rte_mbuf *m);

#endif /* NATGW_FLOW_H */
