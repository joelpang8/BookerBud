import pytest

from bookerbud.normalize.stats import StatMatcher, normalize_label


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("batting_totalBases", "batting totalbases"),
        ("Pts + Rebs + Asts", "pts+rebs+asts"),
        ("points+rebounds+assists", "points+rebounds+assists"),
        ("3-PT Made", "3 pt made"),
        ("  Total   Bases ", "total bases"),
    ],
)
def test_normalize_label(raw, expected):
    assert normalize_label(raw) == expected


@pytest.mark.parametrize(
    "sport, label, canonical",
    [
        ("nba", "points", "points"),
        ("nba", "points+rebounds+assists", "pts_reb_ast"),
        ("nba", "Pts+Rebs+Asts", "pts_reb_ast"),
        ("nba", "threePointersMade", "threes_made"),
        ("mlb", "batting_totalBases", "total_bases"),
        ("mlb", "Total Bases", "total_bases"),
        ("mlb", "pitching_strikeouts", "pitcher_strikeouts"),
        ("mlb", "batting_strikeouts", "batter_strikeouts"),
        ("mlb", "points", "runs"),
        ("nfl", "passing_yards", "passing_yards"),
        ("nfl", "Rush Yards", "rushing_yards"),
    ],
)
def test_stat_matcher_known(sport, label, canonical):
    assert StatMatcher(sport).match(label) == canonical


def test_stat_matcher_unknown_and_sport_scoped():
    assert StatMatcher("nba").match("doubleDoubles") is None
    assert StatMatcher("nba").match("batting_totalBases") is None  # MLB label isn't an NBA stat


def test_custom_alias_table_and_display():
    m = StatMatcher("nba", {"nba": {"pra": {"label": "PRA", "aliases": ["P+R+A"]}}})
    assert m.match("p + r + a") == "pra"
    assert m.display("pra") == "PRA"
    assert m.display("unknown") == "unknown"


@pytest.mark.parametrize(
    "sport, label, canonical",
    [  # statIDs seen in the first live SportsGameOdds run (2026-10-24)
        ("mlb", "batting_hits+runs+rbi", "hits_runs_rbis"),
        ("mlb", "batting_runs+rbi", "runs_rbis"),
        ("mlb", "batting_singles", "singles"),
        ("mlb", "batting_doubles", "doubles"),
        ("mlb", "batting_triples", "triples"),
        ("mlb", "batting_stolenBases", "stolen_bases"),
        ("mlb", "batting_basesOnBalls", "batter_walks"),
        ("mlb", "pitching_basesOnBalls", "pitcher_walks"),
        ("mlb", "pitching_hits", "hits_allowed"),
        ("mlb", "pitching_win", "pitcher_win"),
        ("nba", "doubleDouble", "double_double"),
        ("nba", "tripleDouble", "triple_double"),
        ("nba", "firstBasket", "first_basket"),
        ("nba", "firstToScore", "first_to_score"),
    ],
)
def test_live_stat_ids(sport, label, canonical):
    assert StatMatcher(sport).match(label) == canonical
