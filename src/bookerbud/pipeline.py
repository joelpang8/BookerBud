"""Fetch -> log raw -> normalise -> store. Shared by the CLI and tests."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from bookerbud.config import Settings
from bookerbud.models import Sport
from bookerbud.normalize import NormalizeReport, normalize_props
from bookerbud.providers.base import FetchResult, OddsProvider
from bookerbud.raw_log import read_raw
from bookerbud.storage import finish_run, insert_props, objects_used_since, start_run, upsert_events


if TYPE_CHECKING:
    from bookerbud.context.mlb import MLBContext


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


# --------------------------------------------------------------------- Phase 3


@dataclass
class ContextSummary:
    games: int = 0
    players: int = 0
    objects_used: int = 0
    warnings: list[str] | None = None


def refresh_context(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    sports: list[Sport],
    day: date,
    *,
    mlb: MLBContext | None = None,
    backfill_nba: bool = True,
    now: datetime | None = None,
) -> ContextSummary:
    """Pull injury/lineup/weather/rest context for `day`'s stored events.
    Free sources only; the optional NBA backfill is quota-guarded."""
    from bookerbud.context import nba as nba_ctx
    from bookerbud.storage import events_between, insert_context, mark_schedule_covered, schedule_covered, upsert_events

    now = now or datetime.now(UTC)
    start, end = day_window(day, settings.tz)
    out = ContextSummary(warnings=[])

    if Sport.MLB in sports and mlb is not None:
        events = events_between(conn, "mlb", start, end)
        if events:
            games, players, warns = mlb.build(events, day)
            insert_context(conn, now, "mlb", "game", [(g.event_id, g.model_dump_json()) for g in games])
            insert_context(conn, now, "mlb", "player", [(p.player_key, p.model_dump_json()) for p in players])
            out.games += len(games)
            out.players += len(players)
            out.warnings += warns

    # Yesterday's results (box scores + schedule) for form charts and NBA rest:
    # one combined call for every sport not yet covered.
    yday = (day - timedelta(days=1)).isoformat()
    want = {Sport(x) for x in history_sports(settings)} | ({Sport.NBA} if Sport.NBA in sports else set())
    missing = [sp for sp in sorted(want) if not schedule_covered(conn, sp.value, yday)]
    if missing and backfill_nba:
        ys, ye = nba_ctx.yesterday_window(day, settings.tz)
        used, ok = backfill_results(conn, settings, provider, missing, ys, ye, now=now, cap=60)
        out.objects_used += used
        if ok:
            for sp in missing:
                mark_schedule_covered(conn, sp.value, yday)
        else:
            out.warnings.append("Quota budget too low to store yesterday's results (form charts, NBA rest)")

    if Sport.NBA in sports:
        events = events_between(conn, "nba", start, end)
        if events:
            known = schedule_covered(conn, "nba", yday)
            games = nba_ctx.build(conn, events, day, start, settings.tz, yesterday_known=known)
            insert_context(conn, now, "nba", "game", [(g.event_id, g.model_dump_json()) for g in games])
            out.games += len(games)
    return out


def history_sports(settings: Settings) -> list[str]:
    from bookerbud.tracking.settle import load_tracking_config

    return [x for x in load_tracking_config().history.daily_sports if x in settings.sports]


def backfill_results(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    sports: list[Sport],
    start: datetime,
    end: datetime,
    *,
    now: datetime,
    cap: int | None = None,
) -> tuple[int, bool]:
    """Fetch finished games in [start, end) and store schedule + box scores.
    Returns (objects used, complete?). Quota-guarded; one call for all sports."""
    from bookerbud.storage import upsert_game_logs

    quota = quota_status(conn, settings, now)
    limit = quota.remaining if cap is None else min(quota.remaining, cap)
    if limit <= 0:
        return 0, False
    run_id = start_run(conn, provider.name, "history", "api", now)
    res = provider.fetch_schedule({sp: settings.sports.get(sp.value, sp.value.upper()) for sp in sports},
                                  start, end, max_objects=limit)
    upsert_events(conn, res.events, now)
    upsert_game_logs(conn, res.game_logs)
    finish_run(conn, run_id, finished_at=datetime.now(UTC), requests_made=res.requests_made,
               objects_used=res.objects_used, n_events=len(res.events), n_props=0, n_flagged=0,
               notices=[f"results {start.date()}..{end.date()}"], skipped={}, raw_paths=res.raw_paths)
    return res.objects_used, not res.truncated


def load_context(conn: sqlite3.Connection, sports: list[Sport]):
    """Latest stored context: (games by event_id, players by player_key)."""
    from bookerbud.context.models import GameContext, PlayerContext
    from bookerbud.storage import latest_context

    games, players = {}, {}
    for sp in sports:
        for k, v in latest_context(conn, sp.value, "game").items():
            games[k] = GameContext.model_validate_json(v)
        for k, v in latest_context(conn, sp.value, "player").items():
            players[k] = PlayerContext.model_validate_json(v)
    return games, players


# --------------------------------------------------------------------- Phase 4


@dataclass
class Analysis:
    result: object  # EdgeResult
    cfg: object  # EdgeConfig
    games: dict
    players: dict
    props: list
    now: datetime


def analyze(
    conn: sqlite3.Connection,
    settings: Settings,
    sports: list[Sport],
    *,
    now: datetime,
    use_context: bool = True,
    all_books: bool = False,
    min_edge: float | None = None,
) -> Analysis:
    """Latest snapshot -> edges, with context rules applied. Shared by `edges`,
    `slips` and `report`."""
    from bookerbud.context.apply import apply_context, load_context_rules
    from bookerbud.edge.config import load_availability, load_edge_config, load_payouts
    from bookerbud.edge.engine import find_edges
    from bookerbud.storage import load_latest_props

    cfg = load_edge_config()
    if min_edge is not None:
        cfg.filters.min_edge_pct = min_edge
    props = load_latest_props(conn, [s.value for s in sports])
    games, players = load_context(conn, sports) if use_context else ({}, {})
    res = find_edges(
        props, cfg, now=now, sharp_books=settings.bookmakers.sharp, states=settings.states,
        availability=load_availability(), payouts=load_payouts(), all_books=all_books,
    )
    if use_context:
        # A pick'em leg can sit in both lists as the same object: apply once.
        unique = list({id(e): e for e in res.edges + res.pickem_legs}.values())
        kept, dropped = apply_context(unique, games, players, load_context_rules())
        kept_ids = {id(e) for e in kept}
        res.edges = [e for e in res.edges if id(e) in kept_ids]
        res.pickem_legs = [e for e in res.pickem_legs if id(e) in kept_ids]
        for reason, n in dropped.items():
            res.skipped[f"context:{reason}"] += n
    return Analysis(res, cfg, games, players, props, now)
