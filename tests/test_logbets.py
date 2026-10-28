import json
import threading
import urllib.error
import urllib.request

import pytest

from bookerbud.jobs import log_bet_action
from bookerbud.models import Sport
from bookerbud.report.build import ReportCfg, build_report
from bookerbud.report.html import render_html
from bookerbud.report.serve import KEY_HEADER, make_server
from bookerbud.storage import connect
from bookerbud.storage.tracking import get_bet, update_bet
from bookerbud.tracking.logbets import BetError, bet_from_market, bet_from_rec, describe, edit, place, remove
from tests.test_report import AS_OF, FETCHED
from tests.test_storage import _store


@pytest.fixture
def conn(settings, nba_result):
    c = connect(settings.db_file)
    _store(c, nba_result, FETCHED)
    return c


def _rec_play(conn, settings):
    r = build_report(conn, settings, [Sport.NBA], now=AS_OF, rcfg=ReportCfg())
    plays = [p for ps in r.plays.values() for p in ps if p.get("rec_id") and p["kind"] != "pickem"]
    assert plays, "fixture should produce at least one sportsbook play"
    return r, plays[0]


def test_mark_placed_from_pick_shows_on_dashboard(conn, settings):
    r, play = _rec_play(conn, settings)
    html = render_html(r)
    assert "Mark placed" in html and "Your bets" in html and "No bets noted" in html
    bet = bet_from_rec(conn, play["rec_id"], line=None, price=None)
    bet_id, saved = place(conn, bet, stake=None)
    assert bet_id > 0 and saved["stake"] == 0.0 and "no stake recorded" in describe(saved)
    r2 = build_report(conn, settings, [Sport.NBA], now=AS_OF, rcfg=ReportCfg())
    again = [p for ps in r2.plays.values() for p in ps if p.get("rec_id") == play["rec_id"]]
    assert again and again[0]["placed"] is not None
    assert len(r2.my_bets) == 1 and r2.my_bets[0]["result"] is None
    html2 = render_html(r2)
    assert "✓ Placed" in html2 and "No bets noted" not in html2


def test_log_from_market_matches_latest_odds(conn):
    bet = bet_from_market(conn, player="lebron", team=None, stat="points", side="over", bet_type=None,
                          book="draftkings", line=None, price=-110)
    assert bet["player"] == "LeBron James" and bet["price_american"] == -110 and bet["rec_id"] is None
    _, saved = place(conn, bet, stake=25)
    assert describe(saved).endswith("@ draftkings -110 for $25.00")


def test_log_from_market_errors_are_readable(conn):
    with pytest.raises(BetError, match="No draftkings market for nobody"):
        bet_from_market(conn, player="nobody", team=None, stat="points", side="over", bet_type=None,
                        book="draftkings", line=5.5, price=None)
    with pytest.raises(BetError, match="Give a player"):
        bet_from_market(conn, player=None, team=None, stat="points", side="over", bet_type=None,
                        book="draftkings", line=5.5, price=None)
    bet = bet_from_market(conn, player="lebron", team=None, stat="points", side="over", bet_type=None,
                          book="draftkings", line=None, price=None)
    with pytest.raises(BetError, match="negative"):
        place(conn, bet, stake=-1)
    with pytest.raises(BetError, match="enter the price"):
        place(conn, {**bet, "price_american": None}, stake=None)


def test_log_bet_action(conn, settings):
    conn.close()
    code, body = log_bet_action(settings, {"player": "lebron", "stat": "points", "side": "over", "book": "draftkings",
                                           "price": "−115", "stake": "$10"})
    assert code == 200 and body["ok"] and "-115 for $10.00" in body["text"]
    code, body = log_bet_action(settings, {"player": "lebron", "side": "over", "book": "draftkings", "stake": "ten"})
    assert code == 400 and "Stake must be a number" in body["error"]


@pytest.fixture
def bet_server(tmp_path):
    (tmp_path / "latest.html").write_text("x")
    seen = []

    def action(payload):
        seen.append(payload)
        return (200, {"ok": True, "text": "saved"}) if payload.get("player") else (400, {"error": "need a player"})

    srv = make_server(tmp_path, "127.0.0.1", 0, key="secret", bet_action=action)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api/bets", seen
    srv.shutdown()
    srv.server_close()


def post(url, body: bytes, key="secret"):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={KEY_HEADER: key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_bets(bet_server):
    url, seen = bet_server
    assert post(url, b'{"player": "x"}', key="wrong")[0] == 403
    assert post(url, b'{"player": "Keon Ellis"}') == (200, {"ok": True, "text": "saved"})
    assert post(url, b"{}") == (400, {"error": "need a player"})
    assert post(url, b"[1]")[0] == 400 and post(url, b"not json")[0] == 400
    assert post(url, b"{" + b" " * 5000 + b"}")[0] == 413
    assert seen == [{"player": "Keon Ellis"}, {}]
    # status works without a refresher so the page can reveal the bet controls
    req = urllib.request.Request(url.replace("bets", "status"), headers={KEY_HEADER: "secret"})
    with urllib.request.urlopen(req) as resp:
        assert json.loads(resp.read()) == {"running": False}


def _lebron(conn, **kw):
    bet = bet_from_market(conn, player="lebron", team=None, stat="points", side="over", bet_type=None,
                          book="draftkings", line=None, price=-110)
    return place(conn, bet, stake=kw.get("stake"))[0]


def test_edit_changes_only_given_fields(conn):
    bet_id = _lebron(conn)
    bet = edit(conn, bet_id, stake=20, price=-125)
    assert (bet["stake"], bet["price_american"], bet["side"]) == (20.0, -125, "over")
    assert get_bet(conn, bet_id)["price_american"] == -125
    with pytest.raises(BetError, match="American odds"):
        edit(conn, bet_id, price=50)
    with pytest.raises(BetError, match="over / under"):
        edit(conn, bet_id, side="home")
    with pytest.raises(BetError, match="No bet #999"):
        edit(conn, 999, stake=1)


def test_edit_regrades_a_settled_bet(conn):
    bet_id = _lebron(conn, stake=10)
    # pretend settle graded it: 26 points against over 25.5 at -110 wins 9.09
    update_bet(conn, bet_id, {"line": 25.5, "result": "win", "stat_value": 26.0, "profit": 9.09, "clv_pct": 2.0})
    bet = edit(conn, bet_id, side="under")
    assert bet["result"] == "loss" and bet["profit"] == -10.0 and bet["clv_pct"] is None
    bet = edit(conn, bet_id, line=26)
    assert bet["result"] == "push" and bet["profit"] == 0.0
    bet = edit(conn, bet_id, line=27.5, stake=40, price=+150)
    assert bet["result"] == "win" and bet["profit"] == pytest.approx(60.0)  # 40 x 1.5
    bet = edit(conn, bet_id, stake=0)
    assert bet["profit"] == 0.0


def test_delete_bet(conn):
    bet_id = _lebron(conn)
    assert remove(conn, bet_id)["player"] == "LeBron James"
    assert get_bet(conn, bet_id) is None
    with pytest.raises(BetError):
        remove(conn, bet_id)


def test_edit_and_delete_via_action(conn, settings):
    bet_id = _lebron(conn)
    conn.close()
    code, body = log_bet_action(settings, {"action": "edit", "id": str(bet_id), "stake": "15", "price": "-120",
                                           "line": "", "side": "under", "book": "draftkings"})
    assert code == 200 and body["text"].startswith("Updated: LeBron James under") and "-120 for $15.00" in body["text"]
    html = (settings.raw_dir.parent / "reports" / "latest.html").read_text()
    assert f'data-edit="{bet_id}"' in html and 'value="-120"' in html
    code, body = log_bet_action(settings, {"action": "delete", "id": bet_id})
    assert code == 200 and body["text"].startswith("Deleted:")
    assert log_bet_action(settings, {"action": "delete", "id": bet_id})[0] == 400
    assert log_bet_action(settings, {"action": "edit"}) == (400, {"error": "Which bet? (missing id)"})


def test_refresh_grades_finished_games_at_most_every_30_min(settings, monkeypatch):
    from collections import Counter
    from datetime import timedelta

    import bookerbud.jobs as jobs
    import bookerbud.tracking.settle as settle_mod
    from bookerbud.tracking.settle import SettleSummary

    calls = []

    def fake_settle(conn, s, provider, *, now):
        calls.append(now)
        return SettleSummary(pending=3, settled=Counter(win=2, loss=1), warnings=["note"])

    monkeypatch.setattr(settle_mod, "settle", fake_settle)
    monkeypatch.setattr(jobs, "_last_settle", {})
    warnings: list[str] = []
    assert jobs.settle_finished(None, settings, None, now=AS_OF, warnings=warnings) == {"win": 2, "loss": 1}
    assert warnings == ["note"]
    assert jobs.settle_finished(None, settings, None, now=AS_OF + timedelta(minutes=10), warnings=[]) == {}
    jobs.settle_finished(None, settings, None, now=AS_OF + timedelta(minutes=31), warnings=[])
    assert len(calls) == 2

    def broken(*a, **k):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(settle_mod, "settle", broken)
    warnings = []
    assert jobs.settle_finished(None, settings, None, now=AS_OF + timedelta(hours=2), warnings=warnings) == {}
    assert warnings == ["grading results failed: HTTP 500"]


def test_performance_tab_lists_won_picks(conn, settings):
    from bookerbud.storage.tracking import save_settlement

    r, play = _rec_play(conn, settings)
    others = [p for ps in r.plays.values() for p in ps if p.get("rec_id") and p["rec_id"] != play["rec_id"]]
    save_settlement(conn, "recommendations", play["rec_id"], {"result": "win", "stat_value": 31.0})
    save_settlement(conn, "recommendations", others[0]["rec_id"], {"result": "loss", "stat_value": 2.0})
    place(conn, bet_from_rec(conn, play["rec_id"], line=None, price=None), stake=None)
    r2 = build_report(conn, settings, [Sport.NBA], now=AS_OF, rcfg=ReportCfg())
    recent = r2.performance["recent"]
    assert [x["result"] for x in recent].count("win") == 1 and len(recent) == 2
    won = next(x for x in recent if x["result"] == "win")
    assert won["who"] == play["subject"] and won["you_bet"] and won["stat_value"] == 31.0
    html = render_html(r2)
    assert "Recent results" in html and "Won (1)" in html and "Lost (1)" in html and "you bet it" in html
    assert '<li data-res="loss" hidden>' in html and '<li data-res="win">' in html
