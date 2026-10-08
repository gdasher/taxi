# SPDX-License-Identifier: BSD-3-Clause
"""Alerts: syslog for every event, email rate-limited.

Email goes out from a background thread so a slow or dead relay never
stalls probing. At most `max_per_hour` mails in any rolling hour; alerts
over the limit are held and summarised in the next mail that is allowed."""

import collections
import email.message
import logging
import logging.handlers
import queue
import smtplib
import threading
import time

log = logging.getLogger("wanmgr")


class Alerter:
    def __init__(self, cfg, clock=time.monotonic, sender=None):
        """cfg: config.AlertConfig; sender(msg) replaces SMTP (tests)"""
        self.cfg = cfg
        self.clock = clock
        self.sent_times = collections.deque()
        self.held = []
        self.sent = 0
        self.failed = 0
        self._send = sender or self._smtp_send
        self._q = queue.Queue()
        self._thread = None
        self._syslog = None
        if cfg.syslog:
            try:
                self._syslog = logging.handlers.SysLogHandler(address="/dev/log")
                self._syslog.setFormatter(logging.Formatter("natgw-wanmgr: %(message)s"))
            except OSError:
                self._syslog = None

    def start(self):
        if self.cfg.recipients and self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="wanmgr-mail", daemon=True)
            self._thread.start()

    def stop(self, timeout=5.0):
        if self._thread:
            self._q.put(None)
            self._thread.join(timeout)
            self._thread = None

    def alert(self, subject, body):
        """record an event; mail it now if the rate limit allows"""
        log.warning("%s: %s", subject, body.splitlines()[0] if body else "")
        if self._syslog:
            self._syslog.emit(logging.LogRecord("wanmgr", logging.WARNING, "", 0, f"{subject}: {body}",
                                                None, None))
        if not self.cfg.recipients:
            return
        self.held.append((time.strftime("%Y-%m-%d %H:%M:%S"), subject, body))
        self.flush()

    def flush(self):
        """send held alerts if the rate limit allows (call periodically)"""
        if not self.held:
            return
        now = self.clock()
        while self.sent_times and now - self.sent_times[0] >= 3600:
            self.sent_times.popleft()
        if len(self.sent_times) >= self.cfg.max_per_hour:
            return
        held, self.held = self.held, []
        stamp, subject, body = held[-1]
        if len(held) > 1:
            subject = f"{subject} (+{len(held) - 1} earlier)"
            body = body + "\n\nEarlier alerts held by the rate limit:\n" + "\n".join(
                f"  {t}  {s}" for t, s, _ in held[:-1])
        msg = email.message.EmailMessage()
        msg["Subject"] = f"[{self.cfg.hostname}] {subject}"
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(self.cfg.recipients)
        msg.set_content(f"{stamp}\n\n{body}\n")
        self.sent_times.append(now)
        if self._thread:
            self._q.put(msg)
        else:
            self._deliver(msg)

    def _deliver(self, msg):
        try:
            self._send(msg)
            self.sent += 1
        except Exception as e:  # never let mail break the daemon
            self.failed += 1
            log.error("alert mail failed: %s", e)

    def _worker(self):
        while True:
            msg = self._q.get()
            if msg is None:
                return
            self._deliver(msg)

    def _smtp_send(self, msg):
        c = self.cfg
        with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=c.timeout) as s:
            if c.starttls:
                s.starttls()
            if c.username:
                s.login(c.username, c.password)
            s.send_message(msg)
