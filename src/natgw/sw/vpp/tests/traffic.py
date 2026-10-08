# SPDX-License-Identifier: BSD-3-Clause
"""Traffic endpoints run inside the test namespaces (python3 traffic.py ...).

Clients pause between phases: they print a line starting with 'PAUSE' and
wait for a line on stdin, so the test can inspect VPP while the connection
is established and idle. Results are printed as one JSON line at the end."""

import argparse
import hashlib
import json
import random
import socket
import sys
import time


def pause(tag):
    print(f"PAUSE {tag}", flush=True)
    sys.stdin.readline()


def tcp_server(a):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((a.bind, a.port))
    s.listen(16)
    print("READY", flush=True)
    while True:
        c, peer = s.accept()
        h = hashlib.sha256()
        n = 0
        while True:
            d = c.recv(65536)
            if not d:
                break
            h.update(d)
            n += len(d)
            if a.echo:
                c.sendall(d)
        c.sendall(json.dumps({"bytes": n, "sha": h.hexdigest(), "peer": peer[0], "peer_port": peer[1]}).encode())
        c.close()


def tcp_client(a):
    rnd = random.Random(a.seed)
    s = socket.create_connection((a.dst, a.port), timeout=a.timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    h = hashlib.sha256()
    sent = 0
    echoed = 0
    phases = a.phases
    per = a.bytes // phases
    for p in range(phases):
        left = per
        while left:
            chunk = rnd.randbytes(min(left, a.chunk))
            s.sendall(chunk)
            h.update(chunk)
            sent += len(chunk)
            left -= len(chunk)
            if a.echo:
                got = b""
                while len(got) < len(chunk):
                    got += s.recv(len(chunk) - len(got))
                if got != chunk:
                    raise SystemExit("echo mismatch")
                echoed += len(got)
        if p + 1 < phases:
            pause(f"phase{p + 1}")
    s.shutdown(socket.SHUT_WR)
    reply = b""
    while True:
        d = s.recv(65536)
        if not d:
            break
        reply += d
    r = json.loads(reply.decode())
    print(json.dumps({"sent": sent, "sha": h.hexdigest(), "server": r, "echoed": echoed,
                      "local_port": s.getsockname()[1]}), flush=True)


def udp_echo(a):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((a.bind, a.port))
    print("READY", flush=True)
    while True:
        d, peer = s.recvfrom(65536)
        s.sendto(json.dumps({"peer": peer[0], "peer_port": peer[1]}).encode() + b"|" + d, peer)


def udp_client(a):
    """a.flows sockets (distinct source ports), a.count datagrams each per phase"""
    socks = []
    for _ in range(a.flows):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(a.timeout)
        s.connect((a.dst, a.port))
        socks.append(s)
    res = {"sent": 0, "received": 0, "lost": 0, "peers": [], "local_ports": [s.getsockname()[1] for s in socks],
           "per_flow": [0] * len(socks)}
    for p in range(a.phases):
        for i, s in enumerate(socks):
            for k in range(a.count):
                payload = f"{p}:{i}:{k}:".encode() + b"x" * a.size
                s.send(payload)
                res["sent"] += 1
                try:
                    d = s.recv(65536)
                    meta, echo = d.split(b"|", 1)
                    if echo == payload:
                        res["received"] += 1
                        res["per_flow"][i] += 1
                        if p == a.phases - 1 and k == a.count - 1:
                            res["peers"].append(json.loads(meta))
                    else:
                        res["lost"] += 1
                except socket.timeout:
                    res["lost"] += 1
                if a.gap:
                    time.sleep(a.gap)
        if p + 1 < a.phases:
            pause(f"phase{p + 1}")
    print(json.dumps(res), flush=True)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("tcp-server", "udp-echo"):
        p = sp.add_parser(name)
        p.add_argument("--bind", default="0.0.0.0")
        p.add_argument("--port", type=int, required=True)
        p.add_argument("--echo", action="store_true")
    p = sp.add_parser("tcp-client")
    p.add_argument("--dst", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--bytes", type=int, default=1 << 20)
    p.add_argument("--chunk", type=int, default=8192)
    p.add_argument("--phases", type=int, default=1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--echo", action="store_true")
    p.add_argument("--timeout", type=float, default=10)
    p = sp.add_parser("udp-client")
    p.add_argument("--dst", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--flows", type=int, default=1)
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--size", type=int, default=100)
    p.add_argument("--phases", type=int, default=1)
    p.add_argument("--gap", type=float, default=0)
    p.add_argument("--timeout", type=float, default=2)
    a = ap.parse_args()
    {"tcp-server": tcp_server, "tcp-client": tcp_client, "udp-echo": udp_echo, "udp-client": udp_client}[a.cmd](a)


if __name__ == "__main__":
    main()
