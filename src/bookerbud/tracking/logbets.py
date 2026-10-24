"""Turn "I placed this" into a row in `bets`, from a recommendation or by
finding the market in the latest snapshot. Shared by `betedge log` and the
dashboard's Mark placed / Log a bet controls. Also edits and deletes noted bets."""

from __future__ import annotations

import sqlite3

from bookerbud.models import Sport
from bookerbud.normalize.names import normalize_name
from bookerbud.storage import load_latest_props
from bookerbud.storage.tracking import (
    SETTLE_FIELDS,
    delete_bet,
    get_bet,
    get_recommendation,
    insert_bet,
    update_bet,
)

REC_FIELDS = ("sport", "league", "event_id", "start_time", "market_kind", "entity", "player", "player_key", "team",
              "opponent", "stat_type", "period", "bet_type", "side", "bookmaker", "payout_type", "provider_market_id",
              "is_alt_line", "fair_prob", "ev_pct")


class BetError(ValueError):
    """A bet couldn't be matched or is missing something; message is user-facing."""


def bet_from_rec(conn: sqlite3.Connection, rec_id: int, *, line: float | None, price: int | None) -> dict:
    r = get_recommendation(conn, rec_id)
    if r is None:
        raise BetError(f"No recommendation #{rec_id}.")
    bet = {k: r[k] for k in REC_FIELDS}
    bet.update(rec_id=rec_id, line=r["line"] if line is None else line,
               price_american=r["price_american"] if price is None else price)
    return bet


def bet_from_market(
    conn: sqlite3.Connection, *, player: str | None, team: str | None, stat: str | None, side: str | None,
    bet_type: str | None, book: str | None, line: float | None, price: int | None,
) -> dict:
    if not (player or team) or not side or not book:
        raise BetError("Give a player (or team), a side (over/under/...) and a book.")
    props = load_latest_props(conn, [s.value for s in Sport])
    want_bt = bet_type or ("ou" if player else None)
    want_stat = stat or ("points" if player else None)

    def matches(p) -> bool:
        if p.bookmaker != book or p.side != side or p.is_alt_line:
            return False
        if player:
            if not (p.player and normalize_name(player) in normalize_name(p.player)):
                return False
        elif p.player or not (p.team and team.lower() in p.team.lower()):
            return False  # team bets are game lines for that team
        if want_stat and p.stat_type != want_stat:
            return False
        return not want_bt or p.bet_type == want_bt

    cands = [p for p in props if matches(p)]
    keys = {(p.event_id, p.provider_market_id, p.period) for p in cands}
    if not cands:
        raise BetError(f"No {book} market for {player or team} ({want_stat or 'any stat'}, {side}) in today's odds. "
                       "Check the name, stat and book, or refresh odds first.")
    games = [c for c in cands if c.period == "game"]
    if len({(p.event_id, p.provider_market_id) for p in games}) > 1 or (not games and len(keys) > 1):
        names = sorted({f"{p.player or p.team} {p.stat_type} {p.bet_type} {p.line}" for p in cands})
        raise BetError("More than one market matches: " + "; ".join(names[:6]) + ". Use a fuller name or pick the stat.")
    p = games[0] if games else cands[0]
    return {
        "sport": p.sport.value, "league": p.league, "event_id": p.event_id, "start_time": p.start_time.isoformat(),
        "market_kind": p.market_kind.value, "entity": p.entity, "player": p.player, "player_key": p.player_key,
        "team": p.team, "opponent": p.opponent, "stat_type": p.stat_type, "period": p.period, "bet_type": p.bet_type,
        "side": p.side, "bookmaker": p.bookmaker, "payout_type": p.payout_type.value,
        "provider_market_id": p.provider_market_id, "is_alt_line": 0, "line": p.line if line is None else line,
        "price_american": p.price_american if price is None else price, "rec_id": None,
    }


def place(conn: sqlite3.Connection, bet: dict, *, stake: float | None, note: str | None = None) -> tuple[int, dict]:
    """Save the bet. Stake is optional (0 = not recorded: counts for hit rate and
    CLV, not for ROI)."""
    if stake is not None and stake < 0:
        raise BetError("Stake can't be negative.")
    if bet["payout_type"] != "pickem" and bet.get("price_american") is None:
        raise BetError("This market has no price in today's odds; enter the price you got.")
    bet = {**bet, "stake": float(stake or 0.0), "notes": note}
    return insert_bet(conn, bet), bet


def describe(bet: dict) -> str:
    who = bet.get("player") or bet.get("team") or "Game total"
    line = "" if bet.get("line") is None else f" {bet['line']:g}"
    p = bet.get("price_american")
    price = "pick'em" if p is None else (f"+{p}" if p > 0 else str(p))
    stake = f" for ${bet['stake']:.2f}" if bet.get("stake") else " (no stake recorded)"
    return f"{who} {bet['side']}{line} {bet['stat_type']} @ {bet['bookmaker']} {price}{stake}"


SIDES = {"ou": ("over", "under"), "yn": ("yes", "no")}
CLOSE_FIELDS = tuple(f for f in SETTLE_FIELDS if f.startswith("close_") or f in ("clv_pct", "beat_close"))


def edit(
    conn: sqlite3.Connection, bet_id: int, *, stake: float | None = None, price: int | None = None,
    line: float | None = None, side: str | None = None, book: str | None = None, note: str | None = None,
) -> dict:
    """Change a noted bet; only the fields given change. Returns the updated bet.

    Changing the terms (price, line, side, book) clears its closing-line numbers.
    A graded over/under or yes/no bet is re-graded from the stored stat; any
    other graded bet goes back to pending so the next `settle` grades it again.
    """
    row = get_bet(conn, bet_id)
    if row is None:
        raise BetError(f"No bet #{bet_id}.")
    bet = dict(row)
    changes: dict = {}
    if stake is not None:
        if stake < 0:
            raise BetError("Stake can't be negative.")
        changes["stake"] = float(stake)
    if price is not None:
        if -100 < price < 100:
            raise BetError("Price must be American odds, like -110 or +150.")
        changes["price_american"] = int(price)
    if line is not None:
        changes["line"] = float(line)
    if side is not None:
        side = side.strip().lower()
        if side not in SIDES.get(bet["bet_type"], ()):
            raise BetError(f"Side can only change between {' / '.join(SIDES.get(bet['bet_type'], ())) or 'n/a'} "
                           "for this bet. Delete it and log it again instead.")
        changes["side"] = side
    if book is not None:
        changes["bookmaker"] = book.strip().lower()
    if note is not None:
        changes["notes"] = note
    changes = {k: v for k, v in changes.items() if v != bet[k]}
    if not changes:
        return bet
    terms = {"price_american", "line", "side", "bookmaker"} & set(changes)
    new = {**bet, **changes}
    if terms:
        changes.update(dict.fromkeys(CLOSE_FIELDS))
        if bet["result"] in ("win", "loss", "push"):
            regraded = _regrade(new)
            if regraded is None:
                changes.update(result=None, stat_value=None, settled_at=None, settle_attempts=0)
            else:
                changes["result"] = regraded
        elif bet["result"] == "ungradable":
            changes.update(result=None, settled_at=None, settle_attempts=0)
    new = {**bet, **changes}
    if new["result"] is not None and {"result", "stake", "price_american"} & set(changes):
        from bookerbud.tracking.settle import profit

        changes["profit"] = profit(new["result"], new["stake"] or 0.0, new["price_american"], new["payout_type"])
    elif new["result"] is None:
        changes["profit"] = None
    update_bet(conn, bet_id, changes)
    return {**bet, **changes}


def _regrade(bet: dict) -> str | None:
    v = bet["stat_value"]
    if v is None:
        return None
    if bet["bet_type"] == "ou":
        return "push" if v == bet["line"] else ("win" if (v > bet["line"]) == (bet["side"] == "over") else "loss")
    if bet["bet_type"] == "yn":
        return "win" if (v > 0) == (bet["side"] == "yes") else "loss"
    return None


def remove(conn: sqlite3.Connection, bet_id: int) -> dict:
    row = get_bet(conn, bet_id)
    if row is None or not delete_bet(conn, bet_id):
        raise BetError(f"No bet #{bet_id}.")
    return dict(row)
