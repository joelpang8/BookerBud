"""Engine tests on the synthetic slate. Expected numbers use the
*multiplicative* devig so they can be checked by hand (arithmetic in comments)."""

from datetime import UTC, datetime, timedelta

import pytest

from bookerbud.edge.config import EdgeConfig, SlipTable, load_availability, load_edge_config, load_payouts
from bookerbud.edge.engine import Edge, book_pairs, find_edges, from_threshold, to_threshold
from bookerbud.edge.slips import build_slips
from bookerbud.normalize import group_markets, normalize_props

T = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
PICKEM = {"prizepicks", "underdog", "sleeper"}
approx = pytest.approx


@pytest.fixture
def props(nba_result):
    out, _ = normalize_props(nba_result.props, nba_result.rosters, fetched_at=T, pickem_books=PICKEM)
    return out


def cfg(**filters) -> EdgeConfig:
    c = load_edge_config()
    c.devig_method = "multiplicative"
    c.filters.min_edge_pct = -100  # keep everything unless a test says otherwise
    for k, v in filters.items():
        setattr(c.filters, k, v)
    return c


def run(props, c=None, states=("NY", "WI"), now=T, all_books=False):
    return find_edges(
        props, c or cfg(), now=now, sharp_books=["pinnacle"], states=list(states),
        availability=load_availability(), payouts=load_payouts(), all_books=all_books,
    )


def pick(res, book, player_or_team, stat, side, line):
    hits = [e for e in res.edges + res.pickem_legs if e.bookmaker == book and (e.player or e.team) == player_or_team
            and e.stat_type == stat and e.side == side and e.line == line]
    assert hits, f"no edge for {book} {player_or_team} {stat} {side} {line}"
    return hits[0]


def test_threshold_mapping():
    assert to_threshold("ou", "over", 24.5) == (True, 24.5)
    assert to_threshold("ou", "under", 24.5) == (False, 24.5)
    assert to_threshold("sp", "home", -4.5) == (True, 4.5)  # home margin over 4.5
    assert to_threshold("sp", "away", 4.5) == (False, 4.5)  # same market, other side
    assert to_threshold("ml", "away", None) == (False, None)
    assert from_threshold("sp", "home", 4.5) == -4.5 and from_threshold("sp", "away", 4.5) == 4.5


def test_pinnacle_exact_line_sportsbook_ev_and_kelly(props):
    e = pick(run(props), "draftkings", "LeBron James", "points", "over", 24.5)
    # Pinnacle -140/+120: q = 140/240 = 0.583333, 100/220 = 0.454545; p_over = 0.583333/1.037879 = 0.562044
    assert e.fair_prob == approx(0.562044, abs=1e-6)
    assert e.implied_prob == approx(115 / 215)  # -115 -> 0.534884
    # EV = 0.562044 * (100/115) - 0.437956 = 0.488734 - 0.437956 = 0.050778
    assert e.ev_pct == approx(5.0778, abs=1e-3)
    # Kelly = 0.050778 / 0.869565 = 0.058395; quarter on $1,000 = $14.60
    assert e.kelly_full == approx(0.058395, abs=1e-6)
    assert e.stake == 14.60
    assert (e.fair_source, e.confidence, e.line_gap) == ("pinnacle", "high", 0.0)
    assert e.usable_in == ["NY"]  # DK isn't live in WI


def test_moved_line_is_medium_confidence(props):
    e = pick(run(props), "fanduel", "LeBron James", "points", "over", 25.5)
    assert e.confidence == "medium" and e.fair_line == 24.5 and e.line_gap == 1.0
    assert e.fair_prob < 0.562044  # higher line -> lower over probability


def test_free_tier_consensus_excludes_target_book(props):
    """No Pinnacle on this game: fair = mean of every other priced book."""
    e = pick(run(props), "fanduel", "Stephen Curry", "threes_made", "over", 4.5)
    assert e.fair_source == "consensus" and e.confidence == "low"
    assert e.fair_books == ["betmgm", "caesars", "draftkings", "williamhill"]  # fanduel excluded
    # betmgm -130/+100: 0.565217/(0.565217+0.5)        = 0.530612
    # caesars -128/-102: 0.561404/(0.561404+0.504950)  = 0.526433
    # dk & williamhill -125/-105: 0.555556/(0.555556+0.512195) = 0.520325 (x2)
    # mean = 0.524424; EV at +105 = 0.524424*1.05 - 0.475576 = 0.075069
    assert e.fair_prob == approx(0.524424, abs=1e-5)
    assert e.ev_pct == approx(7.507, abs=1e-2)


def test_spread_sides_pair_up(props):
    e = pick(run(props), "draftkings", "Denver Nuggets", "points", "home", -4.5)
    # Pinnacle home -4.5 -104 / away +4.5 -106: 0.509804 / (0.509804 + 0.514563) = 0.509804 / 1.024367 = 0.497677
    assert e.fair_prob == approx(0.497677, abs=1e-6) and e.confidence == "high"
    away = pick(run(props), "draftkings", "Los Angeles Lakers", "points", "away", 4.5)
    assert away.fair_prob == approx(1 - 0.497677, abs=1e-6)


def test_spread_half_point_move_has_push(props):
    e = pick(run(props), "fanduel", "Denver Nuggets", "points", "home", -4.0)
    assert e.push_prob > 0 and e.line_gap == 0.5 and e.confidence == "medium"


def test_pickem_leg_edge_vs_breakeven(props):
    e = pick(run(props), "underdog", "LeBron James", "points", "over", 24.5)
    # Underdog's lowest break-even is standard 2-pick: 1/sqrt(3.5) = 0.534522
    assert e.implied_prob == approx(3.5 ** -0.5, abs=1e-6) and e.breakeven_slip == "standard-2"
    assert e.ev_pct == approx((0.562044 / 3.5 ** -0.5 - 1) * 100, abs=1e-3)
    assert e.stake == 0.0  # pick'em is staked per slip, not per leg


def test_filters(props):
    res = run(props, cfg(min_edge_pct=5.0))
    assert res.edges and all(e.ev_pct >= 5 for e in res.edges)
    assert res.skipped["below_min_edge"] > 0
    stale = run(props, now=T + timedelta(minutes=31))
    assert stale.edges == [] and stale.skipped["stale"] > 0


def test_require_sharp_drops_consensus(props):
    c = cfg()
    c.fair.require_sharp = True
    res = run(props, c)
    assert res.edges and all(e.fair_source == "pinnacle" for e in res.edges)
    assert res.skipped["no_sharp_price"] > 0


def test_min_consensus_books(props):
    c = cfg()
    c.fair.min_consensus_books = 5  # Curry has 5 books when the target is a pick'em app, 4 for FanDuel
    res = run(props, c)
    consensus = [e for e in res.edges + res.pickem_legs if e.fair_source == "consensus"]
    assert consensus and all(len(e.fair_books) >= 5 for e in consensus)
    c.fair.min_consensus_books = 6
    res = run(props, c)
    assert not [e for e in res.edges + res.pickem_legs if e.fair_source == "consensus"]


def test_line_gap_cap_skips(props):
    c = cfg()
    c.stat_models["nba"]["points"]["max_gap"] = 0.5
    res = run(props, c)
    with pytest.raises(AssertionError):
        pick(res, "fanduel", "LeBron James", "points", "over", 25.5)  # 1.0 from Pinnacle 24.5
    assert res.skipped["line_gap_too_large"] > 0


def test_state_availability(props):
    ny = run(props, states=("NY",))
    assert not [e for e in ny.edges + ny.pickem_legs if e.bookmaker in ("underdog", "sleeper")]
    assert ny.skipped["not_usable_in_your_states"] > 0
    wi = run(props, states=("WI",))
    assert not [e for e in wi.edges if e.bookmaker in ("draftkings", "fanduel")]
    everything = run(props, states=("WI",), all_books=True)
    assert [e for e in everything.edges if e.bookmaker == "draftkings" and e.usable_in == []]


def test_closed_and_suspicious(props):
    res = run(props)
    assert res.skipped["closed"] == 2  # Booker FanDuel market is closed
    tight = cfg()
    tight.filters.max_ev_pct = 5.0
    assert run(props, tight).skipped["suspicious_ev"] > 0


def test_book_pairs_prefers_main_line(props):
    groups = group_markets(props)
    rows = next(v for k, v in groups.items() if k[4] == "nba:lebron_james" and k[5] == "points")
    pairs = book_pairs(rows, "multiplicative")
    assert {p.threshold for p in pairs["pinnacle"]} == {24.5, 26.5}
    assert "prizepicks" not in pairs  # pick'em has no prices to devig


# ------------------------------------------------------------------ slips
def leg(player, team, event, p, app="prizepicks") -> Edge:
    return Edge(
        sport="nba", event_id=event, start_time=T, market_kind="player_prop", player=player, team=team,
        opponent="X", stat_type="points", period="game", bet_type="ou", side="over", line=20.5, bookmaker=app,
        payout_type="pickem", price_american=None, implied_prob=0.55, fair_prob=p, push_prob=0.0,
        fair_source="pinnacle", fair_books=["pinnacle"], fair_line=20.5, line_gap=0.0, confidence="high",
        ev_pct=0.0, kelly_full=0.0, stake=0.0, breakeven_slip="power-2", usable_in=["NY"], player_status=None,
        fetched_at=T,
    )


PP = {"prizepicks": SlipTable(verified=True, slips={"power": {2: {2: 3.0}}, "flex": {2: {2: 2.0, 1: 0.5}}})}


def test_slip_ev_and_kelly():
    c = load_edge_config()
    slips = build_slips([leg("A", "T1", "E1", 0.6), leg("B", "T2", "E2", 0.6)], PP, c, sizes=range(2, 3))
    # power: 0.36*3 - 1 = +8%; flex: 0.36*2 + 0.48*0.5 - 1 = -4% (dropped)
    assert [(s.kind, round(s.ev_pct, 6)) for s in slips] == [("power", 8.0)]
    s = slips[0]
    # Kelly b=2, p=0.36: (2*0.36 - 0.64)/2 = 0.04; quarter of $1,000 = $10.00
    assert s.kelly_full == approx(0.04, abs=1e-9) and s.stake == 10.0
    assert s.win_prob == approx(0.36)


def test_same_game_excluded_then_penalised():
    c = load_edge_config()
    legs = [leg("A", "T1", "E1", 0.6), leg("B", "T2", "E1", 0.6)]
    assert build_slips(legs, PP, c, sizes=range(2, 3)) == []
    c.slips.same_game = "penalize"
    (s,) = build_slips(legs, PP, c, sizes=range(2, 3))
    # 0.6 * 0.97 = 0.582; 0.582^2 * 3 - 1 = 0.016172
    assert s.ev_pct == approx(1.6172, abs=1e-4) and s.same_game_pairs == 1


def test_slip_rules_unique_player_and_two_teams():
    c = load_edge_config()
    same_player = [leg("A", "T1", "E1", 0.7), leg("A", "T1", "E2", 0.7)]
    assert build_slips(same_player, PP, c, sizes=range(2, 3)) == []
    same_team = [leg("A", "T1", "E1", 0.7), leg("B", "T1", "E2", 0.7)]
    assert build_slips(same_team, PP, c, sizes=range(2, 3)) == []


def test_slips_from_engine_flags_unverified_tables(props):
    res = run(props)
    slips = build_slips(res.pickem_legs, load_payouts(), load_edge_config(), sizes=range(2, 4))
    assert slips and all(s.ev_pct > 0 for s in slips)
    assert all(not s.verified_table for s in slips if s.app == "underdog")
    assert not [s for s in slips if s.app == "sleeper"]  # no fixed payout table


def test_started_games_never_recommended(props):
    late = run(props, now=datetime(2026, 10, 25, 1, 5, tzinfo=UTC))  # LAL@DEN tipped at 01:00Z
    assert not [e for e in late.edges if e.event_id == "SYNTH_NBA_LAL_DEN"]
    assert late.skipped["game_started"] > 0


def test_partial_periods_off_by_default(props):
    q1 = [p.model_copy(update={"period": "1q", "provider_market_id": p.provider_market_id.replace("-game-", "-1q-")})
          for p in props]
    res = run(q1)
    assert res.edges == [] and res.skipped["period_not_enabled"] > 0
    c = cfg()
    c.filters.periods = ["game", "1q"]
    assert run(q1, c).edges  # opt-in works


def test_period_scaled_models():
    c = load_edge_config()
    game = c.model_for("nba", "game_total")
    q1 = c.model_for("nba", "game_total", "1q")
    assert q1.sd == pytest.approx(game.sd * 0.5) and q1.max_gap == pytest.approx(game.max_gap * 0.5)
    pts, pts_h = c.model_for("nba", "points"), c.model_for("nba", "points", "1h")
    assert pts_h.cv == pytest.approx(pts.cv / 0.5 ** 0.5)
    assert c.model_for("nba", "game_total", "weird").max_gap == 0.0
    assert c.model_for("mlb", "total_bases", "1i").kind == "poisson"
