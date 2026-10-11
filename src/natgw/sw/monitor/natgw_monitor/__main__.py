# SPDX-License-Identifier: BSD-3-Clause
"""natgw-monitor daemon: python3 -m natgw_monitor -c /etc/natgw/monitor.toml"""

import argparse
import faulthandler
import logging
import signal
import ssl
import sys
import threading

from . import config as config_mod
from .collector import Collector
from .sources import StatsSource, WanSource
from .web import Web, tls_context


def main(argv=None):
    ap = argparse.ArgumentParser(prog="natgw-monitor", description=__doc__)
    ap.add_argument("-c", "--config", default="/etc/natgw/monitor.toml")
    ap.add_argument("--check", action="store_true", help="validate the configuration (and TLS files) and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = config_mod.load(a.config)
    except (OSError, config_mod.ConfigError) as e:
        print(f"natgw-monitor: {a.config}: {e}", file=sys.stderr)
        return 2
    if cfg.tls.enabled:
        try:
            tls_context(cfg.tls.cert, cfg.tls.key)
        except (OSError, ssl.SSLError) as e:   # missing files, bad PEM, key not the certificate's
            print(f"natgw-monitor: TLS certificate {cfg.tls.cert}, key {cfg.tls.key}: {e}", file=sys.stderr)
            return 2
    if a.check:
        print(f"{a.config}: OK ({'https' if cfg.tls.enabled else 'http'} on "
              f"{', '.join(cfg.http.bind)} port {cfg.http.port})")
        return 0

    faulthandler.register(signal.SIGUSR1)   # kill -USR1: every thread's stack to the log
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    col = Collector(cfg, StatsSource(cfg.sources.stats_socket), WanSource(cfg.sources.wanmgr_status))
    web = Web(cfg, col)
    signal.signal(signal.SIGHUP, lambda *_: web.reload_tls())
    sampler = threading.Thread(target=col.run, args=(stop,), daemon=True)
    sampler.start()
    if web.start(stop):
        stop.wait()
        web.stop()
    sampler.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
