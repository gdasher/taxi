/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * natgw_offload: mirrors nat44-ed sessions into the natgw FPGA flow table
 * through rte_flow (cndm or net_natgw_model ports).
 *
 * nat44-ed raises session events on the worker that owns the session; they are
 * queued per thread and handled by a process on the main thread, which owns
 * every rte_flow call. A session is offloaded as two flows (i2o, o2i), each
 * entering on the port where the other direction leaves. Hardware counters
 * and last-hit times are folded back into the sessions periodically under the
 * worker barrier, and sessions that VPP would have expired are deleted.
 */

#ifndef __included_natgw_offload_h__
#define __included_natgw_offload_h__

#include <vlib/vlib.h>
#include <vnet/vnet.h>
#include <nat/nat44-ed/nat44_ed.h>

struct rte_flow;

/* why a session was not offloaded, or a flow removed */
#define foreach_ngo_counter                                                   \
  _ (EVENTS, "events")                                                        \
  _ (INSTALLED, "sessions offloaded")                                         \
  _ (REMOVED, "sessions removed")                                             \
  _ (REINSTALLED, "sessions reinstalled after next-hop change")              \
  _ (SKIP_PROTO, "skipped: not TCP or UDP")                                   \
  _ (SKIP_FLAGS, "skipped: twice-NAT, hairpin or bypass session")             \
  _ (SKIP_OPS, "skipped: translation not expressible")                        \
  _ (SKIP_NO_ROUTE, "skipped: no route or neighbour unresolved")              \
  _ (SKIP_NOT_NATGW, "skipped: interface is not a natgw port")                \
  _ (SKIP_VLAN, "skipped: VLAN tagging differs between sides")                \
  _ (SKIP_FLOW_ERROR, "skipped: rte_flow rejected")                           \
  _ (TABLE_FULL, "table full")                                                \
  _ (RETRY_GAVE_UP, "retries exhausted")                                      \
  _ (QUEUE_OVERFLOW, "install events dropped (queue full)")                   \
  _ (SYNCED, "flows synced")                                                  \
  _ (EXPIRED, "sessions expired after sync")                                  \
  _ (AGED_EVENTS, "hardware age events")                                      \
  _ (STALE, "sync skipped: session gone")                                     \
  _ (BULK, "sessions offloaded as bulk (DDR tier)")

typedef enum
{
#define _(n, s) NGO_CTR_##n,
  foreach_ngo_counter
#undef _
    NGO_N_CTR,
} ngo_counter_t;

/* a nat44-ed session snapshot, taken on the owning thread */
typedef struct
{
  u8 ev; /* nat44_ed_ses_event_t */
  u8 proto;
  u32 thread_index;
  u32 session_index;
  u32 flags;
  u32 timeout; /* seconds */
  nat_6t_flow_t i2o, o2i;
} ngo_event_t;

/* resolved next hop of one direction */
typedef struct
{
  u32 sw_if_index;
  u32 adj_index;
  u16 port_id;
  i16 vid; /* -1: untagged */
  u8 dst_mac[6], src_mac[6];
} ngo_nh_t;

typedef struct
{
  ngo_event_t snap;    /* the session as installed */
  ngo_nh_t nh[2];      /* [0] i2o egress, [1] o2i egress */
  struct rte_flow *flow[2];
  u16 in_port[2];
  u64 pkts, bytes;     /* totals folded into the session */
  f64 installed;
  u8 retries;          /* pending only */
} ngo_session_t;

typedef struct
{
  clib_spinlock_t lock;
  ngo_event_t *events;
  u32 n_install_events;
} ngo_thread_t;

typedef struct
{
  /* config */
  u8 enabled;
  u8 enable_on_start;
  u32 active_pkts;    /* UDP packets before a session is offloaded */
  f64 sync_interval;  /* seconds between sync passes */
  u32 sync_budget;    /* sessions synced per pass */
  u32 max_queue;      /* install events queued per thread */
  u8 max_retries;
  u8 dec_ttl;
  u16 *bulk_udp_ports; /* UDP server ports offloaded as bulk (rte_flow priority 1) */

  /* sessions: offloaded and pending (waiting for a neighbour or space) */
  ngo_session_t *sessions;
  uword *by_key;      /* (thread << 32 | session index) -> pool index */
  u32 *pending;       /* pool indices */
  u32 sync_cursor;
  f64 next_sync, next_retry;

  ngo_thread_t *threads;
  volatile u8 clear_pending;

  u64 counters[NGO_N_CTR];
  u32 stats_offloaded; /* stats segment gauge */

  /* nat44-ed and dpdk plugin symbols */
  snat_main_t *sm;
  int (*register_cb) (nat44_ed_ses_event_cb_t *cb, u32 active_pkts);
  void (*refresh) (u32 thread_index, snat_session_t *s, f64 last_heard,
		   u64 pkts, u64 bytes);
  int (*del_by_index) (u32 thread_index, u32 session_index);
  u32 (*timeout_of) (snat_session_t *s);
  void *dpdk_main;

  u32 process_node_index;
  vlib_main_t *vm;
} ngo_main_t;

extern ngo_main_t ngo_main;

clib_error_t *ngo_enable_disable (int enable);
void ngo_sync_now (vlib_main_t *vm);
u8 *format_ngo_session (u8 *s, va_list *args);

#endif /* __included_natgw_offload_h__ */
