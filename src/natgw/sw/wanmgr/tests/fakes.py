# SPDX-License-Identifier: BSD-3-Clause
"""Test doubles: a fake VPP with the adapter's interface, and a clock."""

from wanmgr.manager import Lease, VppDisconnected


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.t += max(d, 0)


class FakeVpp:
    """Routes, NAT pool, DHCP clients and pings, with routing semantics: a
    ping to a target succeeds only when the target's /32 (or the default)
    route resolves via a gateway that is alive and can reach the target."""

    def __init__(self, interfaces, clients=None):
        self.ifs = dict(interfaces)              # name -> sw_if_index
        self.clients = dict(clients or {})       # sw_if_index -> Lease | None
        self.routes = {}                         # prefix -> [(sw, gw, w)] (API source)
        self.pool = set()
        self.alive_gws = set()                   # gateways that forward
        self.reachable = {}                      # gateway -> targets it reaches (None: all)
        self.log = []                            # every change, in order
        self.flushed = []                        # pool addresses removed
        self.connected = False
        self.fail_after = None                   # raise VppDisconnected after n more calls
        self.pings = []

    # fault injection
    def _check(self):
        if not self.connected:
            raise VppDisconnected("not connected")
        if self.fail_after is not None:
            if self.fail_after <= 0:
                self.connected = False
                self.fail_after = None
                raise VppDisconnected("connection reset")
            self.fail_after -= 1

    def restart(self, keep_clients=True):
        """VPP restarts: API state is gone, DHCP clients rebind later"""
        self.connected = False
        self.routes = {}
        self.pool = set()
        self.clients = {s: None for s in self.clients} if keep_clients else {}

    # adapter interface
    def connect(self):
        if getattr(self, "down", False):
            raise VppDisconnected("connection refused")
        self.connected = True

    def disconnect(self):
        self.connected = False

    def interfaces(self):
        self._check()
        return dict(self.ifs)

    def dhcp_clients(self):
        self._check()
        return dict(self.clients)

    def nat_pool(self):
        self._check()
        return set(self.pool)

    def add_dhcp_client(self, sw_if_index, hostname):
        self._check()
        self.clients.setdefault(sw_if_index, None)
        self.log.append(("dhcp_client", sw_if_index, hostname))

    def route_set(self, prefix, paths):
        self._check()
        self.routes[prefix] = list(paths)
        self.log.append(("route_set", prefix, tuple(paths)))

    def route_del(self, prefix):
        self._check()
        self.routes.pop(prefix, None)
        self.log.append(("route_del", prefix))

    def nat_pool_add(self, address, vrf_id):
        self._check()
        self.pool.add(address)
        self.log.append(("pool_add", address, vrf_id))

    def nat_pool_del(self, address, vrf_id):
        self._check()
        self.pool.discard(address)
        self.flushed.append(address)
        self.log.append(("pool_del", address, vrf_id))

    def ping(self, address, timeout):
        self._check()
        self.pings.append(address)
        paths = self.routes.get(f"{address}/32") or self.routes.get("0.0.0.0/0") or []
        for sw, gw, _ in paths[:1]:
            # VPP's own probes pass NAT's output feature: without the egress
            # WAN's address in the pool they leave with another WAN's
            # address (or none) and the reply never comes back
            own = self.clients.get(sw)
            if own is None or own.address not in self.pool:
                return False
            reach = self.reachable.get(gw)
            if gw in self.alive_gws and (reach is None or address in reach):
                return True
        return False

    # helpers
    def changes(self, kind=None):
        return [e for e in self.log if kind is None or e[0] == kind]


def lease(addr, gw, plen=24):
    return Lease(addr, plen, gw)
