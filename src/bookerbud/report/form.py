"""Recent form for a play: the stat in each of the last N games vs. today's line.

Descriptive context only: edges come from market prices, not from these."""

from __future__ import annotations

import sqlite3
from statistics import fmean

from bookerbud.edge.engine import Edge
from bookerbud.normalize.names import team_key
from bookerbud.normalize.stats import stat_value_from
from bookerbud.storage import recent_games


def _short(name: str | None) -> str:
    return (name or "").split()[-1] if name else ""


def form_for(conn: sqlite3.Connection, e: Edge, events: dict, n: int = 10) -> dict | None:
    if e.period != "game":
        return None
    ev = events.get(e.event_id)
    is_over = e.side in ("over", "home", "away", "yes")  # team bets are judged from the picked team's side
    if e.market_kind == "player_prop":
        if not e.player_key:
            return None
        keys = [e.player_key]

        def value(g):
            return stat_value_from(g["stats"], e.stat_label_raw or e.stat_type)

        threshold = e.line if e.bet_type == "ou" else 0.5
        what = "stat"
    elif e.stat_type not in ("points", ""):
        if e.entity == "all" or not e.team:
            return None  # needs both teams' value of this stat per game; not stored
        keys = [team_key(e.sport, e.team)]

        def value(g):
            return stat_value_from(g["stats"], e.stat_label_raw or e.stat_type)

        threshold = e.line
        what = e.stat_type.replace("_", " ")
    elif e.bet_type in ("ml", "sp"):
        keys = [team_key(e.sport, e.team)] if e.team else []

        def value(g):
            s = g["stats"]
            return s["points"] - s["points_allowed"] if "points" in s and "points_allowed" in s else None

        threshold = 0.0 if e.bet_type == "ml" else -(e.line or 0.0)
        what = "margin"
    elif e.entity == "all":
        keys = [team_key(e.sport, t) for t in ((ev.home_team, ev.away_team) if ev else ())]

        def value(g):
            s = g["stats"]
            return s["points"] + s["points_allowed"] if "points" in s and "points_allowed" in s else None

        threshold = e.line
        what = "game total"
    else:
        keys = [team_key(e.sport, e.team)] if e.team else []

        def value(g):
            return g["stats"].get("points")

        threshold = e.line
        what = "team points"
    if not keys or threshold is None:
        return None

    games = []
    for g in reversed(recent_games(conn, keys, e.start_time, n)):  # oldest -> newest
        v = value(g)
        if v is None:
            continue
        if v == threshold:
            res = "push"
        else:
            res = "hit" if (v > threshold) == is_over else "miss"
        games.append({
            "date": f"{g['start_time'].month}/{g['start_time'].day}",
            "opp": _short(g.get("opponent")) if what != "game total" else f"{_short(g.get('team'))}–{_short(g.get('opponent'))}",
            "value": v, "result": res,
        })
    if not games:
        return None
    vals = [g["value"] for g in games]
    return {
        "what": what, "threshold": threshold, "side": e.side, "games": games, "n": len(games),
        "hits": sum(g["result"] == "hit" for g in games), "avg": fmean(vals),
    }
