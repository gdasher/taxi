# SPDX-License-Identifier: BSD-3-Clause
"""Configuration (TOML).

    [vpp]
    api_socket = "/run/vpp/api.sock"
    api_dir = "/usr/share/vpp/api"

    [probe]
    interval = 2.0          # seconds between rounds
    timeout = 0.5           # per ping
    up_rounds = 5
    down_rounds = 3
    initial_rounds = 1
    lease_grace = 30.0      # seconds for DHCP to bind after (re)connect

    [nat]
    vrf_id = 0              # pool addresses are added with this tenant VRF

    [[wan]]
    name = "wan1"
    interface = "TwentyFiveGigabitEthernet3/0/0.101"
    probe_targets = ["192.0.2.53", "198.51.100.53"]
    weight = 1
    manage_dhcp = true      # create the interface's DHCP client if missing
    hostname = "natgw"

    [alerts]
    smtp_host = "localhost"
    smtp_port = 25
    starttls = false
    username = ""           # login when set
    password = ""
    sender = "natgw@example.net"
    recipients = ["ops@example.net"]
    max_per_hour = 10
    syslog = true
    hostname = "natgw"      # used in subjects
"""

import dataclasses
import ipaddress
import tomllib


class ConfigError(ValueError):
    pass


@dataclasses.dataclass
class WanConfig:
    name: str
    interface: str
    probe_targets: list
    weight: int = 1
    manage_dhcp: bool = True
    hostname: str = "natgw"


@dataclasses.dataclass
class ProbeConfig:
    interval: float = 2.0
    timeout: float = 0.5
    up_rounds: int = 5
    down_rounds: int = 3
    initial_rounds: int = 1
    lease_grace: float = 30.0   # seconds a WAN may lack a lease after (re)connect before it is DOWN


@dataclasses.dataclass
class AlertConfig:
    smtp_host: str = ""
    smtp_port: int = 25
    starttls: bool = False
    username: str = ""
    password: str = ""
    sender: str = "natgw-wanmgr@localhost"
    recipients: list = dataclasses.field(default_factory=list)
    max_per_hour: int = 10
    syslog: bool = True
    hostname: str = "natgw"
    timeout: float = 10.0


@dataclasses.dataclass
class Config:
    wans: list
    probe: ProbeConfig
    alerts: AlertConfig
    api_socket: str = "/run/vpp/api.sock"
    api_dir: str = "/usr/share/vpp/api"
    nat_vrf_id: int = 0


def _section(d, cls, name):
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(d) - known
    if unknown:
        raise ConfigError(f"[{name}]: unknown keys {sorted(unknown)}")
    try:
        return cls(**d)
    except TypeError as e:
        raise ConfigError(f"[{name}]: {e}") from None


def parse(text):
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(str(e)) from None
    unknown = set(raw) - {"vpp", "probe", "nat", "wan", "alerts"}
    if unknown:
        raise ConfigError(f"unknown sections {sorted(unknown)}")
    vpp = raw.get("vpp", {})
    if set(vpp) - {"api_socket", "api_dir"}:
        raise ConfigError(f"[vpp]: unknown keys {sorted(set(vpp) - {'api_socket', 'api_dir'})}")
    nat = raw.get("nat", {})
    if set(nat) - {"vrf_id"}:
        raise ConfigError("[nat]: only vrf_id is supported")
    probe = _section(raw.get("probe", {}), ProbeConfig, "probe")
    if probe.interval <= 0 or probe.timeout <= 0 or probe.timeout >= probe.interval:
        raise ConfigError("[probe]: need 0 < timeout < interval")
    alerts = _section(raw.get("alerts", {}), AlertConfig, "alerts")
    if alerts.max_per_hour < 1:
        raise ConfigError("[alerts]: max_per_hour must be >= 1")
    wans = [_section(w, WanConfig, "wan") for w in raw.get("wan", [])]
    if not wans:
        raise ConfigError("at least one [[wan]] is required")
    names = [w.name for w in wans]
    if len(set(names)) != len(names):
        raise ConfigError("WAN names must be unique")
    if len({w.interface for w in wans}) != len(wans):
        raise ConfigError("WAN interfaces must be unique")
    for w in wans:
        if not w.probe_targets:
            raise ConfigError(f"wan {w.name}: probe_targets is empty")
        try:
            w.probe_targets = [str(ipaddress.IPv4Address(t)) for t in w.probe_targets]
        except ValueError as e:
            raise ConfigError(f"wan {w.name}: {e}") from None
        if not 1 <= w.weight <= 255:
            raise ConfigError(f"wan {w.name}: weight must be 1..255")
    targets = [t for w in wans for t in w.probe_targets]
    if len(set(targets)) != len(targets):
        # each target is pinned to one WAN by a /32 route
        raise ConfigError("a probe target can belong to one WAN only")
    return Config(wans=wans, probe=probe, alerts=alerts, api_socket=vpp.get("api_socket", Config.api_socket),
                  api_dir=vpp.get("api_dir", Config.api_dir), nat_vrf_id=nat.get("vrf_id", 0))


def load(path):
    with open(path) as f:
        return parse(f.read())
