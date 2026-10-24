from datetime import UTC, datetime

import pytest

from bookerbud.models import MarketKind
from bookerbud.providers.sportsgameodds import _name_from_player_id, parse_american, parse_line, parse_ts


@pytest.mark.parametrize("raw, expected", [("+145", 145), ("-110", -110), (-104, -104), ("EVEN", 100), (None, None), ("", None)])
def test_parse_american(raw, expected):
    assert parse_american(raw) == expected


def test_parse_american_garbage_raises():
    with pytest.raises(ValueError):
        parse_american("n/a")


def test_parse_line_and_ts():
    assert parse_line("+9.5") == 9.5 and parse_line("24.5") == 24.5 and parse_line(None) is None
    assert parse_ts("2025-08-27T19:44:30.719Z") == datetime(2025, 8, 27, 19, 44, 30, 719000, tzinfo=UTC)


def test_name_from_player_id():
    assert _name_from_player_id("LEBRON_JAMES_1_NBA") == "Lebron James"
    assert _name_from_player_id("ROBERT_HASSELL_III_1_MLB") == "Robert Hassell Iii"


# --- Real response shape (SGO OpenAPI example: finished NYY vs WSH game) -----------------

def test_real_mlb_example_event(mlb_result):
    (ev,) = mlb_result.events
    assert ev.event_id == "mg55OPFOORFcu5v3FvfO"
    assert (ev.home_team, ev.away_team) == ("New York Yankees", "Washington Nationals")
    assert ev.start_time == datetime(2025, 8, 27, 17, 5, tzinfo=UTC)
    assert "AARON_JUDGE_1_MLB" in {p.provider_player_id for p in mlb_result.rosters[ev.event_id]}


def test_real_mlb_example_pickem_lines(mlb_result):
    tb = [p for p in mlb_result.props if p.provider_market_id == "batting_totalBases-AUSTIN_WELLS_1_MLB-game-ou-over"]
    by_book = {p.bookmaker: p for p in tb if not p.is_alt_line}
    assert set(by_book) >= {"fanduel", "draftkings", "underdog", "prizepicks"}
    assert by_book["fanduel"].line == 1.5 and by_book["fanduel"].price_american == 145
    assert by_book["prizepicks"].line == 0.5
    assert by_book["prizepicks"].deeplink == "https://app.prizepicks.com/?projections=6319058-o-0.5"
    assert by_book["underdog"].player_name == "Austin Wells"
    assert by_book["underdog"].player_team_id == "NEW_YORK_YANKEES_MLB"
    assert all(p.market_kind is MarketKind.PLAYER_PROP and p.entity == "player" for p in tb)


def test_real_mlb_example_game_lines(mlb_result):
    spread = {p.bookmaker: p for p in mlb_result.props if p.provider_market_id == "points-home-game-sp-home" and not p.is_alt_line}
    assert spread["pinnacle"].line == -9.5 and spread["pinnacle"].price_american == -312
    assert spread["pinnacle"].entity == "match" and spread["pinnacle"].market_kind is MarketKind.GAME_LINE
    ml = {p.bookmaker: p for p in mlb_result.props if p.provider_market_id == "points-away-game-ml-away"}
    assert ml["draftkings"].price_american == 10000 and ml["draftkings"].line is None
    assert not ml["draftkings"].available


def test_real_mlb_example_skips_3way(mlb_result):
    assert mlb_result.skipped_markets["bet_type:ml3way"] >= 1
    assert not any(p.bet_type == "ml3way" for p in mlb_result.props)


def test_only_requested_books_kept(offline_provider):
    from tests.conftest import load_fixture
    from bookerbud.models import Sport

    r = offline_provider.parse_payload(load_fixture("sgo_openapi_mlb_example.json"), Sport.MLB, ["pinnacle"])
    assert {p.bookmaker for p in r.props} == {"pinnacle"}


# --- Synthetic NBA slate (documented schema, invented prices) --------------------------

def _lebron_points(nba_result, side="over"):
    return [p for p in nba_result.props if p.provider_market_id == f"points-LEBRON_JAMES_1_NBA-game-ou-{side}"]


def test_pickem_entries_without_odds_field(nba_result):
    rows = {p.bookmaker: p for p in _lebron_points(nba_result) if not p.is_alt_line}
    assert rows["prizepicks"].price_american is None and rows["prizepicks"].line == 25.5
    assert rows["sleeper"].price_american is None
    assert rows["underdog"].price_american == 100  # placeholder kept raw; payout tables decide EV later
    assert rows["pinnacle"].line == 24.5 and rows["pinnacle"].price_american == -112


def test_alt_lines_are_expanded_and_marked(nba_result):
    dk = [p for p in _lebron_points(nba_result) if p.bookmaker == "draftkings"]
    main = [p for p in dk if not p.is_alt_line]
    alts = sorted((p.line, p.price_american) for p in dk if p.is_alt_line)
    assert len(main) == 1 and main[0].is_main_line is True
    assert alts == [(23.5, -160), (25.5, 120)]
    assert all(p.is_main_line is False for p in dk if p.is_alt_line)


def test_notice_status_and_skips_captured(nba_result):
    assert any("missing" in n for n in nba_result.notices)
    assert nba_result.skipped_markets == {"bet_type:ml3way": 1}
    jm = next(p for p in nba_result.props if p.provider_player_id == "JAMAL_MURRAY_1_NBA")
    assert jm.player_status == "questionable"


def test_player_missing_from_roster_gets_fallback_name(nba_result):
    bronny = next(p for p in nba_result.props if p.provider_player_id == "BRONNY_JAMES_1_NBA")
    assert bronny.player_name == "Bronny James" and bronny.player_team_id is None


def test_unavailable_entries_kept(nba_result):
    booker = [p for p in nba_result.props if p.provider_player_id == "DEVIN_BOOKER_1_NBA" and p.bookmaker == "fanduel"]
    assert booker and not any(p.available for p in booker)
