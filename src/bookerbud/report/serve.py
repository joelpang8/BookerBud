"""Serve the reports folder so a phone on the same Wi-Fi can open the dashboard,
plus a small, key-protected API behind the dashboard's "Refresh odds" button.

Static files come from the reports directory only (Python's
SimpleHTTPRequestHandler confines paths to that directory). "/" redirects to
latest.html, keeping the query string (which carries the key).

API (only when started with a refresh job and key):
  GET  /api/status   -> refresh state + quota          (header X-BookerBud-Key)
  POST /api/refresh  -> start fetch + context + report  (202 started, 409 running, 429 cooling down)
  POST /api/bets     -> note a bet you placed (JSON body)  (200 saved, 400 couldn't match)
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

KEY_HEADER = "X-BookerBud-Key"


class Refresher:
    """Runs one refresh job at a time in a background thread, with a cooldown."""

    def __init__(self, job: Callable[[], dict], cooldown_s: float, clock: Callable[[], float] = time.time):
        self.job = job
        self.cooldown_s = cooldown_s
        self.clock = clock
        self._lock = threading.Lock()
        self.running = False
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> tuple[int, dict]:
        with self._lock:
            if self.running:
                return 409, self.status()
            if self.started_at is not None:
                wait = self.cooldown_s - (self.clock() - self.started_at)
                if wait > 0:
                    return 429, {**self.status(), "retry_after": int(wait) + 1}
            self.running, self.error, self.result = True, None, None
            self.started_at = self.clock()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return 202, self.status()

    def _run(self) -> None:
        try:
            result = self.job()
            with self._lock:
                self.result = result
        except Exception as exc:  # surfaced to the page, never crashes the server
            with self._lock:
                self.error = str(exc) or exc.__class__.__name__
            traceback.print_exc()
        finally:
            with self._lock:
                self.running = False
                self.finished_at = self.clock()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def status(self) -> dict:
        def iso(t):
            return datetime.fromtimestamp(t, UTC).isoformat() if t else None

        cooldown_left = 0
        if self.started_at is not None and not self.running:
            cooldown_left = max(0, int(self.cooldown_s - (self.clock() - self.started_at)) + 1)
        return {"running": self.running, "started_at": iso(self.started_at), "finished_at": iso(self.finished_at),
                "result": self.result, "error": self.error, "cooldown_left": cooldown_left if self.started_at else 0}


MAX_BODY = 4096


class ReportHandler(SimpleHTTPRequestHandler):
    refresher: Refresher | None = None
    key: str | None = None
    bet_action: Callable[[dict], tuple[int, dict]] | None = None

    # ---------------------------------------------------------------- helpers
    def _json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        given = self.headers.get(KEY_HEADER, "")
        return bool(self.key) and secrets.compare_digest(given, self.key)

    def _api(self) -> bool:
        """Handle /api/* routes; True if handled."""
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            return False
        if self.refresher is None and self.bet_action is None:
            self._json(404, {"error": "refresh is disabled on this server"})
        elif not self._authorised():
            self._json(403, {"error": "missing or wrong key: open the link printed by `betedge serve`"})
        elif path == "/api/status" and self.command == "GET":
            self._json(200, self.refresher.status() if self.refresher else {"running": False})
        elif path == "/api/refresh" and self.command == "POST" and self.refresher:
            code, body = self.refresher.start()
            self._json(code, body)
        elif path == "/api/bets" and self.command == "POST" and self.bet_action:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(413, {"error": "request too large"})
                return True
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                self._json(400, {"error": "body must be a JSON object"})
                return True
            code, body = type(self).bet_action(payload)
            self._json(code, body)
        else:
            self._json(404, {"error": "unknown endpoint"})
        return True

    # ---------------------------------------------------------------- verbs
    def do_GET(self):  # noqa: N802 - stdlib naming
        if self._api():
            return
        parts = urlsplit(self.path)
        if parts.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/latest.html" + (f"?{parts.query}" if parts.query else ""))
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):  # noqa: N802
        if not self._api():
            self._json(405, {"error": "read-only"})

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


def load_or_create_key(path: Path) -> str:
    """A stable key, so a bookmarked/home-screen link keeps working across restarts."""
    if path.exists():
        key = path.read_text().strip()
        if key:
            return key
    key = secrets.token_urlsafe(12)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def make_server(
    directory: Path, host: str, port: int, *, refresher: Refresher | None = None, key: str | None = None,
    bet_action: Callable[[dict], tuple[int, dict]] | None = None,
) -> ThreadingHTTPServer:
    handler_cls = type("BookerBudHandler", (ReportHandler,),
                       {"refresher": refresher, "key": key, "bet_action": staticmethod(bet_action) if bet_action else None})

    def factory(*args, **kwargs):
        return handler_cls(*args, directory=str(directory), **kwargs)

    return ThreadingHTTPServer((host, port), factory)
