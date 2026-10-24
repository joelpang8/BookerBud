"""Settlement: grading, closing fair value and CLV. Numbers use the
multiplicative devig so they can be checked by hand."""

from datetime import UTC, datetime, timedelta

import pytest

from bookerbud.edge.engine import find_edges
from bookerbud.edge.config import load_availability, load_edge_config, load_payouts
from bookerbud.normalize import normalize_props
from bookerbud.providers.base import FetchResult
from bookerbud.providers.sportsgameodds import parse_settlement
from bookerbud.storage import connect, finish_run, insert_props, start_run, upsert_events
from bookerbud.storage.tracking import insert_bet, record_recommendations
from bookerbud.tracking.settle import grade, profit, settle

T = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)  # snapshot time; LAL@DEN starts 01:00Z
AFTER = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
PICKEM = {"prizepicks", "underdog", "sleeper"}
approx = pytest.approx


def mcfg():
    c = load_edge_config()
    c.devig_method = "multiplicative"
    c.filters.min_edge_pct = -100
    return c


def bk(close, line=None, key="closeOverUnder"):
    d = {"odds": close, "closeOdds": close, "available": False}
    if line is not None:
        d[key] = line
    return d


def odd(odd_id, opp, stat, ent, bet, side, books, score=None, period="game", cancelled=False):
    o = {"oddID": odd_id, "opposingOddID": opp, "statID": stat, "statEntityID": ent, "periodID": period,
         "betTypeID": bet, "sideID": side, "cancelled": cancelled, "byBookmaker": books}
    if score is not None:
        o["score"] = score
    return o


L = "LEBRON_JAMES_1_NBA"
SETTLED_EVENT = {
    "eventID": "SYNTH_NBA_LAL_DEN",
    "status": {"started": True, "ended": True, "finalized": True, "cancelled": False},
    "results": {"game": {"home": {"points": 112}, "away": {"points": 108},
                         L: {"points": 27, "rebounds": 8, "assists": 9},
                         "NIKOLA_JOKIC_1_NBA": {"points": 30, "rebounds": 12, "assists": 10}}},
    "odds": {
        f"points-{L}-game-ou-over": odd(f"points-{L}-game-ou-over", f"points-{L}-game-ou-under", "points", L, "ou", "over",
                                        {"pinnacle": bk("-150", "24.5"), "draftkings": bk("-135", "24.5"), "fanduel": bk("-120", "25.5")}, score=27),
        f"points-{L}-game-ou-under": odd(f"points-{L}-game-ou-under", f"points-{L}-game-ou-over", "points", L, "ou", "under",
                                         {"pinnacle": bk("+130", "24.5"), "draftkings": bk("+105", "24.5"), "fanduel": bk("-110", "25.5")}, score=27),
        "points-home-game-ml-home": odd("points-home-game-ml-home", "points-away-game-ml-away", "points", "home", "ml", "home",
                                        {"pinnacle": bk("-180")}, score=112),
        "points-away-game-ml-away": odd("points-away-game-ml-away", "points-home-game-ml-home", "points", "away", "ml", "away",
                                        {"pinnacle": bk("+160")}, score=108),
        "points-home-game-sp-home": odd("points-home-game-sp-home", "points-away-game-sp-away", "points", "home", "sp", "home",
                                        {"pinnacle": bk("-105", "-4.5", "closeSpread")}, score=112),
        "points-away-game-sp-away": odd("points-away-game-sp-away", "points-home-game-sp-home", "points", "away", "sp", "away",
                                        {"pinnacle": bk("-105", "4.5", "closeSpread")}, score=108),
        "points-all-game-ou-under": odd("points-all-game-ou-under", "points-all-game-ou-over", "points", "all", "ou", "under",
                                        {"pinnacle": bk("-110", "229.5")}),  # no score -> results fallback
        "points-all-game-ou-over": odd("points-all-game-ou-over", "points-all-game-ou-under", "points", "all", "ou", "over",
                                       {"pinnacle": bk("-110", "229.5")}),
        f"rebounds-{L}-game-ou-over": odd(f"rebounds-{L}-game-ou-over", None, "rebounds", L, "ou", "over", {}, cancelled=True),
    },
}


class FakeSettleProvider:
    name = "sportsgameodds"

    def __init__(self, events):
        self.events = events
        self.calls = []

    def fetch_settlement(self, event_ids, bookmakers):
        self.calls.append(list(event_ids))
        data = {e["eventID"]: parse_settlement(e) for e in self.events if e["eventID"] in event_ids}
        return data, FetchResult(requests_made=1, objects_used=len(event_ids))


@pytest.fixture
def db(settings, nba_result):
    conn = connect(settings.db_file)
    props, _ = normalize_props(nba_result.props, nba_result.rosters, fetched_at=T, pickem_books=PICKEM)
    run = start_run(conn, "sportsgameodds", "nba", "replay", T)
    upsert_events(conn, nba_result.events, T)
    insert_props(conn, run, props)
    finish_run(conn, run, finished_at=T, requests_made=0, n_events=2, n_props=len(props), n_flagged=0,
               notices=[], skipped={}, raw_paths=[])
    res = find_edges(props, mcfg(), now=T, sharp_books=["pinnacle"], states=["NY", "WI"],
                     availability=load_availability(), payouts=load_payouts(), all_books=True)
    record_recommendations(conn, res.edges, "2026-10-24", T)
    return conn


def rec(conn, book, odd_id, line, event="SYNTH_NBA_LAL_DEN"):
    return conn.execute(
        "SELECT * FROM recommendations WHERE event_id = ? AND bookmaker = ? AND provider_market_id = ? AND line IS ?",
        (event, book, odd_id, line)).fetchone()


def test_recommendations_recorded_once_per_day(db):
    n = db.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    assert n > 10
    r = rec(db, "draftkings", f"points-{L}-game-ou-over", 24.5)
    assert r["price_american"] == -115 and r["fair_prob"] == approx(0.562044, abs=1e-6)
    assert r["times_seen"] == 1


def test_settle_grades_and_computes_clv(db, settings):
    bet_id = insert_bet(db, {**{k: rec(db, "draftkings", f"points-{L}-game-ou-over", 24.5)[k] for k in (
        "sport", "league", "event_id", "start_time", "market_kind", "entity", "player", "player_key", "team", "opponent",
        "stat_type", "period", "bet_type", "side", "bookmaker", "payout_type", "provider_market_id", "is_alt_line")},
        "line": 24.5, "price_american": -115, "stake": 20.0})
    p = FakeSettleProvider([SETTLED_EVENT])
    s = settle(db, settings, p, now=AFTER, cfg=mcfg())
    assert p.calls == [["SYNTH_NBA_GSW_PHX", "SYNTH_NBA_LAL_DEN"]] and s.objects_used == 2  # both games had recs

    r = rec(db, "draftkings", f"points-{L}-game-ou-over", 24.5)
    assert (r["result"], r["stat_value"]) == ("win", 27)
    # Pinnacle close -150/+130: 0.6 / (0.6 + 0.434783) = 0.579832
    # CLV = 0.579832 * (100/115) - 0.420168 = 0.504202 - 0.420168 = +8.403%
    assert r["close_fair_prob"] == approx(0.579832, abs=1e-6)
    assert r["clv_pct"] == approx(8.4034, abs=1e-3)
    assert r["close_source"] == "provider_close"
    assert (r["close_line"], r["close_price"], r["beat_close"]) == (24.5, -135, 1)  # -115 beat DK's -135 close

    bet = db.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    assert bet["result"] == "win" and bet["profit"] == approx(20 * 100 / 115)  # $17.39
    assert bet["clv_pct"] == approx(8.4034, abs=1e-3)


def test_settle_game_lines(db, settings):
    settle(db, settings, FakeSettleProvider([SETTLED_EVENT]), now=AFTER, cfg=mcfg())
    ml = rec(db, "fanduel", "points-home-game-ml-home", None)
    assert ml["result"] == "win"  # 112-108
    # Pinnacle close -180/+160: 0.642857 / (0.642857 + 0.384615) = 0.625668; FanDuel -178 bet:
    # CLV = 0.625668 * (100/178) - 0.374332 = 0.351499 - 0.374332 = -2.283%
    assert ml["clv_pct"] == approx(-2.2833, abs=1e-3)
    other_game = rec(db, "fanduel", "points-home-game-ml-home", None, event="SYNTH_NBA_GSW_PHX")
    assert other_game["result"] is None and other_game["settle_attempts"] == 1  # no data -> retried later
    sp = rec(db, "fanduel", "points-home-game-sp-home", -4.0)
    assert sp["result"] == "push"  # won by exactly 4
    under = rec(db, "draftkings", "points-all-game-ou-under", 229.5)
    assert (under["result"], under["stat_value"]) == ("win", 220)  # from results: 112 + 108


def test_grade_rules():
    ev = parse_settlement(SETTLED_EVENT)
    row = {"provider_market_id": f"rebounds-{L}-game-ou-over", "bet_type": "ou", "side": "over", "line": 7.5}
    assert grade(row, ev) == ("void", None)  # market cancelled
    pra = parse_settlement({**SETTLED_EVENT, "odds": {"x": odd("x", None, "points+rebounds+assists", L, "ou", "over", {})}})
    assert grade({"provider_market_id": "x", "bet_type": "ou", "side": "over", "line": 43.5}, pra) == ("win", 44)
    live = parse_settlement({**SETTLED_EVENT, "status": {"started": True, "ended": False, "finalized": False}})
    assert grade({"provider_market_id": f"points-{L}-game-ou-over", "bet_type": "ou", "side": "over", "line": 24.5}, live) == (None, None)
    assert profit("loss", 20, -115, "american_odds") == -20 and profit("push", 20, -115, "american_odds") == 0
    assert profit("win", 10, +150, "american_odds") == approx(15)


def test_snapshot_fallback_when_no_close_odds(db, settings):
    no_close = {**SETTLED_EVENT, "odds": {k: {**v, "byBookmaker": {}} for k, v in SETTLED_EVENT["odds"].items()}}
    settle(db, settings, FakeSettleProvider([no_close]), now=AFTER, cfg=mcfg())
    r = rec(db, "draftkings", f"points-{L}-game-ou-over", 24.5)
    # last pre-game snapshot = the one we recommended from, so CLV equals the original edge
    assert r["close_source"] == "last_snapshot"
    assert r["clv_pct"] == approx(r["ev_pct"], abs=1e-6)


def test_unfinished_games_retry_then_ungradable(db, settings):
    live = {**SETTLED_EVENT, "status": {"started": True, "ended": False, "finalized": False}}
    p = FakeSettleProvider([live])
    for i in range(6):
        settle(db, settings, p, now=AFTER + timedelta(hours=i), cfg=mcfg())
    r = rec(db, "draftkings", f"points-{L}-game-ou-over", 24.5)
    assert r["result"] == "ungradable" and r["settle_attempts"] == 6
    calls = len(p.calls)
    settle(db, settings, p, now=AFTER + timedelta(hours=9), cfg=mcfg())
    assert len(p.calls) == calls  # nothing left to fetch


def test_not_settled_before_games_finish(db, settings):
    p = FakeSettleProvider([SETTLED_EVENT])
    s = settle(db, settings, p, now=T + timedelta(hours=2), cfg=mcfg())  # tip-off was 3h after T
    assert p.calls == [] and s.pending == 0


def test_quota_caps_settlement(db, settings):
    settings.quota.monthly_objects = 0
    s = settle(db, settings, FakeSettleProvider([SETTLED_EVENT]), now=AFTER, cfg=mcfg())
    assert s.events == 0 and any("Quota" in w for w in s.warnings)


def test_second_sighting_keeps_first_price(settings, nba_result):
    conn = connect(settings.db_file)
    props, _ = normalize_props(nba_result.props, nba_result.rosters, fetched_at=T, pickem_books=PICKEM)
    res = find_edges(props, mcfg(), now=T, sharp_books=["pinnacle"], states=["NY"],
                     availability=load_availability(), payouts=load_payouts())
    e = next(x for x in res.edges if x.bookmaker == "draftkings" and x.player == "LeBron James" and x.line == 24.5)
    record_recommendations(conn, [e], "2026-10-24", T)
    e.price_american = -130
    record_recommendations(conn, [e], "2026-10-24", T + timedelta(hours=1))
    (r,) = conn.execute("SELECT * FROM recommendations").fetchall()
    assert r["price_american"] == -115 and r["times_seen"] == 2
