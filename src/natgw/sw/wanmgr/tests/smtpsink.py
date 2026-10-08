# SPDX-License-Identifier: BSD-3-Clause
"""A minimal SMTP server that keeps every message it receives (tests)."""

import email
import socketserver
import threading


class _Handler(socketserver.StreamRequestHandler):
    def _send(self, line):
        self.wfile.write((line + "\r\n").encode())

    def handle(self):
        self._send("220 smtpsink ready")
        data_mode, lines, rcpt = False, [], []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").rstrip("\r\n")
            if data_mode:
                if line == ".":
                    data_mode = False
                    msg = email.message_from_string("\n".join(lines))
                    self.server.messages.append((rcpt, msg))
                    lines, rcpt = [], []
                    self._send("250 OK queued")
                else:
                    lines.append(line[1:] if line.startswith("..") else line)
                continue
            cmd = line[:4].upper()
            if cmd in ("HELO", "EHLO"):
                self._send("250 smtpsink")
            elif cmd == "MAIL":
                self._send("250 OK")
            elif cmd == "RCPT":
                rcpt.append(line.split(":", 1)[1].strip(" <>"))
                self._send("250 OK")
            elif cmd == "DATA":
                data_mode = True
                self._send("354 end with .")
            elif cmd == "QUIT":
                self._send("221 bye")
                return
            else:
                self._send("250 OK")


class SmtpSink(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host="127.0.0.1", port=0):
        super().__init__((host, port), _Handler)
        self.messages = []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def close(self):
        self.shutdown()
        self.server_close()

    def subjects(self):
        return [m["Subject"] for _, m in self.messages]
