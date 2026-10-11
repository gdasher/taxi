# SPDX-License-Identifier: BSD-3-Clause
"""The HTTP(S) server: TLS, binding, API, headers, robustness."""

import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time

import pytest

from certs import free_port, make_cert
from monfakes import Clock, FakeStats, FakeWans
from natgw_monitor import config
from natgw_monitor.collector import Collector
from natgw_monitor.web import Web, interface_addresses

MONITOR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Server:
    def __init__(self, tmp_path, bind=('"127.0.0.1"',), tls=True, port=None):
        self.crt, self.key = make_cert(str(tmp_path), "monitor")
        self.port = port or free_port()
        self.cfg = config.parse(f"""
[http]
bind = [{", ".join(bind)}]
port = {self.port}
[tls]
enabled = {"true" if tls else "false"}
cert = "{self.crt}"
key = "{self.key}"
[store]
state_dir = "{tmp_path}/state"
""")
        clock = Clock(time.time())
        self.stats, self.wans = FakeStats(), FakeWans(clock)
        self.col = Collector(self.cfg, self.stats, self.wans, clock=clock)
        self.col.step()
        self.web = Web(self.cfg, self.col, hostname="natgw-test")
        self.stop = threading.Event()
        self.started = None
        self.t = threading.Thread(target=self._start, daemon=True)
        self.t.start()

    def _start(self):
        self.started = self.web.start(self.stop, retry=0.1)

    def wait(self):
        deadline = time.time() + 5
        while self.started is None and time.time() < deadline:
            time.sleep(0.02)
        assert self.started, "server did not start"

    def ctx(self):
        c = ssl.create_default_context(cafile=self.crt)
        return c

    def get(self, path, host="127.0.0.1", method="GET"):
        conn = http.client.HTTPSConnection(host, self.port, context=self.ctx(), timeout=5)
        conn.request(method, path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r, body

    def close(self):
        self.stop.set()
        self.t.join(timeout=5)
        self.web.stop()


@pytest.fixture
def srv(tmp_path):
    s = Server(tmp_path)
    s.wait()
    yield s
    s.close()


def test_https_page_and_api(srv):
    r, body = srv.get("/")
    assert r.status == 200 and b'id="path"' in body and r.getheader("Content-Type").startswith("text/html")
    for f in ("/app.js", "/charts.js", "/style.css", "/fonts/OFL-AtkinsonHyperlegibleNext.txt"):
        assert srv.get(f)[0].status == 200
    r, body = srv.get("/fonts/atkinson-hyperlegible-next.woff2")
    assert r.status == 200 and r.getheader("Content-Type") == "font/woff2" and body[:4] == b"wOF2"
    r, body = srv.get("/api/counters")
    assert r.status == 200 and "names" in json.loads(body)
    assert srv.get("/api/counter?name=nope")[0].status == 404
    assert "storage" in json.loads(srv.get("/api/stats")[1])
    r, body = srv.get("/api/stats")
    st = json.loads(body)
    assert r.status == 200 and st["hostname"] == "natgw-test" and {w["name"] for w in st["wans"]} == {"wan1", "wan2"}
    r, body = srv.get("/api/history?seconds=60")
    assert r.status == 200 and len(json.loads(body)["samples"]) == 1
    r, body = srv.get("/api/events?limit=5")
    assert json.loads(body)["events"][-1]["type"] == "monitor"
    assert srv.get("/healthz")[0].status == 200
    assert srv.get("/api/history?seconds=abc")[0].status == 400


def test_security_headers(srv):
    r, _ = srv.get("/")
    assert "default-src 'self'" in r.getheader("Content-Security-Policy")
    assert r.getheader("Strict-Transport-Security") and r.getheader("X-Frame-Options") == "DENY"
    assert r.getheader("X-Content-Type-Options") == "nosniff" and r.getheader("Cache-Control") == "no-store"
    assert r.getheader("Server") == "natgw-monitor"


def test_only_reads(srv):
    for m in ("POST", "PUT", "DELETE"):
        r, _ = srv.get("/api/stats", method=m)
        assert r.status == 405 and r.getheader("Allow") == "GET, HEAD"
    assert srv.get("/../natgw_monitor/config.py")[0].status == 404
    assert srv.get("/static/../config.py")[0].status == 404
    r, body = srv.get("/", method="HEAD")
    assert r.status == 200 and body == b""


def test_plain_http_and_old_tls_are_refused(srv):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=3)
    with pytest.raises((http.client.HTTPException, ConnectionError, OSError)):
        conn.request("GET", "/")
        conn.getresponse().read()
    c = srv.ctx()
    c.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    with pytest.warns(DeprecationWarning):
        c.maximum_version = ssl.TLSVersion.TLSv1_1
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        with socket.create_connection(("127.0.0.1", srv.port), timeout=3) as s:
            with c.wrap_socket(s, server_hostname="127.0.0.1"):
                pass
    assert srv.get("/healthz")[0].status == 200, "still serving"


def test_slow_client_does_not_block_others(srv):
    idle = socket.create_connection(("127.0.0.1", srv.port))   # never says hello
    try:
        t = time.time()
        assert srv.get("/api/stats")[0].status == 200
        assert time.time() - t < 2
    finally:
        idle.close()


def test_certificate_reload(srv, tmp_path):
    def cert_cn():
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", srv.port), timeout=3) as s:
            with c.wrap_socket(s) as t:
                der = t.getpeercert(binary_form=True)
        out = subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-subject"], input=der,
                             capture_output=True, check=True).stdout
        return out.decode().strip()
    assert "natgw-test" in cert_cn()
    crt, key = make_cert(str(tmp_path), "renewed", cn="natgw-renewed")
    os.replace(crt, srv.crt)
    os.replace(key, srv.key)
    srv.web.reload_tls()
    assert "natgw-renewed" in cert_cn()
    # a broken file keeps the old certificate
    with open(srv.crt, "w") as f:
        f.write("garbage")
    srv.web.reload_tls()
    assert "natgw-renewed" in cert_cn()


def test_bind_address_only(tmp_path):
    s = Server(tmp_path, bind=('"127.0.0.2"',))
    try:
        s.wait()
        assert s.get("/healthz", host="127.0.0.2")[0].status == 200
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", s.port), timeout=2)
        assert s.web.addresses() == [("127.0.0.2", s.port)]
    finally:
        s.close()


def test_bind_interface_name(tmp_path):
    s = Server(tmp_path, bind=('"lo"',))
    try:
        s.wait()
        addrs = {a for a, _ in s.web.addresses()}
        assert "127.0.0.1" in addrs and addrs <= set(interface_addresses("lo"))
        assert s.get("/healthz")[0].status == 200
    finally:
        s.close()


def test_interface_without_address_waits(tmp_path):
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
        pytest.skip("needs passwordless sudo (a dummy interface)")
    dev = "natgwmon0"
    subprocess.run(["sudo", "-n", "ip", "link", "del", dev], capture_output=True)
    subprocess.run(["sudo", "-n", "ip", "link", "add", dev, "type", "dummy"], check=True)
    try:
        s = Server(tmp_path, bind=(f'"{dev}"',))
        time.sleep(0.5)
        assert s.started is None, "must wait for the interface's address"
        subprocess.run(["sudo", "-n", "ip", "addr", "add", "127.0.0.3/32", "dev", dev], check=True)
        subprocess.run(["sudo", "-n", "ip", "link", "set", dev, "up"], check=True)
        s.wait()
        assert s.get("/healthz", host="127.0.0.3")[0].status == 200
        s.close()
    finally:
        subprocess.run(["sudo", "-n", "ip", "link", "del", dev], capture_output=True)


def test_plain_http_when_tls_disabled(tmp_path):
    s = Server(tmp_path, tls=False)
    try:
        s.wait()
        conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=3)
        conn.request("GET", "/api/stats")
        r = conn.getresponse()
        assert r.status == 200 and r.getheader("Strict-Transport-Security") is None
    finally:
        s.close()


def test_check_option(tmp_path):
    crt, key = make_cert(str(tmp_path), "c")
    _, other = make_cert(str(tmp_path), "other")
    env = dict(os.environ, PYTHONPATH=MONITOR)

    def check(text):
        p = tmp_path / "m.toml"
        p.write_text(text)
        return subprocess.run([sys.executable, "-m", "natgw_monitor", "--check", "-c", str(p)], env=env,
                              capture_output=True, text=True)
    r = check(f'[http]\nbind = ["enp1s0"]\n[tls]\ncert = "{crt}"\nkey = "{key}"\n')
    assert r.returncode == 0 and "https on enp1s0 port 8443" in r.stdout
    r = check(f'[tls]\ncert = "{crt}"\nkey = "{other}"\n')
    assert r.returncode == 2 and "TLS" in r.stderr
    r = check('[tls]\ncert = "/nonexistent.crt"\nkey = "/nonexistent.key"\n')
    assert r.returncode == 2
