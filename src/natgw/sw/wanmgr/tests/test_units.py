# SPDX-License-Identifier: BSD-3-Clause
"""Health state machine, configuration and alerting."""

import pytest

from fakes import FakeClock
from smtpsink import SmtpSink
from wanmgr import config
from wanmgr.alerts import Alerter
from wanmgr.health import Health, State

# ---------------------------------------------------------------- health


def run(h, seq):
    return [(h.round(ok), h.state) for ok in seq][-1][1]


def test_health_initial_up_is_fast():
    h = Health(up_rounds=5, down_rounds=3, initial_rounds=1)
    assert h.round(True) == State.UNKNOWN and h.state == State.UP


def test_health_initial_down_needs_down_rounds():
    h = Health(5, 3, 1)
    assert run(h, [False, False]) == State.UNKNOWN
    assert run(h, [False]) == State.DOWN


def test_health_thresholds():
    h = Health(5, 3, 1)
    run(h, [True])
    assert run(h, [False, False, True, False, False]) == State.UP     # never 3 in a row
    assert run(h, [False]) == State.DOWN
    assert run(h, [True] * 4 + [False] + [True] * 4) == State.DOWN   # never 5 in a row
    assert run(h, [True]) == State.UP


def test_health_no_lease_and_reset():
    h = Health(5, 3, 1)
    run(h, [True])
    assert h.no_lease() == State.UP and h.state == State.DOWN
    assert h.no_lease() is None
    h.reset()
    assert h.state == State.UNKNOWN


def test_health_rejects_bad_thresholds():
    with pytest.raises(ValueError):
        Health(0, 3, 1)


# ---------------------------------------------------------------- config

GOOD = """
[vpp]
api_socket = "/tmp/api.sock"
[probe]
interval = 1.0
timeout = 0.25
[nat]
vrf_id = 0
[[wan]]
name = "a"
interface = "X"
probe_targets = ["10.0.0.1"]
[alerts]
recipients = ["x@y"]
"""


def test_config_good():
    c = config.parse(GOOD)
    assert c.api_socket == "/tmp/api.sock" and c.probe.interval == 1.0
    assert c.wans[0].probe_targets == ["10.0.0.1"] and c.wans[0].weight == 1
    assert c.alerts.recipients == ["x@y"] and c.alerts.max_per_hour == 10


@pytest.mark.parametrize("text,msg", [
    ("", "at least one"),
    (GOOD.replace("[nat]", "[natt]"), "unknown sections"),
    (GOOD.replace('name = "a"', 'name = "a"\ncolour = 1'), "unknown keys"),
    (GOOD.replace('"10.0.0.1"', '"10.0.0.300"'), "not permitted"),
    (GOOD.replace('["10.0.0.1"]', "[]"), "empty"),
    (GOOD.replace("timeout = 0.25", "timeout = 2.0"), "timeout < interval"),
    (GOOD + '[[wan]]\nname = "a"\ninterface = "Y"\nprobe_targets = ["10.0.0.2"]\n', "unique"),
    (GOOD + '[[wan]]\nname = "b"\ninterface = "Y"\nprobe_targets = ["10.0.0.1"]\n', "one WAN only"),
    (GOOD.replace('name = "a"', 'name = "a"\nweight = 0'), "weight"),
    ("[[wan]\n", ""),
])
def test_config_errors(text, msg):
    with pytest.raises(config.ConfigError, match=msg):
        config.parse(text)


# ---------------------------------------------------------------- alerts


def alerter(**kw):
    cfg = config.AlertConfig(recipients=["ops@example.net"], syslog=False, **kw)
    sent = []
    clock = FakeClock()
    return Alerter(cfg, clock=clock, sender=sent.append), sent, clock


def test_alert_rate_limit_and_coalescing():
    a, sent, clock = alerter(max_per_hour=2)
    a.alert("one", "1")
    a.alert("two", "2")
    a.alert("three", "3")
    a.alert("four", "4")
    assert [m["Subject"] for m in sent] == ["[natgw] one", "[natgw] two"]
    clock.t += 1800
    a.flush()
    assert len(sent) == 2, "still within the hour"
    clock.t += 1801
    a.flush()
    assert sent[2]["Subject"] == "[natgw] four (+1 earlier)"
    assert "three" in sent[2].get_content()


def test_alert_failure_does_not_raise():
    cfg = config.AlertConfig(recipients=["x@y"], syslog=False)

    def boom(msg):
        raise OSError("relay down")

    a = Alerter(cfg, sender=boom)
    a.alert("x", "y")
    assert a.failed == 1


def test_alert_without_recipients_only_logs():
    a = Alerter(config.AlertConfig(syslog=False), sender=lambda m: pytest.fail("mailed"))
    a.alert("x", "y")


def test_alert_smtp_delivery():
    sink = SmtpSink()
    try:
        cfg = config.AlertConfig(smtp_host="127.0.0.1", smtp_port=sink.port, sender="gw@test",
                                 recipients=["a@test", "b@test"], syslog=False)
        a = Alerter(cfg)
        a.start()
        a.alert("wan1 DOWN", "wan1 is DOWN")
        a.stop()
        assert a.sent == 1
        (rcpt, msg), = sink.messages
        assert rcpt == ["a@test", "b@test"]
        assert msg["Subject"] == "[natgw] wan1 DOWN" and msg["From"] == "gw@test"
        assert "wan1 is DOWN" in msg.get_payload()
    finally:
        sink.close()
