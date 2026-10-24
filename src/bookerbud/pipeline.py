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
from bookerbud.storage import finish_run, insert_props, objects_used_since, start_run, upsert_events


class QuotaExceeded(RuntimeError):
    pass


@dataclass
class FetchSummary:
    run_id: int
    sports: list[Sport]
    result: FetchResult
    report: NormalizeReport


@dataclass
class QuotaStatus:
    used: int
    budget: int
    limit: int
    window_days: int

    @property
    def remaining(self) -> int:
        return max(self.budget - self.used, 0)


def day_window(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[local midnight, next local midnight) for `day`, as aware datetimes."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def quota_status(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> QuotaStatus:
    now = now or datetime.now(UTC)
    q = settings.quota
    used = objects_used_since(conn, now - timedelta(days=q.window_days), settings.provider)
    return QuotaStatus(used=used, budget=q.budget, limit=q.monthly_objects, window_days=q.window_days)


def _leagues(settings: Settings, sports: list[Sport]) -> dict[Sport, str]:
    return {s: settings.sports.get(s.value, s.value.upper()) for s in sports}


def _store(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    sports: list[Sport],
    result: FetchResult,
    *,
    source: str,
    started_at: datetime,
    fetched_at: datetime,
) -> FetchSummary:
    run_id = start_run(conn, provider.name, ",".join(s.value for s in sports), source, started_at)
    props, report = normalize_props(
        result.props, result.rosters, fetched_at=fetched_at, pickem_books=set(settings.bookmakers.pickem)
    )
    upsert_events(conn, result.events, fetched_at)
    insert_props(conn, run_id, props)
    finish_run(
        conn, run_id,
        finished_at=datetime.now(UTC),
        requests_made=result.requests_made,
        objects_used=result.objects_used if source == "api" else 0,
        n_events=len(result.events),
        n_props=len(props),
        n_flagged=sum(1 for p in props if p.flags),
        notices=result.notices,
        skipped=dict(result.skipped_markets),
        raw_paths=result.raw_paths,
    )
    return FetchSummary(run_id, sports, result, report)


def fetch_and_store(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    sports: list[Sport],
    day: date,
    *,
    include_started: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> FetchSummary:
    """One provider call for all `sports`, capped so the rolling-window spend
    stays under the configured budget (or the plan's hard limit with force)."""
    started_at = now or datetime.now(UTC)
    quota = quota_status(conn, settings, started_at)
    cap = (quota.limit - quota.used) if force else quota.remaining
    if cap <= 0:
        raise QuotaExceeded(
            f"Quota guard: {quota.used} objects used in the last {quota.window_days} days "
            f"(budget {quota.budget}, plan limit {quota.limit}). "
            + ("Plan limit reached." if force else "Use --force to spend the remaining headroom.")
        )
    start, end = day_window(day, settings.tz)
    result = provider.fetch_slate(
        _leagues(settings, sports), start, end, settings.bookmakers.all,
        include_started=include_started, max_objects=cap,
    )
    return _store(conn, settings, provider, sports, result, source="api", started_at=started_at, fetched_at=started_at)


def replay_and_store(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    sports: list[Sport],
    raw_files: list[Path],
) -> FetchSummary:
    """Re-run normalisation/storage from logged raw responses (no API calls, no quota)."""
    started_at = datetime.now(UTC)
    result = FetchResult()
    fetched_at = started_at
    for f in raw_files:
        rec = read_raw(f)
        fetched_at = datetime.fromisoformat(rec["fetched_at"]) if rec.get("fetched_at") else started_at
        part = provider.parse_payload(rec["body"], _leagues(settings, sports), settings.bookmakers.all)
        part.raw_paths.append(Path(f))
        result.extend(part)
    return _store(conn, settings, provider, sports, result, source="replay", started_at=started_at, fetched_at=fetched_at)
