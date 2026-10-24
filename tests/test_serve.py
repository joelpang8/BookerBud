import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from bookerbud.report.serve import KEY_HEADER, Refresher, load_or_create_key, make_server


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_refresher_runs_once_and_cools_down():
    clock = Clock()
    gate = threading.Event()
    calls = []

    def job():
        calls.append(1)
        gate.wait(2)
        return {"plays": 3}

    r = Refresher(job, cooldown_s=300, clock=clock)
    code, body = r.start()
    assert code == 202 and body["running"]
    assert r.start()[0] == 409  # already running
    gate.set()
    r.wait(2)
    assert r.status()["result"] == {"plays": 3} and not r.status()["running"]
    clock.t += 60
    code, body = r.start()
    assert code == 429 and 230 < body["retry_after"] <= 241
    clock.t += 300
    assert r.start()[0] == 202
    r.wait(2)
    assert len(calls) == 2


def test_refresher_reports_errors():
    def job():
        raise RuntimeError("quota guard: budget reached")

    r = Refresher(job, cooldown_s=0)
    r.start()
    r.wait(2)
    s = r.status()
    assert not s["running"] and s["error"] == "quota guard: budget reached"


def test_key_is_stable(tmp_path):
    p = tmp_path / "k.txt"
    k = load_or_create_key(p)
    assert len(k) >= 12 and load_or_create_key(p) == k


@pytest.fixture
def server(tmp_path):
    (tmp_path / "latest.html").write_text("<p>dash</p>")
    r = Refresher(lambda: (time.sleep(0.05), {"plays": 1})[1], cooldown_s=300)
    srv = make_server(tmp_path, "127.0.0.1", 0, refresher=r, key="secret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", r
    srv.shutdown()
    srv.server_close()


def call(url, method="GET", key=None):
    req = urllib.request.Request(url, method=method, headers={KEY_HEADER: key} if key else {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_api_requires_key(server):
    base, _ = server
    assert call(base + "/api/status")[0] == 403
    assert call(base + "/api/refresh", "POST", key="wrong")[0] == 403
    code, body = call(base + "/api/status", key="secret")
    assert code == 200 and json.loads(body)["running"] is False


def test_refresh_flow_and_static_files(server):
    base, refresher = server
    code, _ = call(base + "/api/refresh", "POST", key="secret")
    assert code == 202
    refresher.wait(2)
    assert json.loads(call(base + "/api/status", key="secret")[1])["result"] == {"plays": 1}
    assert call(base + "/api/refresh", "POST", key="secret")[0] == 429  # cooldown
    # root keeps the key in the redirect; static files still served
    code, body = call(base + "/?k=secret")
    assert code == 200 and body == b"<p>dash</p>"
    assert call(base + "/latest.html", "POST")[0] in (403, 405)


def test_refresh_disabled_without_refresher(tmp_path):
    (tmp_path / "latest.html").write_text("x")
    srv = make_server(tmp_path, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(f"http://127.0.0.1:{srv.server_address[1]}/api/status", key="any")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
