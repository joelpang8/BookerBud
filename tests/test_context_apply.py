from datetime import UTC, datetime

from bookerbud.context.apply import ContextRules, apply_context, load_context_rules, normalise_status
from bookerbud.context.models import GameContext, PlayerContext, Weather
from bookerbud.edge.engine import Edge

T = datetime(2026, 10, 24, 22, tzinfo=UTC)
RULES = load_context_rules()


def edge(sport="mlb", player="Christian Yelich", team="Milwaukee Brewers", stat="total_bases", conf="high",
         status=None, event="E1") -> Edge:
    return Edge(
        sport=sport, event_id=event, start_time=T, market_kind="player_prop" if player else "game_line",
        player=player, team=team, opponent="Tampa Bay Rays", stat_type=stat, period="game", bet_type="ou",
        side="over", line=1.5, bookmaker="draftkings", payout_type="american_odds", price_american=110,
        implied_prob=0.47, fair_prob=0.52, push_prob=0.0, fair_source="consensus", fair_books=["betmgm"],
        fair_line=1.5, line_gap=0.0, confidence=conf, ev_pct=5.0, kelly_full=0.02, stake=5.0,
        breakeven_slip=None, usable_in=["NY"], player_status=status, fetched_at=T,
    )


def mlb_game(**kw) -> GameContext:
    base = dict(event_id="E1", sport="mlb", home_team="Milwaukee Brewers", away_team="Tampa Bay Rays",
                probable_pitchers={"home": "Jacob Misiorowski", "away": "Drew Rasmussen"},
                lineups_posted={"home": False, "away": False}, lineups={})
    base.update(kw)
    return GameContext(**base)


def run(edges, games=None, players=None, rules=RULES):
    return apply_context(edges, {g.event_id: g for g in (games or [])}, players or {}, rules)


def test_normalise_status():
    assert normalise_status("IR") == "injured_list"
    assert normalise_status("Day-To-Day") == "day_to_day"
    assert normalise_status(None) is None


def test_status_rules_from_sgo():
    kept, dropped = run([edge(status="out"), edge(status="doubtful"), edge(status="questionable"), edge(status="probable")])
    assert dropped == {"status:out": 1, "status:doubtful": 1}
    q, p = kept
    assert q.confidence == "medium" and "status: questionable (confidence lowered)" in q.context_notes
    assert p.confidence == "high" and p.context_notes == ["status: probable"]


def test_mlb_roster_status_drops_il_player():
    players = {"mlb:christian_yelich": PlayerContext(player_key="mlb:christian_yelich", name="Christian Yelich",
                                                     status="injured_list", detail="Injured 10-Day", source="mlb")}
    kept, dropped = run([edge()], players=players)
    assert kept == [] and dropped == {"status:injured_list": 1}


def test_lineup_rules():
    posted = mlb_game(lineups_posted={"home": True, "away": False},
                      lineups={"home": ["Jackson Chourio", "Christian Yelich"], "away": []})
    kept, _ = run([edge()], [posted])
    assert kept[0].context_notes == ["in posted lineup"]
    kept, dropped = run([edge(player="Rhys Hoskins")], [posted])
    assert kept == [] and dropped == {"not_in_lineup": 1}
    # away lineup not posted yet -> no lineup judgement for the away team
    kept, _ = run([edge(player="Yandy Díaz", team="Tampa Bay Rays")], [posted])
    assert kept and kept[0].context_notes == []


def test_pitcher_rules():
    g = mlb_game()
    kept, _ = run([edge(player="Jacob Misiorowski", stat="pitcher_strikeouts")], [g])
    assert kept and kept[0].confidence == "high"
    kept, dropped = run([edge(player="Freddy Peralta", stat="pitcher_strikeouts")], [g])
    assert kept == [] and dropped == {"not_probable_starter": 1}
    tbd = mlb_game(probable_pitchers={"home": None, "away": "Drew Rasmussen"})
    kept, _ = run([edge(player="Freddy Peralta", stat="pitcher_strikeouts")], [tbd])
    assert kept[0].confidence == "medium" and "probable starter not announced" in kept[0].context_notes[0]


def test_weather_rules():
    rainy = mlb_game(weather=Weather(precip_prob=70, wind_mph=15, wind_from_deg=0, wind_out_mph=12, roof="Open"))
    kept, _ = run([edge(player=None, team=None, stat="runs")], [rainy])
    notes = kept[0].context_notes
    assert kept[0].confidence == "medium"
    assert "rain risk 70% at first pitch (confidence lowered)" in notes and "wind blowing out 12 mph" in notes
    dome = mlb_game(weather=Weather(roof="Dome"))
    kept, _ = run([edge(player=None, team=None, stat="runs")], [dome])
    assert kept[0].context_notes == [] and kept[0].confidence == "high"


def test_nba_back_to_back_note_and_downgrade():
    g = GameContext(event_id="N1", sport="nba", home_team="Denver Nuggets", away_team="Los Angeles Lakers",
                    rest_days={"home": 1, "away": 0})
    lakers = edge(sport="nba", player="LeBron James", team="Los Angeles Lakers", stat="points", event="N1")
    nuggets = edge(sport="nba", player="Nikola Jokić", team="Denver Nuggets", stat="points", event="N1")
    total = edge(sport="nba", player=None, team=None, stat="points", event="N1")
    kept, _ = run([lakers, nuggets, total], [g])
    assert kept[0].context_notes == ["Los Angeles Lakers on a back-to-back"]
    assert kept[1].context_notes == []
    assert kept[2].context_notes == ["Los Angeles Lakers on a back-to-back"]  # game lines see both teams
    strict = ContextRules(status=RULES.status, nba={"back_to_back": "downgrade"})
    kept, _ = run([edge(sport="nba", player="LeBron James", team="Los Angeles Lakers", stat="points", event="N1")], [g], rules=strict)
    assert kept[0].confidence == "medium"


def test_no_context_is_a_no_op():
    e = edge()
    kept, dropped = run([e])
    assert kept == [e] and not dropped and e.context_notes == []
