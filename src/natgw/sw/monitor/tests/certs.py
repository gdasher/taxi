# SPDX-License-Identifier: BSD-3-Clause
"""Self-signed test certificates and free ports."""

import os
import socket
import subprocess


def make_cert(d, name, cn="natgw-test"):
    crt, key = os.path.join(d, f"{name}.crt"), os.path.join(d, f"{name}.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", key, "-out", crt, "-days", "2", "-subj", f"/CN={cn}",
                    "-addext", "subjectAltName=IP:127.0.0.1,IP:127.0.0.2,IP:127.0.0.3,DNS:localhost"],
                   check=True, capture_output=True)
    return crt, key


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
