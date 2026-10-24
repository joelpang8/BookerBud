from datetime import UTC, datetime

from bookerbud.models import EventInfo, Flag, MarketKind, PayoutType, RawProp, RosterPlayer, Sport
from bookerbud.normalize import group_markets, normalize_props

NOW = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
PICKEM = {"prizepicks", "underdog", "sleeper"}


def norm(result):
    return normalize_props(result.props, result.rosters, fetched_at=NOW, pickem_books=PICKEM)


def test_lebron_points_lines_up_across_all_books(nba_result):
    props, _ = norm(nba_result)
    groups = group_markets(props)
    key = next(k for k in groups if k[4] == "nba:lebron_james" and k[5] == "points" and k[6] == "game")
    rows = groups[key]
    assert {p.bookmaker for p in rows} == {"pinnacle", "draftkings", "fanduel", "prizepicks", "underdog", "sleeper", "betmgm", "caesars"}
    assert {p.side for p in rows} == {"over", "under"}
    p = next(r for r in rows if r.bookmaker == "prizepicks")
    assert p.payout_type is PayoutType.PICKEM
    assert (p.team, p.opponent) == ("Los Angeles Lakers", "Denver Nuggets")
    assert p.player == "LeBron James" and p.player_status == "probable" and p.flags == []
    assert next(r for r in rows if r.bookmaker == "pinnacle").payout_type is PayoutType.AMERICAN_ODDS


def test_stat_labels_canonicalised(nba_result):
    props, _ = norm(nba_result)
    jokic = {p.stat_type for p in props if p.player_key == "nba:nikola_jokic"}
    assert jokic == {"pts_reb_ast", "assists"}
    curry = {p.stat_type for p in props if p.player_key == "nba:stephen_curry"}
    assert curry == {"threes_made"}


def test_unmapped_stat_flagged_not_dropped(nba_result):
    props, report = norm(nba_result)
    dd = [p for p in props if p.stat_label_raw == "doubleDoubles"]
    assert dd and all(Flag.UNMAPPED_STAT in p.flags for p in dd)
    assert dd[0].stat_type == "doubledoubles"
    assert report.unmapped_stats[("nba", "doubleDoubles")] == len(dd)


def test_player_not_in_roster_flagged(nba_result):
    props, report = norm(nba_result)
    bronny = [p for p in props if p.provider_player_id == "BRONNY_JAMES_1_NBA"]
    assert bronny and all({Flag.PLAYER_NOT_IN_ROSTER, Flag.UNKNOWN_TEAM} <= set(p.flags) for p in bronny)
    assert bronny[0].player_key == "nba:bronny_james"
    assert report.unmatched_players[("nba", "Bronny James")] == len(bronny)


def test_game_total_and_team_total_are_different_markets(nba_result):
    props, _ = norm(nba_result)
    totals = [p for p in props if p.market_kind is MarketKind.GAME_LINE and p.bet_type == "ou" and p.event_id == "SYNTH_NBA_LAL_DEN"]
    keys = {p.market_key for p in totals}
    assert len(keys) == 2
    team_total = next(p for p in totals if p.entity == "home")
    assert (team_total.team, team_total.opponent) == ("Denver Nuggets", "Los Angeles Lakers")
    game_total = next(p for p in totals if p.entity == "all")
    assert game_total.team is None


def test_moneyline_sides_share_market_and_get_teams(nba_result):
    props, _ = norm(nba_result)
    ml = [p for p in props if p.bet_type == "ml" and p.event_id == "SYNTH_NBA_LAL_DEN"]
    assert len({p.market_key for p in ml}) == 1  # both sides = one two-way market
    home = next(p for p in ml if p.side == "home")
    assert home.team == "Denver Nuggets" and home.opponent == "Los Angeles Lakers"
    away = next(p for p in ml if p.side == "away")
    assert away.team == "Los Angeles Lakers"
    spreads = [p for p in props if p.bet_type == "sp" and p.event_id == "SYNTH_NBA_LAL_DEN"]
    assert len({p.market_key for p in spreads}) == 1
    assert {(p.side, p.line) for p in spreads if p.bookmaker == "pinnacle"} == {("home", -4.5), ("away", 4.5)}


def test_report_counts(nba_result):
    props, report = norm(nba_result)
    assert report.total == len(props)
    assert report.single_book_markets >= 2  # Murray double-double (DK only), Bronny (PrizePicks only), team total
    assert report.flagged_rows == sum(len(p.flags) for p in props)


def _event():
    return EventInfo(provider="other", event_id="E1", sport=Sport.NBA, league="NBA", start_time=NOW,
                     home_team="Denver Nuggets", away_team="Los Angeles Lakers",
                     home_team_id="DEN", away_team_id="LAL")


def test_name_only_provider_matches_roster_spelling():
    """A provider that sends names, not IDs, still lines up with the roster."""
    ev = _event()
    raw = RawProp(provider="other", event=ev, market_kind=MarketKind.PLAYER_PROP, provider_market_id="x",
                  stat_label="Pts+Rebs+Asts", period="game", bet_type="ou", side="over", entity="player",
                  line=49.5, bookmaker="underdog", player_name="Nikola Jokic")
    roster = {"E1": [RosterPlayer(provider_player_id="J", name="Nikola Jokić", team_id="DEN")]}
    (p,), report = normalize_props([raw], roster, fetched_at=NOW, pickem_books=PICKEM)
    assert p.player == "Nikola Jokić" and p.player_key == "nba:nikola_jokic"
    assert p.team == "Denver Nuggets" and p.stat_type == "pts_reb_ast" and p.flags == []


def test_ambiguous_player_flagged():
    ev = _event()
    raw = RawProp(provider="other", event=ev, market_kind=MarketKind.PLAYER_PROP, provider_market_id="x",
                  stat_label="points", period="game", bet_type="ou", side="over", entity="player",
                  line=9.5, bookmaker="draftkings", provider_player_id="A")
    roster = {"E1": [RosterPlayer(provider_player_id="A", name="Josh Smith", team_id="DEN"),
                     RosterPlayer(provider_player_id="B", name="Josh Smith Jr.", team_id="LAL")]}
    (p,), _ = normalize_props([raw], roster, fetched_at=NOW, pickem_books=PICKEM)
    assert Flag.AMBIGUOUS_PLAYER in p.flags
