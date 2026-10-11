# SPDX-License-Identifier: BSD-3-Clause
"""Configuration (TOML).

    [http]
    bind = ["127.0.0.1"]      # addresses and/or interface names (enp1s0):
                              # an interface serves on each of its addresses
    port = 8443

    [tls]                     # HTTPS (the default); plain HTTP only with enabled = false
    enabled = true
    cert = "/etc/natgw/tls/monitor.crt"   # PEM, may include the chain
    key = "/etc/natgw/tls/monitor.key"

    [sources]
    stats_socket = "/run/vpp/stats.sock"
    wanmgr_status = "/run/natgw-wanmgr.json"

    [store]
    state_dir = "/var/lib/natgw-monitor"   # monitor.db: statistics, events, outages
    raw_hours = 24                         # every 1 s sample kept this long
    retention_days = 7                     # minute rollups, counters and events
    flush_seconds = 10                     # samples written in batches this often
    history_seconds = 3600                 # also kept in memory (exact, newest)

    [alerts]
    sample_interval = 1.0
    hold_seconds = 300        # a counter-rise alert stays this long after the last rise
    onchip_fill_pct = 85      # warn when the on-chip table stays above this ...
    onchip_fill_seconds = 60  # ... for this long
    wanmgr_stale_seconds = 15 # WAN manager status older than this: not running
"""

import dataclasses
import ipaddress
import tomllib


class ConfigError(ValueError):
    pass


@dataclasses.dataclass
class HttpConfig:
    bind: list = dataclasses.field(default_factory=lambda: ["127.0.0.1"])
    port: int = 8443


@dataclasses.dataclass
class TlsConfig:
    enabled: bool = True
    cert: str = "/etc/natgw/tls/monitor.crt"
    key: str = "/etc/natgw/tls/monitor.key"


@dataclasses.dataclass
class SourcesConfig:
    stats_socket: str = "/run/vpp/stats.sock"
    wanmgr_status: str = "/run/natgw-wanmgr.json"


@dataclasses.dataclass
class StoreConfig:
    state_dir: str = "/var/lib/natgw-monitor"
    raw_hours: float = 24.0
    retention_days: float = 7.0
    flush_seconds: float = 10.0
    history_seconds: int = 3600


@dataclasses.dataclass
class AlertsConfig:
    sample_interval: float = 1.0
    hold_seconds: float = 300.0
    onchip_fill_pct: float = 85.0
    onchip_fill_seconds: float = 60.0
    wanmgr_stale_seconds: float = 15.0


@dataclasses.dataclass
class Config:
    http: HttpConfig
    tls: TlsConfig
    sources: SourcesConfig
    store: StoreConfig
    alerts: AlertsConfig


SECTIONS = {"http": HttpConfig, "tls": TlsConfig, "sources": SourcesConfig, "store": StoreConfig,
            "alerts": AlertsConfig}


def _section(d, cls, name):
    if not isinstance(d, dict):
        raise ConfigError(f"[{name}] must be a table")
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(d) - known
    if unknown:
        raise ConfigError(f"[{name}]: unknown keys {sorted(unknown)}")
    return cls(**d)


def is_address(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def parse(text):
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(str(e)) from None
    unknown = set(raw) - set(SECTIONS)
    if unknown:
        raise ConfigError(f"unknown sections {sorted(unknown)}")
    cfg = Config(**{n: _section(raw.get(n, {}), cls, n) for n, cls in SECTIONS.items()})
    h = cfg.http
    if isinstance(h.bind, str):
        h.bind = [h.bind]
    if not h.bind or not all(isinstance(b, str) and b for b in h.bind):
        raise ConfigError("[http] bind: a list of addresses or interface names")
    for b in h.bind:
        if not is_address(b) and (len(b) > 15 or "/" in b or " " in b):
            raise ConfigError(f"[http] bind: {b!r} is neither an IP address nor an interface name")
    if not isinstance(h.port, int) or not 1 <= h.port <= 65535:
        raise ConfigError("[http] port must be 1..65535")
    if cfg.tls.enabled and not (cfg.tls.cert and cfg.tls.key):
        raise ConfigError("[tls] cert and key are required (or enabled = false for plain HTTP)")
    a = cfg.alerts
    if a.sample_interval <= 0 or a.hold_seconds < 0 or a.wanmgr_stale_seconds <= 0:
        raise ConfigError("[alerts]: intervals must be positive")
    if not 0 < a.onchip_fill_pct <= 100:
        raise ConfigError("[alerts] onchip_fill_pct must be 1..100")
    st = cfg.store
    if st.history_seconds < 60:
        raise ConfigError("[store] history_seconds must be at least 60")
    if not 0 < st.raw_hours <= 24 * st.retention_days:
        raise ConfigError("[store] raw_hours must be positive and within retention_days")
    if not 0 < st.retention_days <= 366 or not 0 < st.flush_seconds <= 300:
        raise ConfigError("[store] retention_days must be 1..366 days, flush_seconds 1..300")
    return cfg


def load(path):
    with open(path) as f:
        return parse(f.read())
