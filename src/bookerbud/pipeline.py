"""Fetch -> log raw -> normalise -> store. Shared by the CLI and tests."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bookerbud.config import Settings
from bookerbud.models import Sport
from bookerbud.normalize import NormalizeReport, normalize_props
from bookerbud.providers.base import FetchResult, OddsProvider
from bookerbud.raw_log import read_raw
from bookerbud.storage import finish_run, insert_props, start_run, upsert_events


@dataclass
class FetchSummary:
    run_id: int
    sport: Sport
    result: FetchResult
    report: NormalizeReport


def day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[local midnight, next local midnight) for `day`, as aware datetimes."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _store(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    sport: Sport,
    result: FetchResult,
    *,
    source: str,
    started_at: datetime,
    fetched_at: datetime,
) -> FetchSummary:
    run_id = start_run(conn, provider.name, sport.value, source, started_at)
    props, report = normalize_props(
        result.props, result.rosters, fetched_at=fetched_at, pickem_books=set(settings.bookmakers.pickem)
    )
    upsert_events(conn, result.events, fetched_at)
    insert_props(conn, run_id, props)
    finish_run(
        conn, run_id,
        finished_at=datetime.now(UTC),
        requests_made=result.requests_made,
        n_events=len(result.events),
        n_props=len(props),
        n_flagged=sum(1 for p in props if p.flags),
        notices=result.notices,
        skipped=dict(result.skipped_markets),
        raw_paths=result.raw_paths,
    )
    return FetchSummary(run_id, sport, result, report)


def fetch_and_store(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    sport: Sport,
    day: date,
    *,
    include_started: bool = False,
) -> FetchSummary:
    started_at = datetime.now(UTC)
    start, end = day_window(day, settings.tz)
    result = provider.fetch_slate(
        sport, start, end, settings.bookmakers.all,
        include_started=include_started, league_id=settings.sports.get(sport.value),
    )
    return _store(conn, settings, provider, sport, result, source="api", started_at=started_at, fetched_at=started_at)


def replay_and_store(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    sport: Sport,
    raw_files: list[Path],
) -> FetchSummary:
    """Re-run normalisation/storage from logged raw responses (no API calls)."""
    started_at = datetime.now(UTC)
    result = FetchResult()
    fetched_at = started_at
    for f in raw_files:
        rec = read_raw(f)
        fetched_at = datetime.fromisoformat(rec["fetched_at"]) if rec.get("fetched_at") else started_at
        part = provider.parse_payload(rec["body"], sport, settings.bookmakers.all)
        part.raw_paths.append(Path(f))
        result.extend(part)
    return _store(conn, settings, provider, sport, result, source="replay", started_at=started_at, fetched_at=fetched_at)
