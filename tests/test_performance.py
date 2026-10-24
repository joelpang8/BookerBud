"""Performance metrics on hand-made rows (numbers checked by hand)."""

from datetime import UTC, datetime

import pytest

from bookerbud.storage import connect
from bookerbud.storage.tracking import insert_bet, save_settlement
from bookerbud.tracking.performance import summarize

approx = pytest.approx
T = datetime(2026, 10, 24, 23, tzinfo=UTC).isoformat()


def add_rec(conn, *, book="draftkings", sport="nba", stat="points", price=-110, ev=3.0, fair=0.55,
            result=None, clv=None, beat=None, payout="american_odds", key="x"):
    conn.execute(
        """INSERT INTO recommendations (rec_day, first_seen_at, last_seen_at, sport, event_id, start_time, market_kind,
           stat_type, period, bet_type, side, line, bookmaker, payout_type, price_american, fair_prob, ev_pct,
           provider_market_id, result, clv_pct, beat_close)
           VALUES ('2026-10-24', ?, ?, ?, ?, ?, 'player_prop', ?, 'game', 'ou', 'over', 20.5, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (T, T, sport, key, T, stat, book, payout, price, fair, ev, key, result, clv, beat),
    )
    conn.commit()


def test_rec_metrics(settings):
    conn = connect(settings.db_file)
    add_rec(conn, price=-110, ev=3, fair=0.55, result="win", clv=4.0, beat=1, key="a")
    add_rec(conn, price=+150, ev=7, fair=0.45, result="loss", clv=-2.0, beat=0, key="b")
    add_rec(conn, price=-110, ev=12, fair=0.60, result="push", clv=1.0, key="c")
    add_rec(conn, price=None, ev=5, fair=0.58, result="win", payout="pickem", book="prizepicks", key="d")
    add_rec(conn, ev=2.5, key="e")  # pending
    p = summarize(conn)
    r = p["recs"]
    assert (r["n"], r["graded"], r["wins"], r["losses"], r["pushes"]) == (5, 4, 2, 1, 1)
    assert r["hit_rate"] == approx(2 / 3)
    assert r["clv"] == approx((4 - 2 + 1) / 3) and r["clv_n"] == 3
    assert r["beat_close"] == approx(0.5)
    # flat units (sportsbook only): win at -110 = +0.909091, loss = -1, push = 0 -> -0.090909 over 3 units
    assert r["flat_units"] == approx(100 / 110 - 1) and r["flat_roi"] == approx((100 / 110 - 1) / 3)
    assert p["pending"] == 1
    books = {x["key"]: x for x in p["by"]["book"]["recs"]}
    assert books["prizepicks"]["hit_rate"] == 1.0 and books["draftkings"]["n"] == 4


def test_edge_buckets_and_calibration(settings):
    conn = connect(settings.db_file)
    add_rec(conn, ev=3, fair=0.52, result="win", key="a")
    add_rec(conn, ev=3.5, fair=0.53, result="loss", key="b")
    add_rec(conn, ev=11, fair=0.62, result="win", key="c")
    p = summarize(conn)
    b = {x["bucket"]: x for x in p["edge_buckets"]}
    assert set(b) == {"2–4%", "10%+"}
    assert b["2–4%"]["n"] == 2 and b["2–4%"]["hit_rate"] == 0.5 and b["2–4%"]["predicted"] == approx(0.525)
    cal = {round(x["lo"], 2): x for x in p["calibration"]}
    assert cal[0.5]["n"] == 2 and cal[0.5]["actual"] == 0.5 and cal[0.6]["actual"] == 1.0


def test_bet_roi(settings):
    conn = connect(settings.db_file)
    base = dict(sport="nba", event_id="e", start_time=T, market_kind="player_prop", stat_type="points", period="game",
                bet_type="ou", side="over", line=20.5, bookmaker="fanduel", payout_type="american_odds",
                provider_market_id="m")
    a = insert_bet(conn, {**base, "price_american": 120, "stake": 20})
    b = insert_bet(conn, {**base, "price_american": -110, "stake": 11})
    insert_bet(conn, {**base, "price_american": -110, "stake": 50})  # unsettled: not in ROI
    save_settlement(conn, "bets", a, {"result": "win", "profit": 24.0, "clv_pct": 5.0})
    save_settlement(conn, "bets", b, {"result": "loss", "profit": -11.0, "clv_pct": -1.0})
    bets = summarize(conn)["bets"]
    # profit 24 - 11 = 13 on 31 staked -> 41.94%
    assert bets["profit"] == 13 and bets["staked"] == 31 and bets["roi"] == approx(13 / 31)
    assert bets["clv"] == approx(2.0) and bets["n"] == 3


def test_since_filter_and_empty(settings):
    conn = connect(settings.db_file)
    p = summarize(conn)
    assert p["recs"]["n"] == 0 and p["recs"]["hit_rate"] is None and p["edge_buckets"] == [] and p["calibration"] == []
    add_rec(conn, result="win", key="a")
    assert summarize(conn, since="2026-11-01")["recs"]["n"] == 0
