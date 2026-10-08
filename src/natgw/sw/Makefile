# libnatgw: host library and software model of the natgw shim
#   make            build/libnatgw.a, build/libnatgw.so (library + model)
#   make test       C sanity checks and the pytest suite (needs python3, pytest, scapy)

CC      ?= cc
CFLAGS  ?= -O2 -g
CFLAGS  += -std=c11 -Wall -Wextra -Werror -Wshadow -Wconversion -Wno-sign-conversion -fPIC -Iinclude
SRCS    := lib/natgw.c model/natgw_model.c
OBJS    := $(SRCS:%.c=build/%.o)
PYTHON  ?= python3

all: build/libnatgw.a build/libnatgw.so

build/%.o: %.c include/natgw.h include/natgw_model.h
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c -o $@ $<

build/libnatgw.a: $(OBJS)
	ar rcs $@ $^

build/libnatgw.so: $(OBJS)
	$(CC) -shared -o $@ $^

test: all
	$(PYTHON) -m pytest -q tests

clean:
	rm -rf build

.PHONY: all test clean
