/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * net_natgw_model: a DPDK virtual device that puts the natgw shim software
 * model behind ethdev ports, one port per lane, with the same rte_flow NAT
 * offload as the cndm PMD on real hardware.
 *
 *   --vdev net_natgw_model0,lanes=8,bucket_w=10,wire=queue|tap,tap_prefix=ngw,
 *          punt_hdr=1,clock=wall|manual[,ddr_bucket_w=<5-20>,ddr_calib=0|1,no_ddr=0|1]
 *
 * The tier policy options of the flow layer (tier_policy, onchip_high,
 * onchip_low, demote_idle, promote_k, promote_n, migrate_budget,
 * min_residency; see natgw_flow.h) are accepted too.
 *
 * ddr_bucket_w gives the model a DDR tier (2^(ddr_bucket_w+2) entries);
 * ddr_calib=0 models that tier with no working DIMM, and no_ddr=1 tells the
 * flow layer not to use it.
 *
 * The "wire" is what the MACs would see. With wire=queue, the test API below
 * injects and collects wire frames; with wire=tap, each lane is a Linux TAP
 * interface <tap_prefix><lane>.
 */

#ifndef RTE_PMD_NATGW_MODEL_H
#define RTE_PMD_NATGW_MODEL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* a frame arrives on the wire of <port_id>'s lane (wire=queue); 0 or -errno */
int rte_pmd_natgw_model_wire_inject(uint16_t port_id, const void *frame, uint16_t len);

/* next frame sent on the wire of <port_id>'s lane (wire=queue): its length,
 * 0 when none, or -errno; *forwarded is set when the shim forwarded it (as
 * opposed to host transmit) */
int rte_pmd_natgw_model_wire_recv(uint16_t port_id, void *buf, uint16_t cap, int *forwarded);

/* advance the model's tick counter and run an aging scan (clock=manual) */
int rte_pmd_natgw_model_advance(uint16_t port_id, uint32_t ticks);

/* process pending wire input for every lane of the device now
 * (normally done inside rx_burst); returns frames processed */
int rte_pmd_natgw_model_service(uint16_t port_id);

/* drain hardware events and update aging now (normally a 100 ms alarm) */
int rte_pmd_natgw_model_poll(uint16_t port_id);

/* test probe: after every table command written to the device (entry or
 * DDR entry write, clear, read), run <frame> through the model on
 * <port_id>'s lane and count the outcome: forwarded, punted, or forwarded
 * with bytes differing from the first forwarded copy. len 0 disables it and
 * every set resets the counts. */
int rte_pmd_natgw_model_probe_set(uint16_t port_id, const void *frame, uint16_t len);
int rte_pmd_natgw_model_probe_get(uint16_t port_id, uint64_t *fwd, uint64_t *punt, uint64_t *diff);

#ifdef __cplusplus
}
#endif

#endif
