"""Recommendations and bets (Phase 5)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from bookerbud.edge.engine import Edge
from bookerbud.storage.db import _ts

TABLES = ("recommendations", "bets")
SETTLE_FIELDS = (
    "close_source", "close_at", "close_line", "close_price", "close_fair_prob", "close_push_prob", "close_books",
    "clv_pct", "beat_close", "result", "stat_value", "settled_at",
)


def record_recommendations(conn: sqlite3.Connection, edges: list[Edge], rec_day: str, now: datetime) -> int:
    """Upsert every recommended play for `rec_day`. The first sighting's price and
    fair value are kept (what you could have bet then); later sightings only bump
    last_seen_at / times_seen."""
    rows = [
        (
            rec_day, _ts(now), _ts(now), e.sport, e.league, e.event_id, _ts(e.start_time), e.market_kind, e.entity,
            e.player, e.player_key, e.team, e.opponent, e.stat_type, e.period, e.bet_type, e.side, e.line,
            e.bookmaker, e.payout_type, e.price_american, e.implied_prob, e.fair_prob, e.push_prob, e.ev_pct,
            e.confidence, e.fair_source, json.dumps(e.fair_books), e.stake, e.breakeven_slip, e.provider_market_id,
            int(e.is_alt_line), json.dumps(e.context_notes),
        )
        for e in edges
    ]
    conn.executemany(
        f"""INSERT INTO recommendations (
            rec_day, first_seen_at, last_seen_at, sport, league, event_id, start_time, market_kind, entity, player,
            player_key, team, opponent, stat_type, period, bet_type, side, line, bookmaker, payout_type,
            price_american, implied_prob, fair_prob, push_prob, ev_pct, confidence, fair_source, fair_books,
            stake_suggested, breakeven_slip, provider_market_id, is_alt_line, notes
        ) VALUES ({",".join("?" * 33)})
        ON CONFLICT (rec_day, event_id, bookmaker, provider_market_id, line) DO UPDATE SET
            last_seen_at = excluded.last_seen_at, times_seen = times_seen + 1""",
        rows,
    )
    conn.commit()
    return len(rows)


def recommendations_for_day(conn: sqlite3.Connection, rec_day: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM recommendations WHERE rec_day = ? ORDER BY ev_pct DESC", (rec_day,)
    ).fetchall()


def get_recommendation(conn: sqlite3.Connection, rec_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM recommendations WHERE id = ?", (rec_id,)).fetchone()


BET_COLS = (
    "created_at", "rec_id", "sport", "league", "event_id", "start_time", "market_kind", "entity", "player",
    "player_key", "team", "opponent", "stat_type", "period", "bet_type", "side", "line", "bookmaker", "payout_type",
    "price_american", "stake", "provider_market_id", "is_alt_line", "fair_prob", "ev_pct", "notes",
)


def insert_bet(conn: sqlite3.Connection, bet: dict[str, Any]) -> int:
    bet = {"created_at": _ts(datetime.now(UTC)), **bet}
    cur = conn.execute(
        f"INSERT INTO bets ({', '.join(BET_COLS)}) VALUES ({','.join('?' * len(BET_COLS))})",
        [bet.get(c) for c in BET_COLS],
    )
    conn.commit()
    return int(cur.lastrowid)


def get_bet(conn: sqlite3.Connection, bet_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()


EDITABLE_BET_COLS = ("stake", "price_american", "line", "side", "bookmaker", "notes", "profit", "settle_attempts",
                     *SETTLE_FIELDS)


def update_bet(conn: sqlite3.Connection, bet_id: int, fields: dict[str, Any]) -> None:
    bad = set(fields) - set(EDITABLE_BET_COLS)
    if bad:
        raise ValueError(f"not editable: {sorted(bad)}")
    if fields:
        conn.execute(f"UPDATE bets SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), bet_id])
        conn.commit()


def delete_bet(conn: sqlite3.Connection, bet_id: int) -> bool:
    cur = conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,))
    conn.commit()
    return cur.rowcount > 0


def pending(conn: sqlite3.Connection, started_before: datetime, max_attempts: int) -> list[tuple[str, sqlite3.Row]]:
    """Unsettled rows whose game started before `started_before`."""
    out = []
    for table in TABLES:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE result IS NULL AND start_time < ? AND settle_attempts < ?",
            (_ts(started_before), max_attempts),
        ).fetchall()
        out += [(table, r) for r in rows]
    return out


def save_settlement(conn: sqlite3.Connection, table: str, row_id: int, fields: dict[str, Any]) -> None:
    if table not in TABLES:
        raise ValueError(table)
    fields = {k: v for k, v in fields.items() if k in (*SETTLE_FIELDS, "profit")}
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE {table} SET {sets + ', ' if sets else ''}settle_attempts = settle_attempts + 1 WHERE id = ?",
        [*fields.values(), row_id],
    )
    conn.commit()
