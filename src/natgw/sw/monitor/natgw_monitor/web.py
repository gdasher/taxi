# SPDX-License-Identifier: BSD-3-Clause
"""HTTP(S) server: the page and its JSON API.

    GET /                  the page (static/index.html, app.js, style.css)
    GET /api/stats         the current sample: traffic, tables, NAT, VPP, ISPs, alerts, outages
    GET /api/history?seconds=600   samples for the graphs (at most ~600 points)
    GET /api/events?limit=200      the event log (WAN, outage, alert, VPP events)
    GET /healthz           {"ok": true} while the collector is sampling

One listening socket per bound address. [http] bind takes addresses and
interface names; an interface name serves on every address the interface has
when the monitor starts (it waits for the interface to get one). With TLS (the
default) only HTTPS is served; TLS 1.2 is the minimum and SIGHUP reloads the
certificate and key (for renewals). The handshake runs in the connection's own
thread, so a slow client cannot hold up the others."""

import http.server
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse

from .config import is_address

log = logging.getLogger("natgw-monitor")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/index.html": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8")}
CONN_TIMEOUT = 30


def interface_addresses(name):
    """the IPv4 and IPv6 addresses of an interface ([] when it has none)"""
    r = subprocess.run(["ip", "-j", "addr", "show", "dev", name], capture_output=True, text=True)
    if r.returncode:
        raise LookupError(r.stderr.strip() or f"no interface {name}")
    out = []
    for link in json.loads(r.stdout or "[]"):
        for a in link.get("addr_info", []):
            if a.get("scope") == "link" and a.get("family") == "inet6":
                continue    # link-local needs a scope id; not useful for a web page
            out.append(a["local"])
    return out


def resolve_binds(binds):
    """addresses to listen on; raises LookupError when an interface has none yet"""
    out = []
    for b in binds:
        if is_address(b):
            out.append(b)
            continue
        addrs = interface_addresses(b)
        if not addrs:
            raise LookupError(f"interface {b} has no address yet")
        out.extend(addrs)
    return list(dict.fromkeys(out))


def tls_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "natgw-monitor"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = CONN_TIMEOUT

    def version_string(self):
        return self.server_version

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(CONN_TIMEOUT)
            try:
                self.request.do_handshake()
            except (ssl.SSLError, OSError) as e:
                log.debug("TLS handshake from %s: %s", self.client_address[0], e)
                raise ConnectionAbortedError from None
        super().setup()

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, ssl.SSLError, TimeoutError):
            pass

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def _send(self, code, body, ctype, extra=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if self.server.tls:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False, default=str),
                   "application/json")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        col = self.server.collector
        if u.path in FILES:
            name, ctype = FILES[u.path]
            with open(os.path.join(STATIC, name), "rb") as f:
                return self._send(200, f.read(), ctype)
        if u.path == "/api/stats":
            cur = col.snapshot()
            if cur is None:
                return self._json({"error": "starting"}, 503)
            return self._json(dict(cur, hostname=self.server.hostname))
        if u.path == "/api/history":
            try:
                seconds = min(max(float(q.get("seconds", ["600"])[0]), 10), 86400)
            except ValueError:
                return self._json({"error": "seconds: a number"}, 400)
            return self._json({"samples": col.history_since(seconds)})
        if u.path == "/api/events":
            try:
                limit = min(max(int(q.get("limit", ["200"])[0]), 1), 1000)
            except ValueError:
                return self._json({"error": "limit: an integer"}, 400)
            return self._json({"events": col.events.tail(limit)})
        if u.path == "/healthz":
            cur = col.snapshot()
            fresh = cur is not None and time.time() - cur["ts"] < 10 * col.cfg.alerts.sample_interval + 5
            return self._json({"ok": fresh}, 200 if fresh else 503)
        self._json({"error": "not found"}, 404)

    def _not_allowed(self):
        self._send(405, "method not allowed\n", "text/plain", [("Allow", "GET, HEAD")])

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, addr, collector, ctx, hostname):
        self.address_family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        self.collector = collector
        self.ctx = ctx
        self.tls = ctx is not None
        self.hostname = hostname
        super().__init__(addr, Handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        log.debug("connection from %s failed", client_address[0],
                  exc_info=not isinstance(sys.exc_info()[1], ConnectionAbortedError))

    def get_request(self):
        sock, addr = self.socket.accept()
        if self.ctx is not None:
            # handshake later, in the connection's thread (Handler.setup)
            sock = self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, addr


class Web:
    """the listening sockets of one configuration"""

    def __init__(self, cfg, collector, hostname=None):
        self.cfg = cfg
        self.collector = collector
        self.hostname = hostname or socket.gethostname()
        self.ctx = tls_context(cfg.tls.cert, cfg.tls.key) if cfg.tls.enabled else None
        self.servers = []
        self.threads = []

    def reload_tls(self):
        if self.ctx is None:
            return
        try:
            self.ctx.load_cert_chain(self.cfg.tls.cert, self.cfg.tls.key)
            log.info("reloaded the TLS certificate")
        except (OSError, ssl.SSLError) as e:
            log.error("TLS reload failed, keeping the old certificate: %s", e)

    def start(self, stop, retry=2.0):
        """bind every address (waiting for interfaces to get theirs), then serve"""
        while True:
            try:
                addrs = resolve_binds(self.cfg.http.bind)
                for a in addrs:
                    self.servers.append(Server((a, self.cfg.http.port), self.collector, self.ctx, self.hostname))
                break
            except (LookupError, OSError) as e:
                for s in self.servers:
                    s.server_close()
                self.servers = []
                log.warning("cannot listen yet (%s); retrying", e)
                if stop.wait(retry):
                    return False
        for s in self.servers:
            t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
            t.start()
            self.threads.append(t)
            host, port = s.server_address[:2]
            log.info("serving %s://%s:%d/", "https" if self.ctx else "http",
                     f"[{host}]" if ":" in host else host, port)
        return True

    def addresses(self):
        return [s.server_address[:2] for s in self.servers]

    def stop(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
