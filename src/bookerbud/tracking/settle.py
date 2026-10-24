"""Closing lines, CLV and grading for every recommendation and logged bet.

After the games, one provider call per batch of events (SportsGameOdds:
`eventIDs` + `includeOpenCloseOdds`, about 1 quota object per event) returns:
  * each book's price/line at event start (`closeOdds`, `closeOverUnder`, `closeSpread`)
  * the stat result (`score` on the market, else `results[period][entity][stat]`)

Closing fair probability is computed exactly like the pre-game fair value
(Pinnacle if present, else a consensus of the other books, target book
excluded), moved to *your* line, so CLV is the EV your bet would have had at
the closing fair price:
    CLV% = P_close(win) x (decimal - 1) - P_close(lose)     (sportsbook)
    CLV% = P_close(win) / per-pick break-even - 1           (pick'em)

Closing odds are optional (config/tracking.yaml `closing_odds`, off by
default). Without them, or when a market has none, the latest snapshot we
stored after the play was priced and before the start is used instead
(close_source = "last_snapshot"); with no such snapshot, CLV stays blank.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pathlib import Path

import yaml
from pydantic import BaseModel

from bookerbud.config import CONFIG_DIR, Settings
from bookerbud.edge.config import EdgeConfig, load_edge_config, load_payouts
from bookerbud.edge.engine import book_pairs, fair_for, pickem_breakeven, to_threshold
from bookerbud.edge.odds import american_to_decimal
from bookerbud.models import MarketKind, PayoutType, Prop, Sport
from bookerbud.pipeline import quota_status
from bookerbud.providers.base import EventSettlement, OddsProvider
from bookerbud.storage import finish_run, start_run
from bookerbud.storage.db import row_to_prop
from bookerbud.storage.tracking import pending, save_settlement

class TrackingCfg(BaseModel):
    closing_odds: bool = False  # ask the provider for closing lines (same quota cost; results are fetched anyway)
    settle_after_hours: float = 4  # long enough for any NBA/MLB/NFL game to finish
    max_attempts: int = 6  # then the row is marked "ungradable"


def load_tracking_config(path: Path | None = None) -> TrackingCfg:
    p = path or CONFIG_DIR / "tracking.yaml"
    return TrackingCfg.model_validate(yaml.safe_load(p.read_text()) or {} if p.exists() else {})


@dataclass
class SettleSummary:
    pending: int = 0
    events: int = 0
    objects_used: int = 0
    settled: Counter = field(default_factory=Counter)  # result -> rows
    clv_rows: int = 0
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ grading

def _stat_from_results(results: dict, period: str, entity: str, stat_id: str) -> float | None:
    per = results.get(period) or {}
    entities = ["home", "away"] if entity == "all" else [entity]
    total = 0.0
    for ent in entities:
        vals = per.get(ent) or {}
        for part in stat_id.split("+"):
            if part not in vals or vals[part] is None:
                return None
            total += float(vals[part])
    return total


def stat_value(ev: EventSettlement, odd_id: str) -> float | None:
    o = ev.odds.get(odd_id)
    if o is None:
        return None
    if o.score is not None:
        return o.score
    return _stat_from_results(ev.results, o.period, o.stat_entity, o.stat_id)


def grade(row: sqlite3.Row, ev: EventSettlement) -> tuple[str | None, float | None]:
    """('win'|'loss'|'push'|'void', stat value) or (None, None) if not final yet."""
    o = ev.odds.get(row["provider_market_id"])
    if ev.cancelled or (o is not None and o.cancelled):
        return "void", None
    if not (ev.finalized or ev.ended):
        return None, None
    v = stat_value(ev, row["provider_market_id"])
    if v is None:
        return None, None
    bt, side, line = row["bet_type"], row["side"], row["line"]
    if bt == "ou":
        if v == line:
            return "push", v
        return ("win" if (v > line) == (side == "over") else "loss"), v
    if bt == "yn":
        return ("win" if (v > 0) == (side == "yes") else "loss"), v
    if bt in ("ml", "sp"):
        opp = stat_value(ev, o.opposing_odd_id) if o and o.opposing_odd_id else None
        if opp is None:
            return None, None
        handicap = (line or 0.0) if bt == "sp" else 0.0
        margin = v - opp + handicap
        if margin == 0:
            return "push", v
        return ("win" if margin > 0 else "loss"), v
    return None, None


def profit(result: str | None, stake: float, price: int | None, payout_type: str) -> float | None:
    if result is None:
        return None
    if result in ("push", "void"):
        return 0.0
    if payout_type == "pickem" or price is None:
        return None  # single pick'em legs aren't bets; slips would need their own payout
    return stake * (american_to_decimal(price) - 1) if result == "win" else -stake


# ------------------------------------------------------------------ closing value

def _model_key(row: sqlite3.Row) -> str:
    if row["market_kind"] == MarketKind.PLAYER_PROP.value:
        return row["stat_type"]
    if row["bet_type"] == "sp":
        return "game_spread"
    return "game_total" if row["entity"] == "all" else "team_total"


def _close_prop(row: sqlite3.Row, book: str, side: str, price: int | None, line: float | None, pickem: set[str]) -> Prop:
    """Minimal Prop for the fair-value code (only pricing fields are read)."""
    return Prop.model_construct(
        sport=Sport(row["sport"]), bookmaker=book, side=side, bet_type=row["bet_type"], line=line,
        price_american=price, available=True, is_main_line=True, is_alt_line=False,
        payout_type=PayoutType.PICKEM if book in pickem else PayoutType.AMERICAN_ODDS,
    )


def _close_from_provider(row, ev: EventSettlement, pickem: set[str]) -> list[Prop]:
    o = ev.odds.get(row["provider_market_id"])
    if o is None:
        return []
    props = [_close_prop(row, b, o.side, p, l, pickem) for b, (p, l) in o.close.items()]
    opp = ev.odds.get(o.opposing_odd_id) if o.opposing_odd_id else None
    if opp is not None:
        props += [_close_prop(row, b, opp.side, p, l, pickem) for b, (p, l) in opp.close.items()]
    return props


def _priced_at(row) -> str:
    """When the play's price was taken: first sighting for a recommendation, logging time for a bet."""
    keys = row.keys()
    return row["first_seen_at"] if "first_seen_at" in keys else row["created_at"]


def _close_from_snapshots(conn: sqlite3.Connection, row) -> tuple[list[Prop], datetime | None]:
    """Latest stored snapshot of this market (both sides) taken before the start
    AND after the play was priced. The snapshot the play came from can't measure
    line movement (its CLV would just repeat the predicted edge), so it's excluded."""
    rows = conn.execute(
        """SELECT * FROM prop_snapshots WHERE event_id = ? AND market_kind = ? AND entity IS ? AND player_key IS ?
           AND stat_type = ? AND period = ? AND bet_type = ? AND available = 1
           AND fetched_at = (SELECT MAX(fetched_at) FROM prop_snapshots
                             WHERE event_id = ? AND fetched_at < ? AND fetched_at > ?)""",
        (row["event_id"], row["market_kind"], row["entity"], row["player_key"], row["stat_type"], row["period"],
         row["bet_type"], row["event_id"], row["start_time"], _priced_at(row)),
    ).fetchall()
    props = [row_to_prop(r) for r in rows]
    return props, (props[0].fetched_at if props else None)


def _better(row, close_line: float | None, close_price: int | None) -> int | None:
    """1 if the bet beat the same book's closing number (better line, or same line and better price)."""
    if close_price is None or row["price_american"] is None:
        return None
    line, side = row["line"], row["side"]
    if line is not None and close_line is not None and line != close_line:
        # over: lower is better; under: higher; spreads: more points (higher) is better for either side
        if row["bet_type"] == "ou":
            return int(line < close_line) if side == "over" else int(line > close_line)
        return int(line > close_line)
    return int(american_to_decimal(row["price_american"]) > american_to_decimal(close_price))


def closing_value(row, props: list[Prop], cfg: EdgeConfig, settings: Settings, payouts) -> dict | None:
    if not props:
        return None
    pairs = book_pairs(props, cfg.devig_method)
    model = cfg.model_for(row["sport"], _model_key(row), row["period"])
    is_over, thr = to_threshold(row["bet_type"], row["side"], row["line"])
    fair, _ = fair_for(row["bookmaker"], thr, pairs, settings.bookmakers.sharp, model, cfg)
    if fair is None:
        return None
    win = fair.outcome.over if is_over else fair.outcome.under
    push = fair.outcome.push
    lose = 1 - win - push
    if row["payout_type"] == "pickem":
        be = pickem_breakeven(payouts.get(row["bookmaker"]))
        clv = (win / (win + lose) / be[0] - 1) * 100 if be else None
    elif row["price_american"] is not None:
        clv = (win * (american_to_decimal(row["price_american"]) - 1) - lose) * 100
    else:
        clv = None
    same = [p for p in props if p.bookmaker == row["bookmaker"] and p.side == row["side"]]
    close_line = same[0].line if same else None
    close_price = same[0].price_american if same else None
    return {
        "close_fair_prob": win, "close_push_prob": push, "close_books": json.dumps(fair.books), "clv_pct": clv,
        "close_line": close_line, "close_price": close_price, "beat_close": _better(row, close_line, close_price),
    }


# ------------------------------------------------------------------ job

def settle(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: OddsProvider,
    *,
    now: datetime | None = None,
    after_hours: float | None = None,
    max_attempts: int | None = None,
    closing_odds: bool | None = None,
    cfg: EdgeConfig | None = None,
) -> SettleSummary:
    tc = load_tracking_config()
    after_hours = tc.settle_after_hours if after_hours is None else after_hours
    max_attempts = tc.max_attempts if max_attempts is None else max_attempts
    closing_odds = tc.closing_odds if closing_odds is None else closing_odds
    now = now or datetime.now(UTC)
    cfg = cfg or load_edge_config()
    payouts = load_payouts()
    pickem = set(settings.bookmakers.pickem)
    todo = pending(conn, now - timedelta(hours=after_hours), max_attempts)
    out = SettleSummary(pending=len(todo))
    if not todo:
        return out

    event_ids = sorted({r["event_id"] for _, r in todo})
    quota = quota_status(conn, settings, now)
    if quota.remaining < len(event_ids):
        out.warnings.append(f"Quota budget allows {quota.remaining} of {len(event_ids)} events; settling the oldest first.")
        oldest = sorted({(r["start_time"], r["event_id"]) for _, r in todo})
        keep: list[str] = []
        for _, eid in oldest:
            if eid not in keep:
                keep.append(eid)
        event_ids = keep[: max(quota.remaining, 0)]

    data: dict[str, EventSettlement] = {}
    if event_ids:
        run_id = start_run(conn, provider.name, "settle", "api", now)
        data, meta = provider.fetch_settlement(event_ids, settings.bookmakers.all, include_close=closing_odds)
        finish_run(conn, run_id, finished_at=datetime.now(UTC), requests_made=meta.requests_made,
                   objects_used=meta.objects_used, n_events=len(data), n_props=0, n_flagged=0,
                   notices=["settlement"], skipped={}, raw_paths=meta.raw_paths)
        out.events, out.objects_used = len(data), meta.objects_used

    for table, row in todo:
        if row["event_id"] not in event_ids:
            continue
        ev = data.get(row["event_id"])
        fields: dict = {}
        if ev is not None:
            result, value = grade(row, ev)
            if result is not None:
                fields.update(result=result, stat_value=value, settled_at=now.isoformat())
            close = closing_value(row, _close_from_provider(row, ev, pickem), cfg, settings, payouts) if closing_odds else None
            if close is not None:
                fields.update(close, close_source="provider_close", close_at=row["start_time"])
        if "clv_pct" not in fields and row["close_source"] is None:
            props, at = _close_from_snapshots(conn, row)
            close = closing_value(row, props, cfg, settings, payouts)
            if close is not None:
                fields.update(close, close_source="last_snapshot", close_at=at.isoformat() if at else None)
        if "result" not in fields and row["settle_attempts"] + 1 >= max_attempts:
            fields.update(result="ungradable", settled_at=now.isoformat())
        if table == "bets" and fields.get("result"):
            fields["profit"] = profit(fields["result"], row["stake"], row["price_american"], row["payout_type"])
        if fields.get("result"):
            out.settled[fields["result"]] += 1
        if fields.get("clv_pct") is not None:
            out.clv_rows += 1
        save_settlement(conn, table, row["id"], fields)
    return out
