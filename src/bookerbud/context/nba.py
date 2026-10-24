"""NBA rest / back-to-back flags.

NBA.com's terms forbid using its data "in connection with any gambling
activity (including legal gambling activity)", so we don't touch NBA.com feeds.
Instead rest days come from the events table we already fill from
SportsGameOdds. If yesterday isn't in it, one cheap SGO schedule call
backfills it (costs about 1 quota object per game played yesterday, at most
once per day).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

from bookerbud.context.models import GameContext
from bookerbud.models import EventInfo


def last_game_before(conn: sqlite3.Connection, team: str, before: datetime) -> datetime | None:
    row = conn.execute(
        """SELECT MAX(start_time) FROM events WHERE sport = 'nba' AND (home_team = ? OR away_team = ?)
           AND start_time < ?""",
        (team, team, before.astimezone(UTC).isoformat()),  # events.start_time is stored as UTC ISO
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def rest_days(conn: sqlite3.Connection, team: str, game_day: date, day_start: datetime, tz) -> int | None:
    """0 = back-to-back. None = no earlier game on record."""
    last = last_game_before(conn, team, day_start)
    if last is None:
        return None
    return (game_day - last.astimezone(tz).date()).days - 1


def build(conn: sqlite3.Connection, events: list[EventInfo], day: date, day_start: datetime, tz,
          yesterday_known: bool) -> list[GameContext]:
    out = []
    for ev in events:
        rd = {side: rest_days(conn, getattr(ev, f"{side}_team"), day, day_start, tz) for side in ("home", "away")}
        if not yesterday_known:
            # Without yesterday's slate we can't tell a back-to-back from rest; say so instead of guessing.
            rd = {s: (d if d is not None and d >= 1 else None) for s, d in rd.items()}
        ctx = GameContext(event_id=ev.event_id, sport="nba", home_team=ev.home_team, away_team=ev.away_team,
                          rest_days=rd, sources=["sportsgameodds-schedule"])
        for side, d in rd.items():
            team = getattr(ev, f"{side}_team")
            if d == 0:
                ctx.notes.append(f"{team} on a back-to-back")
            elif d is None:
                ctx.notes.append(f"{team} rest unknown")
        out.append(ctx)
    return out


def yesterday_window(day: date, tz) -> tuple[datetime, datetime]:
    start = datetime.combine(day - timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start, start + timedelta(days=1)
