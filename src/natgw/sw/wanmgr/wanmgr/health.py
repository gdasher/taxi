# SPDX-License-Identifier: BSD-3-Clause
"""Per-WAN health state machine.

A WAN is UNKNOWN until probed (after start or a VPP reconnect). A probe
round fails when no target replies. DOWN after `down_rounds` consecutive
failed rounds, UP after `up_rounds` consecutive good rounds. From UNKNOWN,
`initial_rounds` good rounds are enough for UP, so a restart re-converges
quickly. A WAN without a lease is DOWN at once: it has no gateway."""

import enum


class State(enum.Enum):
    UNKNOWN = "unknown"
    UP = "up"
    DOWN = "down"


class Health:
    def __init__(self, up_rounds=5, down_rounds=3, initial_rounds=1):
        if min(up_rounds, down_rounds, initial_rounds) < 1:
            raise ValueError("round thresholds must be >= 1")
        self.up_rounds = up_rounds
        self.down_rounds = down_rounds
        self.initial_rounds = initial_rounds
        self.reset()

    def reset(self):
        self.state = State.UNKNOWN
        self.good = 0
        self.bad = 0

    def no_lease(self):
        """returns the previous state when it changed"""
        prev = self.state
        self.state, self.good, self.bad = State.DOWN, 0, 0
        return prev if prev != self.state else None

    def round(self, ok):
        """record one probe round; returns the previous state on a change"""
        if ok:
            self.good, self.bad = self.good + 1, 0
        else:
            self.good, self.bad = 0, self.bad + 1
        prev = self.state
        if self.state == State.UNKNOWN:
            if self.good >= self.initial_rounds:
                self.state = State.UP
            elif self.bad >= self.down_rounds:
                self.state = State.DOWN
        elif self.state == State.UP:
            if self.bad >= self.down_rounds:
                self.state = State.DOWN
        elif self.good >= self.up_rounds:
            self.state = State.UP
        return prev if prev != self.state else None
