"""SQLite snapshot store. Every fetch appends rows (never updates), so line
movement is just the sequence of snapshots for one market/book/side."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bookerbud.models import EventInfo, Flag, MarketKind, PayoutType, Prop, Sport
from bookerbud.normalize.names import normalize_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,
    sport         TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    source        TEXT NOT NULL,           -- 'api' or 'replay'
    requests_made INTEGER DEFAULT 0,
    objects_used  INTEGER DEFAULT 0,           -- provider billing units
    n_events      INTEGER DEFAULT 0,
    n_props       INTEGER DEFAULT 0,
    n_flagged     INTEGER DEFAULT 0,
    notices       TEXT,                    -- JSON list
    skipped       TEXT,                    -- JSON {reason: count}
    raw_paths     TEXT                     -- JSON list
);

CREATE TABLE IF NOT EXISTS events (
    provider     TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    sport        TEXT NOT NULL,
    league       TEXT NOT NULL,
    start_time   TEXT NOT NULL,
    home_team    TEXT NOT NULL,
    away_team    TEXT NOT NULL,
    status       TEXT,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (provider, event_id)
);

CREATE TABLE IF NOT EXISTS prop_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES fetch_runs(run_id),
    fetched_at         TEXT NOT NULL,
    provider           TEXT NOT NULL,
    sport              TEXT NOT NULL,
    league             TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    start_time         TEXT NOT NULL,
    market_kind        TEXT NOT NULL,
    entity             TEXT NOT NULL,
    player             TEXT,
    player_key         TEXT,
    player_search      TEXT,               -- normalised name for LIKE search
    provider_player_id TEXT,
    player_status      TEXT,
    team               TEXT,
    opponent           TEXT,
    stat_type          TEXT NOT NULL,
    stat_label_raw     TEXT NOT NULL,
    period             TEXT NOT NULL,
    bet_type           TEXT NOT NULL,
    side               TEXT NOT NULL,
    line               REAL,
    price_american     INTEGER,
    payout_type        TEXT NOT NULL,
    bookmaker          TEXT NOT NULL,
    available          INTEGER NOT NULL,
    is_main_line       INTEGER,
    is_alt_line        INTEGER NOT NULL,
    book_updated_at    TEXT,
    provider_market_id TEXT NOT NULL,
    deeplink           TEXT,
    flags              TEXT NOT NULL DEFAULT ''   -- comma-separated Flag values
);

CREATE INDEX IF NOT EXISTS ix_snap_player ON prop_snapshots (player_search, stat_type, fetched_at);
CREATE INDEX IF NOT EXISTS ix_snap_market ON prop_snapshots (event_id, market_kind, entity, player_key, stat_type, period, bet_type, side, bookmaker);
CREATE INDEX IF NOT EXISTS ix_snap_run ON prop_snapshots (run_id);

-- Phase 3: injury/lineup/weather/rest context, append-only like odds.
CREATE TABLE IF NOT EXISTS context_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    sport      TEXT NOT NULL,
    kind       TEXT NOT NULL,      -- 'game' | 'player'
    key        TEXT NOT NULL,      -- event_id or player_key
    payload    TEXT NOT NULL       -- JSON (GameContext / PlayerContext)
);
CREATE INDEX IF NOT EXISTS ix_ctx ON context_snapshots (sport, kind, key, fetched_at);

-- Which (sport, local date) schedules we hold in `events` in full.
CREATE TABLE IF NOT EXISTS schedule_coverage (
    sport TEXT NOT NULL,
    day   TEXT NOT NULL,
    PRIMARY KEY (sport, day)
);
"""


def _ts(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fetch_runs)")}
    if "objects_used" not in cols:
        conn.execute("ALTER TABLE fetch_runs ADD COLUMN objects_used INTEGER DEFAULT 0")
        conn.commit()


def objects_used_since(conn: sqlite3.Connection, since: datetime, provider: str | None = None) -> int:
    """Billing units spent on live API calls since `since` (replays cost nothing)."""
    sql = "SELECT COALESCE(SUM(objects_used), 0) FROM fetch_runs WHERE source = 'api' AND started_at >= ?"
    args: list = [_ts(since)]
    if provider:
        sql += " AND provider = ?"
        args.append(provider)
    return int(conn.execute(sql, args).fetchone()[0])


def start_run(conn: sqlite3.Connection, provider: str, sport: str, source: str, started_at: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO fetch_runs (provider, sport, started_at, source) VALUES (?, ?, ?, ?)",
        (provider, sport, _ts(started_at), source),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: datetime,
    requests_made: int,
    objects_used: int = 0,
    n_events: int,
    n_props: int,
    n_flagged: int,
    notices: list[str],
    skipped: dict[str, int],
    raw_paths: list[Path],
) -> None:
    conn.execute(
        """UPDATE fetch_runs SET finished_at=?, requests_made=?, objects_used=?, n_events=?, n_props=?, n_flagged=?,
           notices=?, skipped=?, raw_paths=? WHERE run_id=?""",
        (
            _ts(finished_at), requests_made, objects_used, n_events, n_props, n_flagged,
            json.dumps(notices), json.dumps(skipped), json.dumps([str(p) for p in raw_paths]), run_id,
        ),
    )
    conn.commit()


def upsert_events(conn: sqlite3.Connection, events: Iterable[EventInfo], seen_at: datetime) -> None:
    conn.executemany(
        """INSERT INTO events (provider, event_id, sport, league, start_time, home_team, away_team, status, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(provider, event_id) DO UPDATE SET
             start_time=excluded.start_time, status=excluded.status, last_seen_at=excluded.last_seen_at""",
        [
            (e.provider, e.event_id, e.sport.value, e.league, _ts(e.start_time), e.home_team, e.away_team, e.status, _ts(seen_at))
            for e in events
        ],
    )
    conn.commit()


def insert_props(conn: sqlite3.Connection, run_id: int, props: Iterable[Prop]) -> int:
    rows = [
        (
            run_id, _ts(p.fetched_at), p.provider, p.sport.value, p.league, p.event_id, _ts(p.start_time),
            p.market_kind.value, p.entity, p.player, p.player_key,
            normalize_name(p.player) if p.player else None, p.provider_player_id, p.player_status,
            p.team, p.opponent, p.stat_type, p.stat_label_raw, p.period, p.bet_type, p.side, p.line,
            p.price_american, p.payout_type.value, p.bookmaker, int(p.available),
            None if p.is_main_line is None else int(p.is_main_line), int(p.is_alt_line),
            _ts(p.book_updated_at), p.provider_market_id, p.deeplink, ",".join(f.value for f in p.flags),
        )
        for p in props
    ]
    conn.executemany(
        f"""INSERT INTO prop_snapshots (
            run_id, fetched_at, provider, sport, league, event_id, start_time, market_kind, entity, player,
            player_key, player_search, provider_player_id, player_status, team, opponent, stat_type,
            stat_label_raw, period, bet_type, side, line, price_american, payout_type, bookmaker, available,
            is_main_line, is_alt_line, book_updated_at, provider_market_id, deeplink, flags
        ) VALUES ({",".join("?" * 32)})""",
        rows,
    )
    conn.commit()
    return len(rows)


def query_player_props(
    conn: sqlite3.Connection,
    player: str,
    *,
    sport: str | None = None,
    stat_type: str | None = None,
    bookmaker: str | None = None,
    include_alt: bool = False,
    history: bool = False,
) -> pd.DataFrame:
    """Snapshots for players whose normalised name contains `player`.

    history=False returns only each book/side/line's latest snapshot; True
    returns every snapshot so you can see movement over time."""
    where = ["player_search LIKE ?"]
    args: list = [f"%{normalize_name(player)}%"]
    if sport:
        where.append("sport = ?")
        args.append(sport)
    if stat_type:
        where.append("stat_type = ?")
        args.append(stat_type)
    if bookmaker:
        where.append("bookmaker = ?")
        args.append(bookmaker)
    if not include_alt:
        where.append("is_alt_line = 0")
    sql = f"SELECT * FROM prop_snapshots WHERE {' AND '.join(where)}"
    if not history:
        sql = f"""SELECT * FROM ({sql}) s WHERE fetched_at = (
                    SELECT MAX(fetched_at) FROM prop_snapshots t
                    WHERE t.event_id = s.event_id AND t.player_key IS s.player_key AND t.stat_type = s.stat_type
                      AND t.period = s.period AND t.bet_type = s.bet_type AND t.side = s.side
                      AND t.bookmaker = s.bookmaker AND t.is_alt_line = s.is_alt_line)"""
    sql += " ORDER BY start_time, player, stat_type, period, bookmaker, side, line, fetched_at"
    return pd.read_sql_query(sql, conn, params=args)


def latest_runs(conn: sqlite3.Connection, limit: int = 10) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM fetch_runs ORDER BY run_id DESC LIMIT ?", conn, params=[limit])


def flagged_summary(conn: sqlite3.Connection, run_id: int | None = None) -> pd.DataFrame:
    """Flagged rows grouped by reason. Default: the latest run of each sport."""
    if run_id is None:
        run_ids = [r[0] for r in conn.execute("SELECT MAX(run_id) FROM fetch_runs GROUP BY sport")]
    else:
        run_ids = [run_id]
    if not run_ids:
        return pd.DataFrame(columns=["run_id", "sport", "flags", "stat_label_raw", "player", "n_rows"])
    marks = ",".join("?" * len(run_ids))
    return pd.read_sql_query(
        f"""SELECT run_id, sport, flags, stat_label_raw, COALESCE(player, '') AS player, COUNT(*) AS n_rows
            FROM prop_snapshots WHERE run_id IN ({marks}) AND flags != ''
            GROUP BY run_id, sport, flags, stat_label_raw, player ORDER BY n_rows DESC""",
        conn, params=run_ids,
    )


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def row_to_prop(r: sqlite3.Row) -> Prop:
    return Prop(
        sport=Sport(r["sport"]), league=r["league"], provider=r["provider"], event_id=r["event_id"],
        start_time=_dt(r["start_time"]), market_kind=MarketKind(r["market_kind"]), entity=r["entity"],
        player=r["player"], player_key=r["player_key"], provider_player_id=r["provider_player_id"],
        player_status=r["player_status"], team=r["team"], opponent=r["opponent"], stat_type=r["stat_type"],
        stat_label_raw=r["stat_label_raw"], period=r["period"], bet_type=r["bet_type"], side=r["side"],
        line=r["line"], price_american=r["price_american"], payout_type=PayoutType(r["payout_type"]),
        bookmaker=r["bookmaker"], available=bool(r["available"]),
        is_main_line=None if r["is_main_line"] is None else bool(r["is_main_line"]),
        is_alt_line=bool(r["is_alt_line"]), book_updated_at=_dt(r["book_updated_at"]),
        fetched_at=_dt(r["fetched_at"]), provider_market_id=r["provider_market_id"], deeplink=r["deeplink"],
        flags=[Flag(f) for f in r["flags"].split(",") if f],
    )


def latest_run_ids(conn: sqlite3.Connection, sports: list[str]) -> dict[str, int]:
    """Most recent run that covered each sport (runs may cover several, e.g. 'nba,mlb,nfl')."""
    out: dict[str, int] = {}
    for sport in sports:
        row = conn.execute(
            "SELECT MAX(run_id) FROM fetch_runs WHERE ',' || sport || ',' LIKE ?", (f"%,{sport},%",)
        ).fetchone()
        if row and row[0] is not None:
            out[sport] = row[0]
    return out


def load_latest_props(conn: sqlite3.Connection, sports: list[str]) -> list[Prop]:
    """Every row from the latest snapshot of each sport."""
    props: list[Prop] = []
    for sport, run_id in latest_run_ids(conn, sports).items():
        rows = conn.execute("SELECT * FROM prop_snapshots WHERE run_id = ? AND sport = ?", (run_id, sport))
        props += [row_to_prop(r) for r in rows]
    return props


def insert_context(conn: sqlite3.Connection, fetched_at: datetime, sport: str, kind: str,
                   items: list[tuple[str, str]]) -> None:
    """items: (key, payload_json)."""
    conn.executemany(
        "INSERT INTO context_snapshots (fetched_at, sport, kind, key, payload) VALUES (?, ?, ?, ?, ?)",
        [(_ts(fetched_at), sport, kind, k, payload) for k, payload in items],
    )
    conn.commit()


def latest_context(conn: sqlite3.Connection, sport: str, kind: str) -> dict[str, str]:
    """key -> payload JSON from the most recent context refresh for that sport."""
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM context_snapshots WHERE sport = ? AND kind = ?", (sport, kind)
    ).fetchone()
    if not row or row[0] is None:
        return {}
    rows = conn.execute(
        "SELECT key, payload FROM context_snapshots WHERE sport = ? AND kind = ? AND fetched_at = ?",
        (sport, kind, row[0]),
    )
    return {r["key"]: r["payload"] for r in rows}


def mark_schedule_covered(conn: sqlite3.Connection, sport: str, day: str) -> None:
    conn.execute("INSERT OR IGNORE INTO schedule_coverage (sport, day) VALUES (?, ?)", (sport, day))
    conn.commit()


def schedule_covered(conn: sqlite3.Connection, sport: str, day: str) -> bool:
    return conn.execute("SELECT 1 FROM schedule_coverage WHERE sport = ? AND day = ?", (sport, day)).fetchone() is not None


def events_between(conn: sqlite3.Connection, sport: str, start: datetime, end: datetime) -> list[EventInfo]:
    rows = conn.execute(
        "SELECT * FROM events WHERE sport = ? AND start_time >= ? AND start_time < ? ORDER BY start_time",
        (sport, _ts(start), _ts(end)),
    )
    return [
        EventInfo(provider=r["provider"], event_id=r["event_id"], sport=Sport(r["sport"]), league=r["league"],
                  start_time=_dt(r["start_time"]), home_team=r["home_team"], away_team=r["away_team"], status=r["status"])
        for r in rows
    ]


def run_info(conn: sqlite3.Connection, run_id: int) -> dict:
    row = conn.execute("SELECT * FROM fetch_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else {}


def latest_context_time(conn: sqlite3.Connection, sport: str) -> datetime | None:
    row = conn.execute("SELECT MAX(fetched_at) FROM context_snapshots WHERE sport = ?", (sport,)).fetchone()
    return _dt(row[0]) if row and row[0] else None


def events_by_id(conn: sqlite3.Connection, ids: list[str]) -> dict[str, EventInfo]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT * FROM events WHERE event_id IN ({marks})", ids)
    return {
        r["event_id"]: EventInfo(provider=r["provider"], event_id=r["event_id"], sport=Sport(r["sport"]),
                                 league=r["league"], start_time=_dt(r["start_time"]), home_team=r["home_team"],
                                 away_team=r["away_team"], status=r["status"])
        for r in rows
    }
