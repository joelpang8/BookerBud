"""Serve the reports folder so a phone on the same Wi-Fi can open the dashboard.

Read-only static files from the reports directory only (Python's
SimpleHTTPRequestHandler confines paths to that directory). "/" redirects to
latest.html.
"""

from __future__ import annotations

import functools
import socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ReportHandler(SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/latest.html")
            self.end_headers()
            return
        super().do_GET()

    def end_headers(self):
        # Always fetch the newest report; never let a phone show yesterday's cached page.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass


def lan_addresses() -> list[str]:
    """Best-effort local IPv4 addresses (the one with a default route first)."""
    addrs: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET address: no packet is sent for UDP connect
            addrs.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addrs and not ip.startswith("127."):
                addrs.append(ip)
    except OSError:
        pass
    return addrs


def make_server(directory: Path, host: str, port: int) -> ThreadingHTTPServer:
    handler = functools.partial(ReportHandler, directory=str(directory))
    return ThreadingHTTPServer((host, port), handler)
