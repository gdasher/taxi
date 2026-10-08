/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * NAT gateway shim (natgw) support: the NAT register window in BAR0, the
 * rte_flow NAT offload (natgw/dpdk/common/natgw_flow.c) and punt metadata.
 * A bitstream without the NAT block leaves the driver unchanged.
 */

#include <bus_pci_driver.h>
#include <ethdev_driver.h>
#include <rte_alarm.h>
#include <rte_io.h>

#include "cndm.h"

#define NATGW_POLL_US     100000
#define NATGW_TICK_HZ     1000
#define NATGW_TICK_DIV    250000     /* 250 MHz core clock / 1000 */

static uint32_t natgw_io_rd(void *ctx, uint32_t off)
{
	struct cndm_dev *cdev = ctx;

	return rte_read32(cdev->hw_addr + NATGW_BAR_OFFSET + off);
}

static void natgw_io_wr(void *ctx, uint32_t off, uint32_t val)
{
	struct cndm_dev *cdev = ctx;

	rte_write32(val, cdev->hw_addr + NATGW_BAR_OFFSET + off);
}

static void cndm_natgw_alarm(void *arg)
{
	struct cndm_dev *cdev = arg;

	if (!cdev->natgw)
		return;
	natgw_flow_poll(cdev->natgw);
	rte_eal_alarm_set(NATGW_POLL_US, cndm_natgw_alarm, cdev);
}

int cndm_natgw_init(struct cndm_dev *cdev)
{
	struct natgw_io io = { natgw_io_rd, natgw_io_wr, cdev };
	struct natgw_flow_cfg cfg;
	unsigned lanes;
	int ret;

	if (cdev->hw_regs_size < NATGW_BAR_OFFSET + 0x2000 ||
	    natgw_io_rd(cdev, NATGW_REG_ID) != NATGW_ID) {
		DRV_LOG(NOTICE, "No NAT block in this bitstream");
		return 0;
	}
	ret = natgw_punt_meta_register();
	if (ret)
		return ret;

	lanes = RTE_MIN(cdev->port_count, (__u32)NATGW_LANES);
	memset(&cfg, 0, sizeof(cfg));
	cfg.lanes = lanes;
	cfg.ticks_per_sec = NATGW_TICK_HZ;
	cfg.tick_div = NATGW_TICK_DIV;
	cfg.seed0 = 0xffffffff;
	cfg.seed1 = 0xffffffff;
	cfg.max_depth = 6;
	cfg.punt_hdr = cdev->natgw_punt_hdr;
	cdev->natgw = natgw_flow_ctx_create(&io, &cfg, cdev->pdev->device.numa_node);
	if (!cdev->natgw) {
		DRV_LOG(ERR, "NAT block present but could not be initialised");
		return -EIO;
	}
	for (unsigned k = 0; k < lanes; k++)
		if (cdev->eth_dev[k])
			natgw_flow_bind_port(cdev->natgw, k, cdev->eth_dev[k]->data->port_id);
	rte_eal_alarm_set(NATGW_POLL_US, cndm_natgw_alarm, cdev);
	DRV_LOG(NOTICE, "NAT block: %u lanes, %u entries, punt header %s", lanes,
		natgw_flow_capacity(cdev->natgw), cfg.punt_hdr ? "on" : "off");
	return 0;
}

void cndm_natgw_remove(struct cndm_dev *cdev)
{
	if (!cdev->natgw)
		return;
	rte_eal_alarm_cancel(cndm_natgw_alarm, cdev);
	for (unsigned k = 0; k < cdev->port_count && k < NATGW_LANES; k++)
		if (cdev->eth_dev[k])
			natgw_flow_unbind_port(cdev->eth_dev[k]->data->port_id);
	natgw_flow_ctx_destroy(cdev->natgw);
	cdev->natgw = NULL;
}

void cndm_natgw_port_started(struct cndm_dev *cdev, bool started)
{
	if (!cdev->natgw)
		return;
	if (started) {
		if (cdev->natgw_started++ == 0)
			natgw_flow_enable(cdev->natgw, true);
	} else if (cdev->natgw_started > 0 && --cdev->natgw_started == 0) {
		natgw_flow_enable(cdev->natgw, false);
	}
}
