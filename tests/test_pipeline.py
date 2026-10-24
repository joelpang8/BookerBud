import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from bookerbud.models import Sport
from bookerbud.pipeline import day_window, fetch_and_store, replay_and_store
from bookerbud.providers.base import FetchResult
from bookerbud.storage import connect, latest_runs, query_player_props
from tests.conftest import FIXTURES, load_fixture

NY = ZoneInfo("America/New_York")


def test_day_window_normal_day():
    start, end = day_window(date(2026, 10, 24), NY)
    assert start.astimezone(UTC) == datetime(2026, 10, 24, 4, 0, tzinfo=UTC)
    assert end - start == timedelta(days=1)
    assert end.astimezone(UTC) == datetime(2026, 10, 25, 4, 0, tzinfo=UTC)


def test_day_window_dst_end_is_25_hours():
    start, end = day_window(date(2026, 11, 1), NY)  # clocks fall back
    assert (end.astimezone(UTC) - start.astimezone(UTC)) == timedelta(hours=25)


def _raw_record(tmp_path, name, fixture):
    path = tmp_path / name
    path.write_text(json.dumps({"provider": "sportsgameodds", "fetched_at": "2026-10-24T22:00:00+00:00",
                                "params": {}, "status": 200, "body": load_fixture(fixture)}))
    return path


def test_replay_and_store(tmp_path, settings, offline_provider):
    files = [_raw_record(tmp_path, "p1.json", "sgo_nba_page1.json"), _raw_record(tmp_path, "p2.json", "sgo_nba_page2.json")]
    conn = connect(settings.db_file)
    summary = replay_and_store(conn, settings, offline_provider, Sport.NBA, files)
    assert len(summary.result.events) == 2
    runs = latest_runs(conn)
    assert runs.iloc[0]["source"] == "replay" and runs.iloc[0]["n_events"] == 2
    df = query_player_props(conn, "curry")
    assert set(df["fetched_at"]) == {"2026-10-24T22:00:00+00:00"}


class FakeProvider:
    name = "fake"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch_slate(self, sport, starts_after, starts_before, bookmakers, *, include_started=False, league_id=None):
        self.calls.append((sport, starts_after, starts_before, tuple(bookmakers), include_started, league_id))
        return self.result


def test_fetch_and_store_uses_local_day_and_config(settings, nba_result):
    conn = connect(settings.db_file)
    fake = FakeProvider(nba_result)
    s = fetch_and_store(conn, settings, fake, Sport.NBA, date(2026, 10, 24))
    sport, start, end, books, include_started, league = fake.calls[0]
    assert start.astimezone(UTC) == datetime(2026, 10, 24, 4, tzinfo=UTC)
    assert books == ("pinnacle", "draftkings", "fanduel", "prizepicks", "underdog", "sleeper")
    assert include_started is False and league == "NBA"
    assert s.report.total == len(nba_result.props)
    run = latest_runs(conn).iloc[0]
    assert json.loads(run["skipped"]) == {"bet_type:ml3way": 1}
    assert "missing" in json.loads(run["notices"])[0]
