# DPDK-level tests of the natgw rte_flow offload against the net_natgw_model PMD.
# Requires DPDK (with the common_natgw and net_natgw_model drivers) installed.
BUILD ?= build
CFLAGS += -DALLOW_EXPERIMENTAL_API -O2 -g -Wall -Wextra -Werror $(shell pkg-config --cflags libdpdk)
LDLIBS += $(shell pkg-config --libs libdpdk) -lrte_net_natgw_model

all: $(BUILD)/test_natgw_pmd

$(BUILD)/test_natgw_pmd: test_natgw_pmd.c
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f $(BUILD)/test_natgw_pmd

.PHONY: all clean
