# SPDX-License-Identifier: BSD-3-Clause
"""natgw-wanmgr daemon: python3 -m wanmgr -c /etc/natgw/wanmgr.toml"""

import argparse
import logging
import signal
import sys

from . import config as config_mod
from .alerts import Alerter
from .manager import WanManager
from .vpp import VppApi


def main(argv=None):
    ap = argparse.ArgumentParser(prog="natgw-wanmgr", description=__doc__)
    ap.add_argument("-c", "--config", default="/etc/natgw/wanmgr.toml")
    ap.add_argument("--status-file", help="write the manager's state as JSON after every round")
    ap.add_argument("--check", action="store_true", help="validate the configuration and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = config_mod.load(a.config)
    except (OSError, config_mod.ConfigError) as e:
        print(f"natgw-wanmgr: {a.config}: {e}", file=sys.stderr)
        return 2
    if a.check:
        print(f"{a.config}: OK ({len(cfg.wans)} WANs)")
        return 0

    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    alerter = Alerter(cfg.alerts)
    alerter.start()
    mgr = WanManager(cfg, VppApi(cfg.api_socket, cfg.api_dir), alerter)
    mgr.status_file = a.status_file
    try:
        mgr.run(stop=lambda: bool(stopping))
    finally:
        mgr.vpp.disconnect()
        alerter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
