/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * natgw_offload: nat44-ed session offload to the natgw FPGA via rte_flow.
 * See natgw_offload.h for the design.
 */

#include <vlib/vlib.h>
#include <vlib/unix/plugin.h>
#include <vlib/stats/stats.h>
#include <vnet/vnet.h>
#include <vnet/plugin/plugin.h>
#include <vnet/fib/ip4_fib.h>
#include <vnet/dpo/load_balance.h>
#include <vnet/dpo/load_balance_map.h>
#include <vnet/adj/adj.h>
#include <vnet/ip/ip4_inlines.h>
#include <vnet/tcp/tcp_packet.h>
#include <vnet/udp/udp_packet.h>
#include <vpp/app/version.h>

#include <dpdk/device/dpdk.h>
#include <rte_flow.h>

#include <natgw_offload/natgw_offload.h>

ngo_main_t ngo_main;

VLIB_REGISTER_LOG_CLASS (ngo_log, static) = {
  .class_name = "natgw-offload",
};
#define ngo_log_err(...)   vlib_log (VLIB_LOG_LEVEL_ERR, ngo_log.class, __VA_ARGS__)
#define ngo_log_debug(...) vlib_log (VLIB_LOG_LEVEL_DEBUG, ngo_log.class, __VA_ARGS__)

static const char *ngo_counter_names[] = {
#define _(n, s) s,
  foreach_ngo_counter
#undef _
};

#define NGO_COUNT(c) (ngo_main.counters[NGO_CTR_##c]++)

typedef enum
{
  NGO_OK,
  NGO_RETRY, /* may succeed later: neighbour, route or table space */
  NGO_FAIL,
} ngo_rv_t;

static_always_inline u64
ngo_key (u32 thread_index, u32 session_index)
{
  return ((u64) thread_index << 32) | session_index;
}

/* ------------------------------------------------------------------ */
/* session events (any thread) */

static void
ngo_session_event (u32 thread_index, snat_session_t *s,
		   nat44_ed_ses_event_t ev)
{
  ngo_main_t *ngm = &ngo_main;
  ngo_thread_t *t;
  ngo_event_t *e;
  int install;

  if (ev == NAT44_ED_SES_EV_CLEAR)
    {
      /* every session is gone: queued events are moot */
      vec_foreach (t, ngm->threads)
	{
	  clib_spinlock_lock (&t->lock);
	  vec_reset_length (t->events);
	  t->n_install_events = 0;
	  clib_spinlock_unlock (&t->lock);
	}
      ngm->clear_pending = 1;
      return;
    }

  switch (ev)
    {
    case NAT44_ED_SES_EV_ESTABLISHED:
      install = 1;
      break;
    case NAT44_ED_SES_EV_ACTIVE:
      /* UDP after a few packets; TCP waits for its handshake */
      if (s->proto == IP_PROTOCOL_TCP &&
	  s->tcp_state != NAT44_ED_TCP_STATE_ESTABLISHED)
	return;
      install = 1;
      break;
    default:
      /* only sessions we were told about can have flows */
      if (!(s->flags & SNAT_SESSION_FLAG_OFFLOAD))
	return;
      install = 0;
      break;
    }

  if (install && s->proto != IP_PROTOCOL_TCP && s->proto != IP_PROTOCOL_UDP)
    return;
  /* queue by owning thread, so a session's events stay in order */
  if (s->thread_index >= vec_len (ngm->threads))
    return;
  t = vec_elt_at_index (ngm->threads, s->thread_index);
  clib_spinlock_lock (&t->lock);
  if (install && t->n_install_events >= ngm->max_queue)
    {
      clib_spinlock_unlock (&t->lock);
      clib_atomic_fetch_add (&ngm->counters[NGO_CTR_QUEUE_OVERFLOW], 1);
      return;
    }
  vec_add2 (t->events, e, 1);
  t->n_install_events += install;
  e->ev = ev;
  e->proto = s->proto;
  e->thread_index = s->thread_index;
  e->session_index = s - ngm->sm->per_thread_data[s->thread_index].sessions;
  e->flags = s->flags;
  if (install)
    {
      e->timeout = ngm->timeout_of (s);
      clib_memcpy_fast (&e->i2o, &s->i2o, sizeof (e->i2o));
      clib_memcpy_fast (&e->o2i, &s->o2i, sizeof (e->o2i));
      s->flags |= SNAT_SESSION_FLAG_OFFLOAD;
    }
  else if (ev == NAT44_ED_SES_EV_DELETE)
    s->flags &= ~SNAT_SESSION_FLAG_OFFLOAD;
  clib_spinlock_unlock (&t->lock);
}

/* ------------------------------------------------------------------ */
/* next-hop resolution: the path VPP's own forwarding would take */

static ngo_rv_t
ngo_resolve (u32 fib_index, const ip4_address_t *src, const ip4_address_t *dst,
	     u16 sport, u16 dport, u8 proto, ngo_nh_t *nh)
{
  ngo_main_t *ngm = &ngo_main;
  vnet_main_t *vnm = vnet_get_main ();
  dpdk_main_t *dm = ngm->dpdk_main;
  struct
  {
    ip4_header_t ip;
    udp_header_t l4; /* ports only; same offsets for TCP */
  } h = {};
  const load_balance_t *lb;
  const dpo_id_t *dpo;
  ip_adjacency_t *adj;
  vnet_hw_interface_t *hw;
  vnet_device_class_t *dc;
  dpdk_device_t *xd;
  u32 hash = 0;
  u8 *rw;

  h.ip.ip_version_and_header_length = 0x45;
  h.ip.protocol = proto;
  h.ip.src_address = *src;
  h.ip.dst_address = *dst;
  h.l4.src_port = clib_host_to_net_u16 (sport);
  h.l4.dst_port = clib_host_to_net_u16 (dport);

  lb = load_balance_get (ip4_fib_forwarding_lookup (fib_index, dst));
  for (int depth = 0;; depth++)
    {
      /* as ip4-lookup, then ip4-load-balance for recursive paths */
      if (lb->lb_n_buckets > 1)
	{
	  hash = hash ? hash >> 1 :
			ip4_compute_flow_hash (&h.ip, lb->lb_hash_config);
	  dpo = load_balance_get_fwd_bucket (lb,
					     hash & lb->lb_n_buckets_minus_1);
	}
      else
	dpo = load_balance_get_bucket_i (lb, 0);
      if (dpo->dpoi_type != DPO_LOAD_BALANCE || depth >= 8)
	break;
      lb = load_balance_get (dpo->dpoi_index);
    }

  if (dpo->dpoi_type != DPO_ADJACENCY)
    return NGO_RETRY; /* incomplete, glean, drop, receive ... */
  adj = adj_get (dpo->dpoi_index);
  if (adj->lookup_next_index != IP_LOOKUP_NEXT_REWRITE)
    return NGO_RETRY;

  nh->adj_index = dpo->dpoi_index;
  nh->sw_if_index = adj->rewrite_header.sw_if_index;
  hw = vnet_get_sup_hw_interface (vnm, nh->sw_if_index);
  dc = vnet_get_device_class (vnm, hw->dev_class_index);
  if (!dm || !dc->name || strcmp (dc->name, "dpdk") ||
      hw->dev_instance >= vec_len (dm->devices))
    return NGO_FAIL;
  xd = vec_elt_at_index (dm->devices, hw->dev_instance);
  nh->port_id = xd->port_id;

  /* Ethernet rewrite: dst, src, [802.1Q tag,] IPv4 ethertype */
  rw = vnet_rewrite_get_data (*adj);
  if (adj->rewrite_header.data_bytes == 14 &&
      clib_net_to_host_u16 (*(u16 *) (rw + 12)) == ETHERNET_TYPE_IP4)
    nh->vid = -1;
  else if (adj->rewrite_header.data_bytes == 18 &&
	   clib_net_to_host_u16 (*(u16 *) (rw + 12)) == ETHERNET_TYPE_VLAN &&
	   clib_net_to_host_u16 (*(u16 *) (rw + 16)) == ETHERNET_TYPE_IP4)
    nh->vid = clib_net_to_host_u16 (*(u16 *) (rw + 14)) & 0xfff;
  else
    return NGO_FAIL;
  clib_memcpy_fast (nh->dst_mac, rw, 6);
  clib_memcpy_fast (nh->src_mac, rw + 6, 6);
  return NGO_OK;
}

static_always_inline u32
ngo_tx_fib (const nat_6t_flow_t *f)
{
  return (f->ops & NAT_FLOW_OP_TXFIB_REWRITE) ? f->rewrite.fib_index :
						 f->match.fib_index;
}

/* resolve both directions of a session snapshot */
static ngo_rv_t
ngo_resolve_session (const ngo_event_t *e, ngo_nh_t nh[2])
{
  const nat_6t_flow_t *i = &e->i2o, *o = &e->o2i;
  ngo_rv_t rv;

  /* i2o: VPP looks the route up before output-feature NAT translates the
   * source, so hash the original header */
  rv = ngo_resolve (ngo_tx_fib (i), &i->match.saddr, &i->match.daddr,
		    clib_net_to_host_u16 (i->match.sport),
		    clib_net_to_host_u16 (i->match.dport), e->proto, &nh[0]);
  if (rv != NGO_OK)
    return rv;
  /* o2i: out2in translates the destination before the lookup */
  return ngo_resolve (
    ngo_tx_fib (o), &o->match.saddr, &o->rewrite.daddr,
    clib_net_to_host_u16 (o->match.sport),
    clib_net_to_host_u16 ((o->ops & NAT_FLOW_OP_DPORT_REWRITE) ?
			    o->rewrite.dport :
			    o->match.dport),
    e->proto, &nh[1]);
}

static int
ngo_nh_equal (const ngo_nh_t *a, const ngo_nh_t *b)
{
  return a->port_id == b->port_id && a->vid == b->vid &&
	 !memcmp (a->dst_mac, b->dst_mac, 6) &&
	 !memcmp (a->src_mac, b->src_mac, 6);
}

/* ------------------------------------------------------------------ */
/* rte_flow */

/* a session the hardware may keep in its slower, larger tier: UDP to one of
 * the configured server ports (e.g. DNS) */
static int
ngo_is_bulk (const ngo_event_t *e)
{
  ngo_main_t *ngm = &ngo_main;
  u16 *p, port = clib_net_to_host_u16 (e->i2o.match.dport);

  if (e->proto != IP_PROTOCOL_UDP)
    return 0;
  vec_foreach (p, ngm->bulk_udp_ports)
    if (*p == port)
      return 1;
  return 0;
}

static struct rte_flow *
ngo_flow_create (u16 in_port, i16 in_vid, const nat_6t_flow_t *f, u8 proto,
		 int dnat, const ngo_nh_t *nh, u32 age, void *age_ctx,
		 int bulk, struct rte_flow_error *err)
{
  ngo_main_t *ngm = &ngo_main;
  struct rte_flow_attr attr = { .ingress = 1, .priority = bulk ? 1 : 0 };
  struct rte_flow_item_vlan vs = {}, vm = {};
  struct rte_flow_item_ipv4 ips = {}, ipm = {};
  struct rte_flow_item_tcp ts = {}, tm = {};
  struct rte_flow_item_udp us = {}, um = {};
  struct rte_flow_item pat[5];
  struct rte_flow_action_set_ipv4 sip = {};
  struct rte_flow_action_set_tp stp = {};
  struct rte_flow_action_set_mac smac, dmac;
  struct rte_flow_action_port_id pid = { .id = nh->port_id };
  struct rte_flow_action_count cnt = {};
  struct rte_flow_action_age aga = { .timeout = age, .context = age_ctx };
  struct rte_flow_action_of_set_vlan_vid vid = {};
  struct rte_flow_action act[12];
  int i = 0, j = 0;

  pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_ETH };
  if (in_vid >= 0)
    {
      vs.hdr.vlan_tci = clib_host_to_net_u16 (in_vid);
      vm.hdr.vlan_tci = clib_host_to_net_u16 (0x0fff);
      pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_VLAN,
					 .spec = &vs,
					 .mask = &vm };
    }
  ips.hdr.src_addr = f->match.saddr.as_u32;
  ips.hdr.dst_addr = f->match.daddr.as_u32;
  ipm.hdr.src_addr = ipm.hdr.dst_addr = ~0;
  pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_IPV4,
				     .spec = &ips,
				     .mask = &ipm };
  if (proto == IP_PROTOCOL_TCP)
    {
      ts.hdr.src_port = f->match.sport;
      ts.hdr.dst_port = f->match.dport;
      tm.hdr.src_port = tm.hdr.dst_port = 0xffff;
      pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_TCP,
					 .spec = &ts,
					 .mask = &tm };
    }
  else
    {
      us.hdr.src_port = f->match.sport;
      us.hdr.dst_port = f->match.dport;
      um.hdr.src_port = um.hdr.dst_port = 0xffff;
      pat[i++] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_UDP,
					 .spec = &us,
					 .mask = &um };
    }
  pat[i] = (struct rte_flow_item){ .type = RTE_FLOW_ITEM_TYPE_END };

  /* nat_6t ports are in network order, like rte_flow's */
  if (dnat)
    {
      sip.ipv4_addr = f->rewrite.daddr.as_u32;
      stp.port = (f->ops & NAT_FLOW_OP_DPORT_REWRITE) ? f->rewrite.dport :
							 f->match.dport;
    }
  else
    {
      sip.ipv4_addr = f->rewrite.saddr.as_u32;
      stp.port = (f->ops & NAT_FLOW_OP_SPORT_REWRITE) ? f->rewrite.sport :
							 f->match.sport;
    }
  clib_memcpy_fast (smac.mac_addr, nh->src_mac, 6);
  clib_memcpy_fast (dmac.mac_addr, nh->dst_mac, 6);
  act[j++] = (struct rte_flow_action){
    .type = dnat ? RTE_FLOW_ACTION_TYPE_SET_IPV4_DST :
		   RTE_FLOW_ACTION_TYPE_SET_IPV4_SRC,
    .conf = &sip
  };
  act[j++] = (struct rte_flow_action){ .type = dnat ?
						 RTE_FLOW_ACTION_TYPE_SET_TP_DST :
						 RTE_FLOW_ACTION_TYPE_SET_TP_SRC,
				       .conf = &stp };
  if (ngm->dec_ttl)
    act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_DEC_TTL };
  act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_SET_MAC_SRC,
				       .conf = &smac };
  act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_SET_MAC_DST,
				       .conf = &dmac };
  if (nh->vid >= 0)
    {
      vid.vlan_vid = clib_host_to_net_u16 (nh->vid);
      act[j++] = (struct rte_flow_action){
	.type = RTE_FLOW_ACTION_TYPE_OF_SET_VLAN_VID, .conf = &vid
      };
    }
  act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_PORT_ID,
				       .conf = &pid };
  act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_COUNT,
				       .conf = &cnt };
  act[j++] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_AGE,
				       .conf = &aga };
  act[j] = (struct rte_flow_action){ .type = RTE_FLOW_ACTION_TYPE_END };

  return rte_flow_create (in_port, &attr, pat, act, err);
}

static void
ngo_flows_destroy (ngo_session_t *ngs)
{
  struct rte_flow_error err;

  for (int d = 0; d < 2; d++)
    if (ngs->flow[d])
      {
	if (rte_flow_destroy (ngs->in_port[d], ngs->flow[d], &err))
	  ngo_log_err ("rte_flow_destroy port %u: %s", ngs->in_port[d],
		       err.message ? err.message : "?");
	ngs->flow[d] = 0;
      }
}

/* nat44-ed may set rewrite ops that leave a field unchanged; only the
 * changes matter: i2o may change the source, o2i the destination */
static int
ngo_ops_ok (const ngo_event_t *e)
{
  const nat_6t_flow_t *i = &e->i2o, *o = &e->o2i;

  if ((i->ops | o->ops) & NAT_FLOW_OP_ICMP_ID_REWRITE)
    return 0;
  if ((i->ops & NAT_FLOW_OP_DADDR_REWRITE) &&
      i->rewrite.daddr.as_u32 != i->match.daddr.as_u32)
    return 0;
  if ((i->ops & NAT_FLOW_OP_DPORT_REWRITE) &&
      i->rewrite.dport != i->match.dport)
    return 0;
  if ((o->ops & NAT_FLOW_OP_SADDR_REWRITE) &&
      o->rewrite.saddr.as_u32 != o->match.saddr.as_u32)
    return 0;
  if ((o->ops & NAT_FLOW_OP_SPORT_REWRITE) &&
      o->rewrite.sport != o->match.sport)
    return 0;
  return (i->ops & NAT_FLOW_OP_SADDR_REWRITE) &&
	 (o->ops & NAT_FLOW_OP_DADDR_REWRITE) && i->match.proto == e->proto &&
	 o->match.proto == e->proto;
}

/* install both flows of a session; on failure nothing is left installed */
static ngo_rv_t
ngo_install (ngo_session_t *ngs, u32 pool_index)
{
  ngo_main_t *ngm = &ngo_main;
  const ngo_event_t *e = &ngs->snap;
  struct rte_flow_error err = {};
  void *ctx = uword_to_pointer ((uword) pool_index + 1, void *);
  ngo_rv_t rv;

  if (e->proto != IP_PROTOCOL_TCP && e->proto != IP_PROTOCOL_UDP)
    return NGO_COUNT (SKIP_PROTO), NGO_FAIL;
  if (e->flags & (SNAT_SESSION_FLAG_TWICE_NAT | SNAT_SESSION_FLAG_HAIRPINNING |
		  SNAT_SESSION_FLAG_FWD_BYPASS))
    return NGO_COUNT (SKIP_FLAGS), NGO_FAIL;
  if (!ngo_ops_ok (e))
    return NGO_COUNT (SKIP_OPS), NGO_FAIL;

  rv = ngo_resolve_session (e, ngs->nh);
  if (rv == NGO_RETRY)
    return NGO_RETRY;
  if (rv != NGO_OK)
    return NGO_COUNT (SKIP_NOT_NATGW), NGO_FAIL;
  if ((ngs->nh[0].vid < 0) != (ngs->nh[1].vid < 0))
    return NGO_COUNT (SKIP_VLAN), NGO_FAIL;

  /* each direction enters where the other leaves */
  ngs->in_port[0] = ngs->nh[1].port_id;
  ngs->in_port[1] = ngs->nh[0].port_id;
  int bulk = ngo_is_bulk (e);
  ngs->flow[0] = ngo_flow_create (ngs->in_port[0], ngs->nh[1].vid, &e->i2o,
				  e->proto, 0, &ngs->nh[0], e->timeout, ctx,
				  bulk, &err);
  if (ngs->flow[0])
    ngs->flow[1] = ngo_flow_create (ngs->in_port[1], ngs->nh[0].vid,
				    &e->o2i, e->proto, 1, &ngs->nh[1],
				    e->timeout, ctx, bulk, &err);
  if (!ngs->flow[0] || !ngs->flow[1])
    {
      int code = rte_errno;
      ngo_flows_destroy (ngs);
      if (code == ENOSPC)
	return NGO_COUNT (TABLE_FULL), NGO_RETRY;
      ngo_log_debug ("flow rejected: %s (%d)", err.message ? err.message : "?",
		     code);
      return NGO_COUNT (SKIP_FLOW_ERROR), NGO_FAIL;
    }
  ngs->installed = vlib_time_now (ngm->vm);
  ngs->pkts = ngs->bytes = 0;
  if (bulk)
    NGO_COUNT (BULK);
  return NGO_OK;
}

static void
ngo_gauge_update (void)
{
  ngo_main_t *ngm = &ngo_main;
  vlib_stats_set_gauge (ngm->stats_offloaded,
			pool_elts (ngm->sessions) - vec_len (ngm->pending));
}

static void
ngo_session_free (u32 pi)
{
  ngo_main_t *ngm = &ngo_main;
  ngo_session_t *ngs = pool_elt_at_index (ngm->sessions, pi);
  u32 k;

  if (ngs->flow[0])
    NGO_COUNT (REMOVED);
  ngo_flows_destroy (ngs);
  if ((k = vec_search (ngm->pending, pi)) != ~0)
    vec_del1 (ngm->pending, k);
  hash_unset (ngm->by_key,
	      ngo_key (ngs->snap.thread_index, ngs->snap.session_index));
  pool_put (ngm->sessions, ngs);
}

static void
ngo_remove_all (void)
{
  ngo_main_t *ngm = &ngo_main;
  u32 *pis = 0, *pi, k;

  pool_foreach_index (k, ngm->sessions)
    vec_add1 (pis, k);
  vec_foreach (pi, pis)
    ngo_session_free (*pi);
  vec_free (pis);
  vec_reset_length (ngm->pending);
  ngo_gauge_update ();
}

static void
ngo_handle_event (ngo_event_t *e)
{
  ngo_main_t *ngm = &ngo_main;
  u64 key = ngo_key (e->thread_index, e->session_index);
  uword *p = hash_get (ngm->by_key, key);
  ngo_session_t *ngs;
  u32 pi;

  ngm->counters[NGO_CTR_EVENTS]++;
  switch (e->ev)
    {
    case NAT44_ED_SES_EV_ESTABLISHED:
    case NAT44_ED_SES_EV_ACTIVE:
      if (p)
	return; /* offloaded or pending already */
      pool_get_zero (ngm->sessions, ngs);
      pi = ngs - ngm->sessions;
      ngs->snap = *e;
      hash_set (ngm->by_key, key, pi);
      switch (ngo_install (ngs, pi))
	{
	case NGO_OK:
	  NGO_COUNT (INSTALLED);
	  break;
	case NGO_RETRY:
	  vec_add1 (ngm->pending, pi);
	  break;
	case NGO_FAIL:
	  ngo_session_free (pi);
	  break;
	}
      break;
    case NAT44_ED_SES_EV_CLOSING:
    case NAT44_ED_SES_EV_DELETE:
      if (p)
	ngo_session_free (p[0]);
      break;
    default:
      break;
    }
}

static void
ngo_drain_events (void)
{
  ngo_main_t *ngm = &ngo_main;
  ngo_thread_t *t;
  ngo_event_t *events = 0, *e;

  if (ngm->clear_pending)
    {
      ngm->clear_pending = 0;
      ngo_remove_all ();
    }
  vec_foreach (t, ngm->threads)
    {
      if (!vec_len (t->events))
	continue;
      clib_spinlock_lock (&t->lock);
      events = t->events;
      t->events = 0;
      t->n_install_events = 0;
      clib_spinlock_unlock (&t->lock);
      vec_foreach (e, events)
	ngo_handle_event (e);
      vec_free (events);
    }
  ngo_gauge_update ();
}

static void
ngo_retry_pending (void)
{
  ngo_main_t *ngm = &ngo_main;
  u32 *pending = ngm->pending, *pi;

  ngm->pending = 0;
  vec_foreach (pi, pending)
    {
      ngo_session_t *ngs = pool_elt_at_index (ngm->sessions, *pi);
      switch (ngo_install (ngs, *pi))
	{
	case NGO_OK:
	  NGO_COUNT (INSTALLED);
	  break;
	case NGO_RETRY:
	  if (++ngs->retries < ngm->max_retries)
	    {
	      vec_add1 (ngm->pending, *pi);
	      break;
	    }
	  NGO_COUNT (RETRY_GAVE_UP);
	  /* fallthrough */
	case NGO_FAIL:
	  ngo_session_free (*pi);
	  break;
	}
    }
  vec_free (pending);
  ngo_gauge_update ();
}

/* ------------------------------------------------------------------ */
/* sync: hardware counters and last-hit times back into nat44-ed */

typedef struct
{
  u32 pi;
  f64 last_heard; /* 0: no new hits */
  u64 pkts, bytes;
} ngo_update_t;

static int
ngo_query (ngo_session_t *ngs, f64 now, ngo_update_t *u)
{
  struct rte_flow_action qc[] = { { .type = RTE_FLOW_ACTION_TYPE_COUNT },
				  { .type = RTE_FLOW_ACTION_TYPE_END } };
  struct rte_flow_action qa[] = { { .type = RTE_FLOW_ACTION_TYPE_AGE },
				  { .type = RTE_FLOW_ACTION_TYPE_END } };
  struct rte_flow_error err;
  u32 since = ~0;
  int heard = 0;

  u->pkts = u->bytes = 0;
  u->last_heard = 0;
  for (int d = 0; d < 2; d++)
    {
      struct rte_flow_query_count c = { .reset = 1 };
      struct rte_flow_query_age a = {};
      if (rte_flow_query (ngs->in_port[d], ngs->flow[d], qc, &c, &err) ||
	  rte_flow_query (ngs->in_port[d], ngs->flow[d], qa, &a, &err))
	return -1;
      if (!c.hits_set)
	{
	  /* a flow in the DDR tier: no counters, only the time since the
	   * hardware last saw it active (refresh never moves last_heard
	   * backwards, so reporting an old time is harmless) */
	  if (a.sec_since_last_hit_valid)
	    {
	      since = clib_min (since, (u32) a.sec_since_last_hit);
	      heard = 1;
	    }
	  continue;
	}
      u->pkts += c.hits;
      u->bytes += c.bytes;
      if (c.hits && a.sec_since_last_hit_valid)
	{
	  u32 sec = a.sec_since_last_hit;
	  since = clib_min (since, sec);
	}
      heard |= c.hits != 0;
    }
  if (heard)
    u->last_heard = now - (since == ~0 ? 0 : since);
  return 0;
}

/* next hops changed since install? reinstall (outside the barrier) */
static void
ngo_revalidate (u32 pi)
{
  ngo_main_t *ngm = &ngo_main;
  ngo_session_t *ngs = pool_elt_at_index (ngm->sessions, pi);
  ngo_nh_t nh[2];
  ngo_rv_t rv = ngo_resolve_session (&ngs->snap, nh);

  if (rv == NGO_OK && ngo_nh_equal (&nh[0], &ngs->nh[0]) &&
      ngo_nh_equal (&nh[1], &ngs->nh[1]))
    return;
  ngo_flows_destroy (ngs);
  ngs->retries = 0;
  switch (ngo_install (ngs, pi))
    {
    case NGO_OK:
      NGO_COUNT (REINSTALLED);
      break;
    case NGO_RETRY:
      vec_add1 (ngm->pending, pi);
      break;
    case NGO_FAIL:
      NGO_COUNT (REMOVED);
      ngo_session_free (pi);
      break;
    }
}

static void
ngo_sync (vlib_main_t *vm, int all)
{
  ngo_main_t *ngm = &ngo_main;
  snat_main_t *sm = ngm->sm;
  f64 now = vlib_time_now (vm);
  ngo_update_t *updates = 0, *u;
  u32 *pis = 0, *pi, n, start;
  uword *seen = 0;

  /* sessions whose hardware entries aged first, then a budgeted sweep */
  {
    u16 ports[RTE_MAX_ETHPORTS] = {}, port;
    ngo_session_t *ngs;
    pool_foreach (ngs, ngm->sessions)
      if (ngs->flow[0])
	ports[ngs->in_port[0]] = ports[ngs->in_port[1]] = 1;
    for (port = 0; port < RTE_MAX_ETHPORTS; port++)
      {
	void *ctx[256];
	struct rte_flow_error err;
	int k;
	if (!ports[port])
	  continue;
	while ((k = rte_flow_get_aged_flows (port, ctx, ARRAY_LEN (ctx), &err)) >
	       0)
	  {
	    int progress = 0;
	    for (int x = 0; x < k; x++)
	      {
		u32 idx = pointer_to_uword (ctx[x]) - 1;
		ngm->counters[NGO_CTR_AGED_EVENTS]++;
		if (!pool_is_free_index (ngm->sessions, idx) &&
		    !clib_bitmap_get (seen, idx))
		  {
		    seen = clib_bitmap_set (seen, idx, 1);
		    vec_add1 (pis, idx);
		    progress = 1;
		  }
	      }
	    if (!progress || k < ARRAY_LEN (ctx))
	      break;
	  }
      }
  }
  n = pool_len (ngm->sessions);
  start = ngm->sync_cursor;
  for (u32 i = 0, taken = 0;
       i < n && (all || taken < ngm->sync_budget); i++)
    {
      u32 k = (start + i) % n;
      if (pool_is_free_index (ngm->sessions, k))
	continue;
      ngm->sync_cursor = k + 1;
      taken++;
      if (!clib_bitmap_get (seen, k))
	vec_add1 (pis, k);
    }
  clib_bitmap_free (seen);

  vec_foreach (pi, pis)
    {
      ngo_session_t *ngs;
      if (pool_is_free_index (ngm->sessions, *pi))
	continue;
      ngs = pool_elt_at_index (ngm->sessions, *pi);
      if (!ngs->flow[0])
	continue; /* pending */
      vec_add2 (updates, u, 1);
      u->pi = *pi;
      if (ngo_query (ngs, now, u))
	{
	  vec_dec_len (updates, 1);
	  continue;
	}
      ngm->counters[NGO_CTR_SYNCED]++;
    }

  /* fold into the sessions; expire those VPP would have expired */
  if (vec_len (updates))
    {
      vlib_worker_thread_barrier_sync (vm);
      vec_foreach (u, updates)
	{
	  ngo_session_t *ngs = pool_elt_at_index (ngm->sessions, u->pi);
	  snat_main_per_thread_data_t *tsm;
	  snat_session_t *s;
	  u32 ti = ngs->snap.thread_index, si = ngs->snap.session_index;

	  tsm = vec_elt_at_index (sm->per_thread_data, ti);
	  if (pool_is_free_index (tsm->sessions, si))
	    {
	      ngm->counters[NGO_CTR_STALE]++;
	      continue;
	    }
	  s = pool_elt_at_index (tsm->sessions, si);
	  if (s->i2o.match.as_u64[0] != ngs->snap.i2o.match.as_u64[0] ||
	      s->i2o.match.as_u64[1] != ngs->snap.i2o.match.as_u64[1])
	    {
	      ngm->counters[NGO_CTR_STALE]++;
	      continue;
	    }
	  ngs->pkts += u->pkts;
	  ngs->bytes += u->bytes;
	  ngm->refresh (ti, s, u->last_heard, u->pkts, u->bytes);
	  if (now >= s->last_heard + (f64) ngm->timeout_of (s))
	    {
	      /* raises DELETE; the flows go when the event is drained */
	      ngm->del_by_index (ti, si);
	      ngm->counters[NGO_CTR_EXPIRED]++;
	    }
	}
      vlib_worker_thread_barrier_release (vm);
    }

  /* route and neighbour changes */
  vec_foreach (u, updates)
    if (!pool_is_free_index (ngm->sessions, u->pi))
      ngo_revalidate (u->pi);

  vec_free (updates);
  vec_free (pis);
  ngo_drain_events ();
}

void
ngo_sync_now (vlib_main_t *vm)
{
  ngo_drain_events ();
  ngo_retry_pending ();
  ngo_sync (vm, 1);
}

/* ------------------------------------------------------------------ */
/* main-thread process */

static uword
ngo_process (vlib_main_t *vm, vlib_node_runtime_t *rt, vlib_frame_t *f)
{
  ngo_main_t *ngm = &ngo_main;
  uword *event_data = 0;

  while (1)
    {
      vlib_process_wait_for_event_or_clock (vm, ngm->enabled ? 0.01 : 1.0);
      vlib_process_get_events (vm, &event_data);
      vec_reset_length (event_data);
      if (!ngm->enabled)
	continue;
      ngo_drain_events ();
      f64 now = vlib_time_now (vm);
      if (now >= ngm->next_retry)
	{
	  ngm->next_retry = now + 1.0;
	  ngo_retry_pending ();
	}
      if (now >= ngm->next_sync)
	{
	  ngm->next_sync = now + ngm->sync_interval;
	  ngo_sync (vm, 0);
	}
    }
  return 0;
}

VLIB_REGISTER_NODE (ngo_process_node) = {
  .function = ngo_process,
  .type = VLIB_NODE_TYPE_PROCESS,
  .name = "natgw-offload-process",
  /* rte_flow_create can need tens of KB of stack (rte_hash cuckoo search) */
  .process_log2_n_stack_bytes = 18,
};

/* ------------------------------------------------------------------ */
/* enable / disable */

static clib_error_t *
ngo_resolve_symbols (void)
{
  ngo_main_t *ngm = &ngo_main;

  if (ngm->sm)
    return 0;
#define SYM(field, plugin, name)                                              \
  if (!(ngm->field = vlib_get_plugin_symbol (plugin, name)))                  \
    return clib_error_return (0, "%s: %s not found (plugin enabled?)",       \
			      plugin, name);
  SYM (register_cb, "nat_plugin.so", "nat44_ed_register_session_event_cb");
  SYM (refresh, "nat_plugin.so", "nat44_ed_session_refresh");
  SYM (del_by_index, "nat_plugin.so", "nat44_ed_del_session_by_index");
  SYM (timeout_of, "nat_plugin.so", "nat44_ed_session_timeout");
  SYM (dpdk_main, "dpdk_plugin.so", "dpdk_main");
  SYM (sm, "nat_plugin.so", "snat_main");
#undef SYM
  return 0;
}

clib_error_t *
ngo_enable_disable (int enable)
{
  ngo_main_t *ngm = &ngo_main;
  clib_error_t *err;
  int rv;

  if (enable == ngm->enabled)
    return 0;
  if ((err = ngo_resolve_symbols ()))
    return err;
  vlib_worker_thread_barrier_sync (ngm->vm);
  if (enable)
    {
      rv = ngm->register_cb (ngo_session_event, ngm->active_pkts);
      if (rv)
	{
	  vlib_worker_thread_barrier_release (ngm->vm);
	  return clib_error_return (0, "nat44-ed session callback in use (%d)",
				    rv);
	}
      ngm->enabled = 1;
    }
  else
    {
      ngm->register_cb (0, 0);
      ngm->enabled = 0;
      ngo_thread_t *t;
      vec_foreach (t, ngm->threads)
	{
	  vec_reset_length (t->events);
	  t->n_install_events = 0;
	}
      ngo_remove_all ();
    }
  vlib_worker_thread_barrier_release (ngm->vm);
  vlib_process_signal_event (ngm->vm, ngo_process_node.index, 0, 0);
  return 0;
}

static clib_error_t *
ngo_init (vlib_main_t *vm)
{
  ngo_main_t *ngm = &ngo_main;
  ngo_thread_t *t;

  ngm->vm = vm;
  ngm->active_pkts = 4;
  ngm->sync_interval = 1.0;
  ngm->sync_budget = 4096;
  ngm->max_queue = 65536;
  ngm->max_retries = 10;
  ngm->dec_ttl = 1;
  ngm->by_key = hash_create (0, sizeof (uword));
  vec_validate (ngm->threads, vlib_get_n_threads () - 1);
  vec_foreach (t, ngm->threads)
    clib_spinlock_init (&t->lock);
  ngm->stats_offloaded = vlib_stats_add_gauge ("/natgw/offloaded");
  return 0;
}

VLIB_INIT_FUNCTION (ngo_init);

static clib_error_t *
ngo_config (vlib_main_t *vm, unformat_input_t *input)
{
  ngo_main_t *ngm = &ngo_main;
  u8 enable = 0;
  u32 v;

  while (unformat_check_input (input) != UNFORMAT_END_OF_INPUT)
    {
      if (unformat (input, "enable"))
	enable = 1;
      else if (unformat (input, "active-packets %u", &ngm->active_pkts))
	;
      else if (unformat (input, "sync-interval %f", &ngm->sync_interval))
	;
      else if (unformat (input, "sync-budget %u", &ngm->sync_budget))
	;
      else if (unformat (input, "queue-size %u", &ngm->max_queue))
	;
      else if (unformat (input, "max-retries %u", &v))
	ngm->max_retries = v;
      else if (unformat (input, "no-dec-ttl"))
	ngm->dec_ttl = 0;
      else if (unformat (input, "bulk-udp-port %u", &v) && v && v < 65536)
	vec_add1 (ngm->bulk_udp_ports, (u16) v);
      else
	return clib_error_return (0, "unknown input '%U'",
				  format_unformat_error, input);
    }
  ngm->enable_on_start = enable;
  return 0;
}

VLIB_CONFIG_FUNCTION (ngo_config, "natgw-offload");

static clib_error_t *
ngo_main_loop_enter (vlib_main_t *vm)
{
  ngo_main_t *ngm = &ngo_main;

  if (ngm->enable_on_start)
    return ngo_enable_disable (1);
  return 0;
}

VLIB_MAIN_LOOP_ENTER_FUNCTION (ngo_main_loop_enter);

/* ------------------------------------------------------------------ */
/* CLI */

u8 *
format_ngo_session (u8 *s, va_list *args)
{
  ngo_session_t *ngs = va_arg (*args, ngo_session_t *);
  const ngo_event_t *e = &ngs->snap;

  s = format (s, "%s %U:%u -> %U:%u as %U:%u  thread %u session %u",
	      e->proto == IP_PROTOCOL_TCP ? "tcp" : "udp", format_ip4_address,
	      &e->i2o.match.saddr, clib_net_to_host_u16 (e->i2o.match.sport),
	      format_ip4_address, &e->i2o.match.daddr,
	      clib_net_to_host_u16 (e->i2o.match.dport), format_ip4_address,
	      &e->i2o.rewrite.saddr,
	      clib_net_to_host_u16 ((e->i2o.ops & NAT_FLOW_OP_SPORT_REWRITE) ?
				      e->i2o.rewrite.sport :
				      e->i2o.match.sport),
	      e->thread_index, e->session_index);
  if (!ngs->flow[0])
    return format (s, "\n    pending (retries %u)", ngs->retries);
  return format (s,
		 "\n    i2o port %u -> %u %U  o2i port %u -> %u %U  pkts %llu "
		 "bytes %llu",
		 ngs->in_port[0], ngs->nh[0].port_id, format_ethernet_address,
		 ngs->nh[0].dst_mac, ngs->in_port[1], ngs->nh[1].port_id,
		 format_ethernet_address, ngs->nh[1].dst_mac, ngs->pkts,
		 ngs->bytes);
}

static clib_error_t *
ngo_enable_command_fn (vlib_main_t *vm, unformat_input_t *input,
		       vlib_cli_command_t *cmd)
{
  ngo_main_t *ngm = &ngo_main;
  int enable = -1;
  u32 v;

  while (unformat_check_input (input) != UNFORMAT_END_OF_INPUT)
    {
      if (unformat (input, "enable"))
	enable = 1;
      else if (unformat (input, "disable"))
	enable = 0;
      else if (unformat (input, "active-packets %u", &v))
	{
	  ngm->active_pkts = v;
	  if (ngm->enabled)
	    ngm->register_cb (ngo_session_event, v);
	}
      else if (unformat (input, "sync-interval %f", &ngm->sync_interval))
	;
      else if (unformat (input, "sync-budget %u", &ngm->sync_budget))
	;
      else
	return clib_error_return (0, "unknown input '%U'",
				  format_unformat_error, input);
    }
  if (enable >= 0)
    return ngo_enable_disable (enable);
  return 0;
}

VLIB_CLI_COMMAND (ngo_enable_command, static) = {
  .path = "natgw offload",
  .short_help = "natgw offload [enable|disable] [active-packets <n>] "
		"[sync-interval <sec>] [sync-budget <n>]",
  .function = ngo_enable_command_fn,
};

static clib_error_t *
ngo_sync_command_fn (vlib_main_t *vm, unformat_input_t *input,
		     vlib_cli_command_t *cmd)
{
  if (!ngo_main.enabled)
    return clib_error_return (0, "natgw offload is disabled");
  ngo_sync_now (vm);
  return 0;
}

VLIB_CLI_COMMAND (ngo_sync_command, static) = {
  .path = "natgw offload sync",
  .short_help = "natgw offload sync (drain events, retry, sync all sessions)",
  .function = ngo_sync_command_fn,
};

static clib_error_t *
ngo_show_command_fn (vlib_main_t *vm, unformat_input_t *input,
		     vlib_cli_command_t *cmd)
{
  ngo_main_t *ngm = &ngo_main;
  ngo_session_t *ngs;
  int sessions = 0;

  if (unformat (input, "sessions"))
    sessions = 1;
  vlib_cli_output (vm, "natgw offload: %s, active-packets %u, sync every %.2fs",
		   ngm->enabled ? "enabled" : "disabled", ngm->active_pkts,
		   ngm->sync_interval);
  vlib_cli_output (vm, "  offloaded sessions: %u", pool_elts (ngm->sessions) -
						      vec_len (ngm->pending));
  vlib_cli_output (vm, "  pending sessions: %u", vec_len (ngm->pending));
  for (int i = 0; i < NGO_N_CTR; i++)
    vlib_cli_output (vm, "  %s: %llu", ngo_counter_names[i], ngm->counters[i]);
  if (sessions)
    pool_foreach (ngs, ngm->sessions)
      vlib_cli_output (vm, "  %U", format_ngo_session, ngs);
  return 0;
}

VLIB_CLI_COMMAND (ngo_show_command, static) = {
  .path = "show natgw offload",
  .short_help = "show natgw offload [sessions]",
  .function = ngo_show_command_fn,
};

static clib_error_t *
ngo_clear_command_fn (vlib_main_t *vm, unformat_input_t *input,
		      vlib_cli_command_t *cmd)
{
  clib_memset (ngo_main.counters, 0, sizeof (ngo_main.counters));
  return 0;
}

VLIB_CLI_COMMAND (ngo_clear_command, static) = {
  .path = "clear natgw offload counters",
  .short_help = "clear natgw offload counters",
  .function = ngo_clear_command_fn,
};

VLIB_PLUGIN_REGISTER () = {
  .version = VPP_BUILD_VER,
  .description = "natgw FPGA offload of nat44-ed sessions",
};
