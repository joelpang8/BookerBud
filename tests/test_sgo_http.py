import json
from datetime import UTC, datetime

import httpx
import pytest

from bookerbud.config import SportsGameOddsSettings
from bookerbud.models import Sport
from bookerbud.providers.base import ProviderAuthError, ProviderError
from bookerbud.providers.sportsgameodds import SportsGameOddsProvider
from bookerbud.raw_log import RawLogger
from tests.conftest import ALL_BOOKS, ALL_LEAGUES, NBA, load_fixture

START = datetime(2026, 10, 24, 4, 0, tzinfo=UTC)
END = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def now(self) -> float:
        return self.t


def make_provider(tmp_path, handler, *, key="secret-key", **cfg):
    clock = FakeClock()
    client = httpx.Client(base_url="https://api.test/v2", transport=httpx.MockTransport(handler))
    p = SportsGameOddsProvider(
        key, SportsGameOddsSettings(**cfg), RawLogger(tmp_path / "raw"), client=client, sleep=clock.sleep, clock=clock.now
    )
    return p, clock


def test_paginates_sends_params_and_logs_raw(tmp_path):
    seen: list[httpx.Request] = []
    pages = {None: load_fixture("sgo_nba_page1.json"), "n.SYNTH.page2": load_fixture("sgo_nba_page2.json")}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=pages[req.url.params.get("cursor")])

    p, clock = make_provider(tmp_path, handler)
    r = p.fetch_slate(NBA, START, END, ALL_BOOKS)

    assert len(seen) == 2 and r.requests_made == 2
    q = seen[0].url.params
    assert seen[0].url.path == "/v2/events"
    assert seen[0].headers["x-api-key"] == "secret-key"
    assert "apiKey" not in q  # key only in header
    assert q["leagueID"] == "NBA"
    assert q["startsAfter"] == "2026-10-24T04:00:00Z" and q["startsBefore"] == "2026-10-25T04:00:00Z"
    assert q["bookmakerID"] == "pinnacle,draftkings,fanduel,prizepicks,underdog,sleeper"
    assert q["oddsAvailable"] == "true" and q["started"] == "false" and q["includeAltLines"] == "true"
    assert seen[1].url.params["cursor"] == "n.SYNTH.page2"

    assert {e.event_id for e in r.events} == {"SYNTH_NBA_LAL_DEN", "SYNTH_NBA_GSW_PHX"}
    assert r.objects_used == 2 and q["limit"] == "25" and not r.truncated
    assert len(r.raw_paths) == 2
    for path in r.raw_paths:
        text = path.read_text()
        assert "secret-key" not in text
        assert json.loads(text)["body"]["success"] is True
    # 10 req/min -> second request waits ~6s
    assert clock.sleeps == [pytest.approx(6.0)]


def test_include_started_drops_filter(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"success": True, "data": []})

    p, _ = make_provider(tmp_path, handler)
    r = p.fetch_slate(ALL_LEAGUES, START, END, ALL_BOOKS, include_started=True)
    assert "started" not in seen[0].url.params
    assert seen[0].url.params["leagueID"] == "NBA,MLB,NFL"  # one call for every league
    assert r.objects_used == 1  # an empty response still bills one object


def test_retries_429_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"success": False, "error": "Rate limit"}, headers={"retry-after": "3"})
        return httpx.Response(200, json={"success": True, "data": []})

    p, clock = make_provider(tmp_path, handler)
    r = p.fetch_slate(NBA, START, END, ALL_BOOKS)
    assert calls["n"] == 2 and r.events == []
    assert 3.0 in clock.sleeps
    # both attempts were logged to disk
    assert len(list((tmp_path / "raw").rglob("*.json"))) == 2


def test_gives_up_after_max_retries(tmp_path):
    p, _ = make_provider(tmp_path, lambda req: httpx.Response(503, text="down"), max_retries=2)
    with pytest.raises(ProviderError, match="503"):
        p.fetch_slate(NBA, START, END, ALL_BOOKS)


def test_auth_errors_not_retried(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(401, json={"success": False, "error": "Invalid API key"})

    p, _ = make_provider(tmp_path, handler)
    with pytest.raises(ProviderAuthError, match="Invalid API key"):
        p.fetch_slate(NBA, START, END, ALL_BOOKS)
    assert calls["n"] == 1


def test_missing_key_fails_before_network(tmp_path):
    def handler(req):
        raise AssertionError("should not be called")

    p, _ = make_provider(tmp_path, handler, key=None)
    with pytest.raises(ProviderAuthError, match="SPORTSGAMEODDS_API_KEY"):
        p.fetch_slate(NBA, START, END, ALL_BOOKS)


def test_success_false_raises(tmp_path):
    p, _ = make_provider(tmp_path, lambda req: httpx.Response(200, json={"success": False, "error": "bad leagueID"}))
    with pytest.raises(ProviderError, match="bad leagueID"):
        p.fetch_slate(NBA, START, END, ALL_BOOKS)


def test_usage_endpoint(tmp_path):
    def handler(req):
        assert req.url.path == "/v2/account/usage"
        return httpx.Response(200, json={"success": True, "data": {"keyID": "abc", "isActive": True}})

    p, _ = make_provider(tmp_path, handler)
    assert p.usage() == {"keyID": "abc", "isActive": True}


def test_max_objects_caps_page_size_and_stops(tmp_path):
    seen = []
    pages = {None: load_fixture("sgo_nba_page1.json"), "n.SYNTH.page2": load_fixture("sgo_nba_page2.json")}

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=pages[req.url.params.get("cursor")])

    p, _ = make_provider(tmp_path, handler)
    r = p.fetch_slate(NBA, START, END, ALL_BOOKS, max_objects=1)
    assert len(seen) == 1 and seen[0].url.params["limit"] == "1"
    assert r.truncated and r.objects_used == 1
    assert any("quota budget" in n for n in r.notices)


def test_fetch_schedule_is_events_only_and_capped(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=load_fixture("sgo_nba_page2.json") | {"nextCursor": None})

    p, _ = make_provider(tmp_path, handler)
    r = p.fetch_schedule(NBA, START, END, max_objects=5)
    q = seen[0].url.params
    assert "oddsAvailable" not in q and "started" not in q  # finished games included
    assert q["oddID"] == "points-home-game-ml-home" and q["limit"] == "5"
    assert [e.event_id for e in r.events] == ["SYNTH_NBA_GSW_PHX"] and r.props == []
    assert r.objects_used == 1


@pytest.mark.parametrize("include_close", [True, False])
def test_fetch_settlement_close_param(tmp_path, include_close):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"success": True, "data": [load_fixture("sgo_openapi_mlb_example.json")["data"][0]]})

    p, _ = make_provider(tmp_path, handler)
    data, meta = p.fetch_settlement(["mg55OPFOORFcu5v3FvfO"], ALL_BOOKS, include_close=include_close)
    q = seen[0].url.params
    assert q["eventIDs"] == "mg55OPFOORFcu5v3FvfO"
    assert ("includeOpenCloseOdds" in q) is include_close
    assert data["mg55OPFOORFcu5v3FvfO"].finalized and meta.objects_used == 1
