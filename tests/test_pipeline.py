import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from bookerbud.models import Sport
import pytest

from bookerbud.pipeline import QuotaExceeded, day_window, fetch_and_store, quota_status, replay_and_store
from bookerbud.providers.base import FetchResult
from bookerbud.storage import connect, finish_run, latest_runs, query_player_props, start_run
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
    summary = replay_and_store(conn, settings, offline_provider, [Sport.NBA], files)
    assert len(summary.result.events) == 2
    runs = latest_runs(conn)
    assert runs.iloc[0]["source"] == "replay" and runs.iloc[0]["n_events"] == 2
    assert quota_status(conn, settings).used == 0  # replays never count against the quota
    df = query_player_props(conn, "curry")
    assert set(df["fetched_at"]) == {"2026-10-24T22:00:00+00:00"}


class FakeProvider:
    name = "sportsgameodds"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def fetch_slate(self, leagues, starts_after, starts_before, bookmakers, *, include_started=False, max_objects=None):
        self.calls.append((leagues, starts_after, starts_before, tuple(bookmakers), include_started, max_objects))
        self.result.objects_used = 2
        return self.result


def test_fetch_and_store_uses_local_day_and_config(settings, nba_result):
    conn = connect(settings.db_file)
    fake = FakeProvider(nba_result)
    s = fetch_and_store(conn, settings, fake, [Sport.NBA, Sport.NFL], date(2026, 10, 24))
    leagues, start, end, books, include_started, max_objects = fake.calls[0]
    assert leagues == {Sport.NBA: "NBA", Sport.NFL: "NFL"}
    assert start.astimezone(UTC) == datetime(2026, 10, 24, 4, tzinfo=UTC)
    assert books[:6] == ("pinnacle", "draftkings", "fanduel", "prizepicks", "underdog", "sleeper")
    assert "betmgm" in books and len(books) == 13  # + free-tier consensus books
    assert include_started is False and max_objects == 2250  # 90% of 2,500
    assert quota_status(conn, settings).used == 2
    assert s.report.total == len(nba_result.props)
    run = latest_runs(conn).iloc[0]
    assert json.loads(run["skipped"]) == {"bet_type:ml3way": 1}
    assert "missing" in json.loads(run["notices"])[0]


def _api_run(conn, objects, when):
    run = start_run(conn, "sportsgameodds", "nba", "api", when)
    finish_run(conn, run, finished_at=when, requests_made=1, objects_used=objects, n_events=0, n_props=0,
               n_flagged=0, notices=[], skipped={}, raw_paths=[])


def test_quota_guard_blocks_and_caps(settings, nba_result):
    conn = connect(settings.db_file)
    now = datetime(2026, 10, 24, 18, tzinfo=UTC)
    _api_run(conn, 2000, now - timedelta(days=40))  # outside the rolling 30-day window
    _api_run(conn, 2200, now - timedelta(days=3))
    q = quota_status(conn, settings, now)
    assert (q.used, q.budget, q.remaining) == (2200, 2250, 50)

    fake = FakeProvider(nba_result)
    fetch_and_store(conn, settings, fake, [Sport.NBA], date(2026, 10, 24), now=now)
    assert fake.calls[-1][-1] == 50  # page cap passed down to the provider

    _api_run(conn, 100, now - timedelta(days=1))
    with pytest.raises(QuotaExceeded, match="--force"):
        fetch_and_store(conn, settings, fake, [Sport.NBA], date(2026, 10, 24), now=now)
    # --force may spend up to the real plan limit, never past it
    fetch_and_store(conn, settings, fake, [Sport.NBA], date(2026, 10, 24), now=now, force=True)
    assert fake.calls[-1][-1] == 2500 - 2302
