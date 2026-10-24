import json
from collections import Counter
from datetime import UTC, date, datetime

import httpx
import pytest

from bookerbud.context.http import ContextSourceError, JsonClient
from bookerbud.context.mlb import MLBContext, match_game, parse_roster, parse_schedule
from bookerbud.context.weather import OpenMeteo, parse_hourly, wind_out_component
from bookerbud.models import EventInfo, Sport
from bookerbud.raw_log import RawLogger
from tests.conftest import load_fixture

approx = pytest.approx


# ----------------------------------------------------------------- weather
def test_wind_out_component_hand_checked():
    # Wrigley CF bearing 37 deg. Wind FROM 217 blows TO 37 -> straight out.
    assert wind_out_component(10, 217, 37) == approx(10)
    assert wind_out_component(10, 37, 37) == approx(-10)  # from CF -> blowing in
    assert wind_out_component(10, 127, 37) == approx(0, abs=1e-9)  # pure crosswind
    # 60 deg off straight-out: cos(60) = 0.5
    assert wind_out_component(10, 277, 37) == approx(5)


def test_parse_hourly_picks_game_hour():
    body = load_fixture("openmeteo_milwaukee.json")
    h = parse_hourly(body, datetime(2026, 10, 25, 0, 8, tzinfo=UTC))
    i = body["hourly"]["time"].index("2026-10-25T00:00")
    assert h["temperature_2m"] == body["hourly"]["temperature_2m"][i]
    assert parse_hourly(body, datetime(2030, 1, 1, tzinfo=UTC)) == {}


# ------------------------------------------------------------------- MLB
def test_parse_schedule_real_response():
    (g,) = parse_schedule(load_fixture("mlb_schedule_2026-10-23.json"))
    assert (g["away"], g["home"]) == ("Tampa Bay Rays", "Milwaukee Brewers")
    assert g["home_pp"] == "Jacob Misiorowski"
    assert len(g["home_lineup"]) == 9 and len(g["away_lineup"]) == 9
    assert "Christian Yelich" in g["home_lineup"]
    assert (g["cf_bearing"], g["roof"]) == (129.0, "Retractable")
    assert g["lat"] == approx(43.02838)


def test_parse_roster_statuses():
    players = parse_roster(load_fixture("mlb_roster_158.json"), "Milwaukee Brewers")
    assert Counter(p.status for p in players) == {"active": 26, "not_active_roster": 13, "injured_list": 3}
    il = next(p for p in players if p.status == "injured_list")
    assert il.detail == "Injured 60-Day" and il.player_key.startswith("mlb:")


def _event(home="Milwaukee Brewers", away="Tampa Bay Rays", start=datetime(2026, 10, 24, 0, tzinfo=UTC), eid="E1"):
    return EventInfo(provider="sgo", event_id=eid, sport=Sport.MLB, league="MLB", start_time=start,
                     home_team=home, away_team=away)


def test_match_game_handles_doubleheaders():
    g = {"home": "Milwaukee Brewers", "away": "Tampa Bay Rays", "start": datetime(2026, 10, 24, 23, tzinfo=UTC)}
    early = _event(start=datetime(2026, 10, 24, 17, tzinfo=UTC), eid="early")
    late = _event(start=datetime(2026, 10, 24, 23, 5, tzinfo=UTC), eid="late")
    assert match_game([early, late], g).event_id == "late"
    assert match_game([_event(home="Chicago Cubs")], g) is None


def _mock_client(tmp_path, routes: dict[str, dict]) -> JsonClient:
    def handler(req: httpx.Request) -> httpx.Response:
        for frag, body in routes.items():
            if frag in str(req.url):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    return JsonClient("test", RawLogger(tmp_path / "raw"), client=httpx.Client(transport=httpx.MockTransport(handler)),
                      sleep=lambda s: None)


def test_mlb_context_build_end_to_end(tmp_path):
    client = _mock_client(tmp_path, {
        "/schedule": load_fixture("mlb_schedule_2026-10-23.json"),
        "/roster": load_fixture("mlb_roster_158.json"),
        "open-meteo": load_fixture("openmeteo_milwaukee.json"),
    })
    ctx = MLBContext(client, OpenMeteo(client))
    games, players, warnings = ctx.build([_event(), _event(home="Chicago Cubs", eid="nogame")], date(2026, 10, 23))
    (g,) = games
    assert g.event_id == "E1" and g.lineups_posted == {"home": True, "away": True}
    assert g.probable_pitchers["home"] == "Jacob Misiorowski"
    assert g.weather is not None and g.weather.roof == "Retractable" and g.weather.temp_f is not None
    assert len(players) == 84  # 42-man fixture used for both teams
    assert any("not found in the MLB schedule" in w for w in warnings)
    assert list((tmp_path / "raw").rglob("*.json"))  # every response logged


def test_dome_skips_weather_call(tmp_path):
    def handler(req):
        raise AssertionError("no network for domes")

    om = OpenMeteo(JsonClient("om", RawLogger(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler))))
    w = om.at(27.77, -82.65, datetime(2026, 7, 1, tzinfo=UTC), cf_bearing=45, roof="Dome")
    assert w.roof == "Dome" and w.describe() == "dome"


def test_json_client_retries_then_raises(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503, text="busy")

    c = JsonClient("x", RawLogger(tmp_path), client=httpx.Client(transport=httpx.MockTransport(handler)),
                   max_retries=2, sleep=lambda s: None)
    with pytest.raises(ContextSourceError, match="503"):
        c.get("https://example.test/a", {}, "a")
    assert calls["n"] == 3
