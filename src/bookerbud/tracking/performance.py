"""Performance: ROI, CLV, hit rate and calibration for recommendations and bets.

Definitions
  hit rate     wins / (wins + losses); pushes, voids and ungradable rows excluded
  CLV          mean of clv_pct (EV of the bet at the closing fair price, %)
  beat close   share of rows whose price/line beat the same book's closing number
  flat ROI     recommendations only: 1 unit on every sportsbook rec at its first
               price (win = decimal - 1, loss = -1, push = 0) / units risked
  ROI          logged bets: total profit / total staked
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from bookerbud.edge.odds import american_to_decimal

EDGE_BUCKETS = [(-1e9, 2, "< 2%"), (2, 4, "2–4%"), (4, 6, "4–6%"), (6, 10, "6–10%"), (10, 1e9, "10%+")]
PROB_STEP = 0.05


def _frames(conn: sqlite3.Connection, since: str | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    where, args = ("WHERE start_time >= ?", [since]) if since else ("", [])
    recs = pd.read_sql_query(f"SELECT * FROM recommendations {where}", conn, params=args)
    bets = pd.read_sql_query(f"SELECT * FROM bets {where}", conn, params=args)
    return recs, bets


def _flat_units(row) -> float | None:
    if row["payout_type"] == "pickem" or pd.isna(row["price_american"]) or row["result"] not in ("win", "loss", "push"):
        return None
    if row["result"] == "win":
        return american_to_decimal(int(row["price_american"])) - 1
    return -1.0 if row["result"] == "loss" else 0.0


def _metrics(df: pd.DataFrame, *, bets: bool) -> dict[str, Any]:
    wins = int((df["result"] == "win").sum()) if len(df) else 0
    losses = int((df["result"] == "loss").sum()) if len(df) else 0
    graded = df[df["result"].isin(["win", "loss", "push"])] if len(df) else df
    clv = df["clv_pct"].dropna() if len(df) else pd.Series(dtype=float)
    beat = df["beat_close"].dropna() if len(df) else pd.Series(dtype=float)
    out: dict[str, Any] = {
        "n": int(len(df)),
        "graded": int(len(graded)),
        "wins": wins,
        "losses": losses,
        "pushes": int((df["result"] == "push").sum()) if len(df) else 0,
        "hit_rate": wins / (wins + losses) if wins + losses else None,
        "clv": float(clv.mean()) if len(clv) else None,
        "clv_n": int(len(clv)),
        "beat_close": float(beat.mean()) if len(beat) else None,
    }
    if bets:
        settled = df[df["profit"].notna() & (df["stake"] > 0)] if len(df) else df  # no-stake bets: hit rate/CLV only
        staked = float(settled["stake"].sum()) if len(settled) else 0.0
        prof = float(settled["profit"].sum()) if len(settled) else 0.0
        out.update(staked=staked, profit=prof, roi=prof / staked if staked else None)
    else:
        units = df.apply(_flat_units, axis=1).dropna() if len(df) else pd.Series(dtype=float)
        out.update(flat_units=float(units.sum()) if len(units) else 0.0,
                   flat_roi=float(units.sum() / len(units)) if len(units) else None,
                   avg_edge=float(df["ev_pct"].mean()) if len(df) else None)
    return out


def _breakdown(df: pd.DataFrame, col: str, *, bets: bool) -> list[dict]:
    if df.empty:
        return []
    rows = [{"key": str(k), **_metrics(g, bets=bets)} for k, g in df.groupby(col)]
    return sorted(rows, key=lambda r: -r["n"])


def edge_buckets(recs: pd.DataFrame) -> list[dict]:
    out = []
    for lo, hi, label in EDGE_BUCKETS:
        g = recs[(recs["ev_pct"] >= lo) & (recs["ev_pct"] < hi)] if len(recs) else recs
        if g.empty:
            continue
        m = _metrics(g, bets=False)
        dec = g[g["result"].isin(["win", "loss"])]
        m.update(bucket=label, avg_edge=float(g["ev_pct"].mean()),
                 predicted=float(dec["fair_prob"].mean()) if len(dec) else None)
        out.append(m)
    return out


def calibration(recs: pd.DataFrame) -> list[dict]:
    """Predicted fair probability vs. actual hit rate, in 5-point buckets (win/loss rows only)."""
    dec = recs[recs["result"].isin(["win", "loss"])] if len(recs) else recs
    if dec.empty:
        return []
    b = (dec["fair_prob"] // PROB_STEP) * PROB_STEP
    out = []
    for lo, g in dec.groupby(b):
        out.append({"lo": float(lo), "hi": float(lo + PROB_STEP), "n": int(len(g)),
                    "predicted": float(g["fair_prob"].mean()), "actual": float((g["result"] == "win").mean())})
    return out


def summarize(conn: sqlite3.Connection, since: str | None = None) -> dict[str, Any]:
    recs, bets = _frames(conn, since)
    return {
        "since": since,
        "recs": _metrics(recs, bets=False),
        "bets": _metrics(bets, bets=True),
        "by": {
            dim: {"recs": _breakdown(recs, col, bets=False), "bets": _breakdown(bets, col, bets=True)}
            for dim, col in (("sport", "sport"), ("stat", "stat_type"), ("book", "bookmaker"))
        },
        "edge_buckets": edge_buckets(recs),
        "calibration": calibration(recs),
        "pending": int(recs["result"].isna().sum()) if len(recs) else 0,
    }
