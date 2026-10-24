from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from bookerbud.context import nba
from bookerbud.models import EventInfo, Sport
from bookerbud.pipeline import load_context, quota_status, refresh_context
from bookerbud.providers.base import FetchResult
from bookerbud.storage import connect, mark_schedule_covered, schedule_covered, upsert_events

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 24)


def ev(eid, home, away, start):
    return EventInfo(provider="sportsgameodds", event_id=eid, sport=Sport.NBA, league="NBA", start_time=start,
                     home_team=home, away_team=away)


TODAY = ev("T1", "Denver Nuggets", "Los Angeles Lakers", datetime(2026, 10, 25, 1, tzinfo=UTC))  # 9pm ET Oct 24
YDAY = ev("Y1", "Los Angeles Lakers", "Phoenix Suns", datetime(2026, 10, 24, 2, tzinfo=UTC))  # 10pm ET Oct 23
TWO_AGO = ev("Z1", "Denver Nuggets", "Utah Jazz", datetime(2026, 10, 23, 1, tzinfo=UTC))  # 9pm ET Oct 22


def test_rest_days_from_events(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    upsert_events(conn, [TODAY, YDAY, TWO_AGO], datetime.now(UTC))
    start = datetime.combine(DAY, datetime.min.time(), tzinfo=NY)
    assert nba.rest_days(conn, "Los Angeles Lakers", DAY, start, NY) == 0  # played last night
    assert nba.rest_days(conn, "Denver Nuggets", DAY, start, NY) == 1  # Oct 22 -> one day off
    assert nba.rest_days(conn, "Boston Celtics", DAY, start, NY) is None
    (g,) = nba.build(conn, [TODAY], DAY, start, NY, yesterday_known=True)
    assert g.back_to_back == {"home": False, "away": True}
    assert "Los Angeles Lakers on a back-to-back" in g.notes


def test_unknown_yesterday_never_claims_rest(tmp_path):
    conn = connect(tmp_path / "db.sqlite")
    upsert_events(conn, [TODAY, TWO_AGO], datetime.now(UTC))
    start = datetime.combine(DAY, datetime.min.time(), tzinfo=NY)
    (g,) = nba.build(conn, [TODAY], DAY, start, NY, yesterday_known=False)
    assert g.rest_days == {"home": 1, "away": None}  # Denver's game 2 days ago is still known
    assert "Los Angeles Lakers rest unknown" in g.notes


class ScheduleProvider:
    name = "sportsgameodds"

    def __init__(self):
        self.calls = []

    def fetch_schedule(self, leagues, starts_after, starts_before, *, max_objects=None):
        self.calls.append((starts_after, starts_before, max_objects))
        self.leagues = leagues
        return FetchResult(events=[YDAY], objects_used=1, requests_made=1)


def test_refresh_context_backfills_once_and_counts_quota(settings):
    conn = connect(settings.db_file)
    upsert_events(conn, [TODAY], datetime.now(UTC))
    p = ScheduleProvider()
    now = datetime(2026, 10, 24, 16, tzinfo=UTC)
    s = refresh_context(conn, settings, p, [Sport.NBA], DAY, now=now)
    assert len(p.calls) == 1 and s.objects_used == 1 and s.games == 1
    after, before, cap = p.calls[0]
    assert after.astimezone(UTC) == datetime(2026, 10, 23, 4, tzinfo=UTC) and cap == 60
    assert set(p.leagues) == {Sport.NBA, Sport.NFL}  # one combined call (config/tracking.yaml history)
    assert schedule_covered(conn, "nba", "2026-10-23") and schedule_covered(conn, "nfl", "2026-10-23")
    assert quota_status(conn, settings, now).used == 1
    games, _ = load_context(conn, [Sport.NBA])
    assert games["T1"].back_to_back["away"] is True

    refresh_context(conn, settings, p, [Sport.NBA], DAY, now=now + timedelta(hours=1))
    assert len(p.calls) == 1  # covered -> no second spend


def test_refresh_context_respects_no_backfill(settings):
    conn = connect(settings.db_file)
    upsert_events(conn, [TODAY], datetime.now(UTC))
    p = ScheduleProvider()
    refresh_context(conn, settings, p, [Sport.NBA], DAY, backfill_nba=False)
    assert p.calls == []
    mark_schedule_covered(conn, "nba", "2026-10-23")
    assert schedule_covered(conn, "nba", "2026-10-23")
