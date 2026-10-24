"""Assemble the daily report as plain data (dicts/lists), shared by the
Markdown and HTML renderers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from bookerbud.config import CONFIG_DIR, Settings
from bookerbud.edge.config import load_payouts
from bookerbud.edge.engine import Edge
from bookerbud.edge.odds import american_to_decimal
from bookerbud.edge.slips import Slip, build_slips
from bookerbud.models import Sport
from bookerbud.pipeline import analyze, quota_status
from bookerbud.report import text
from bookerbud.report.form import form_for
from bookerbud.storage import events_by_id, latest_context_time, latest_run_ids, run_info
from bookerbud.storage.tracking import record_recommendations, recommendations_for_day
from bookerbud.tracking.logbets import SIDES


class ReportCfg(BaseModel):
    top_plays_per_sport: int = 12
    slip_legs: list[int] = [2, 3]
    top_slips: int = 6
    output_dir: Path | None = None  # None -> <settings.data_dir>/reports
    quota_warn_pct: float = 80
    top_picks: int = 3
    refresh_cooldown_minutes: float = 5
    confidence_weight: dict[str, float] = {"high": 1.0, "medium": 0.75, "low": 0.5}


def report_dir(settings: Settings, rcfg: ReportCfg) -> Path:
    if rcfg.output_dir is None:
        return settings.raw_dir.parent / "reports"
    return rcfg.output_dir if rcfg.output_dir.is_absolute() else settings._abs(rcfg.output_dir)


def load_report_config(path: Path | None = None) -> ReportCfg:
    p = path or CONFIG_DIR / "report.yaml"
    return ReportCfg.model_validate(yaml.safe_load(p.read_text()) or {} if p.exists() else {})


@dataclass
class Report:
    generated_at: datetime
    as_of: datetime
    tz: str
    sports: list[str]
    freshness: list[dict]
    quota: dict
    warnings: list[dict]  # {"level": "warn"|"info", "text": str}
    plays: dict[str, list[dict]]  # sport -> plays (best edge first)
    slips: list[dict]
    games: list[dict]
    skipped: dict[str, int]
    settings: dict
    totals: dict = field(default_factory=dict)
    performance: dict | None = None
    my_bets: list[dict] = field(default_factory=list)  # bets you noted for today's games
    top_picks: list[dict] = field(default_factory=list)  # across all sports
    top_picks_by_sport: dict[str, list[dict]] = field(default_factory=dict)  # "nba" -> that sport's top picks
    recommended: list = field(default_factory=list, repr=False)  # Edge objects shown (recorded for CLV)

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in self.__dict__.items() if k != "recommended"}, default=str, indent=1)


def _local(dt: datetime, tz) -> datetime:
    return dt.astimezone(tz)


def _short_team(name: str | None) -> str:
    """'Los Angeles Lakers' -> 'Lakers' (last word, or last two for 'Red Sox' style names)."""
    if not name:
        return ""
    parts = name.split()
    if len(parts) >= 3 and parts[-2] in {"Red", "White", "Blue", "Trail"}:
        return " ".join(parts[-2:])
    return parts[-1]


def play_dict(e: Edge, events: dict, tz, bankroll: float) -> dict:
    ev = events.get(e.event_id)
    start = _local(ev.start_time if ev else e.start_time, tz)
    matchup = f"{_short_team(ev.away_team)} @ {_short_team(ev.home_team)}" if ev else ""
    return {
        "id": f"{e.event_id}|{e.bookmaker}|{e.stat_type}|{e.period}|{e.side}|{e.line}|{e.player}|{e.team}",
        "sport": e.sport,
        "subject": text.subject(e),
        "market": text.market(e),
        "line": e.line,
        "matchup": matchup,
        "start": start.isoformat(),
        "start_label": start.strftime("%I:%M %p").lstrip("0"),
        "book": e.bookmaker,
        "book_label": text.book(e.bookmaker),
        "price": text.american(e.price_american) if e.payout_type != "pickem" else "Pick'em",
        "decimal": american_to_decimal(e.price_american) if e.payout_type != "pickem" and e.price_american else 0.0,
        "kind": e.payout_type,
        "fair": e.fair_prob,
        "implied": e.implied_prob,
        "edge_pct": e.ev_pct,
        "stake": e.stake,
        "stake_pct": (e.stake / bankroll * 100) if bankroll else 0.0,
        "confidence": e.confidence,
        "source": "Pinnacle" if e.fair_source == "pinnacle" else f"Consensus of {len(e.fair_books)}",
        "fair_books": [text.book(b) for b in e.fair_books],
        "line_gap": e.line_gap,
        "alt": e.is_alt_line,
        "states": e.usable_in,
        "notes": e.context_notes,
        "status": e.player_status,
        "breakeven_slip": e.breakeven_slip,
        "reason": text.reason(e),
        "driver": text.key_driver(e),
        "deeplink": e.deeplink,
        "odd_id": e.provider_market_id,
        "event_id": e.event_id,
    }


def slip_dict(s: Slip, events: dict, tz, bankroll: float) -> dict:
    return {
        "app": s.app,
        "app_label": text.book(s.app),
        "kind": s.kind,
        "size": s.size,
        "ev_pct": s.ev_pct,
        "win_prob": s.win_prob,
        "stake": s.stake,
        "verified": s.verified_table,
        "same_game_pairs": s.same_game_pairs,
        "payouts": {str(k): v for k, v in sorted(s.payouts.items(), reverse=True)},
        "legs": [
            {"subject": text.subject(l), "market": text.market(l), "fair": p, "confidence": l.confidence,
             "matchup": play_dict(l, events, tz, bankroll)["matchup"], "notes": l.context_notes}
            for l, p in zip(s.legs, s.probs)
        ],
    }


def bets_for_day(conn: sqlite3.Connection, now: datetime, tz) -> list[dict]:
    """Bets whose game is today or later (local), plus any noted today."""
    start = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    rows = conn.execute(
        "SELECT * FROM bets WHERE start_time >= ? OR created_at >= ? ORDER BY start_time, id",
        (start.astimezone(UTC).isoformat(), start.astimezone(UTC).isoformat()),
    ).fetchall()
    out = []
    for b in rows:
        st = datetime.fromisoformat(b["start_time"]).astimezone(tz)
        line = "" if b["line"] is None else f" {text.line_str(b['line'], signed=b['bet_type'] == 'sp')}"
        stat = text.StatMatcher(b["sport"]).display(b["stat_type"])
        side = {"home": "", "away": ""}.get(b["side"], b["side"].title() + " ")
        what = {"ml": "Moneyline", "sp": f"Spread{line}"}.get(b["bet_type"], f"{side}{line.strip()} {stat}".strip())
        out.append({
            "id": b["id"], "who": b["player"] or b["team"] or "Game total", "market": what,
            "book_label": text.book(b["bookmaker"]),
            "price": text.american(b["price_american"]) if b["price_american"] is not None else "Pick'em",
            "stake": b["stake"] or 0.0, "result": b["result"], "profit": b["profit"],
            "start_label": st.strftime("%I:%M %p").lstrip("0"), "day": f"{st.month}/{st.day}",
            "from_pick": b["rec_id"] is not None,
            "edit": {"stake": b["stake"] or 0.0, "price": b["price_american"], "line": b["line"], "side": b["side"],
                     "sides": list(SIDES.get(b["bet_type"], ())), "book": b["bookmaker"]},
        })
    return out


def pick_top(plays: dict[str, list[dict]], rcfg: ReportCfg) -> list[dict]:
    """Best plays across all sports by edge x confidence weight, one per player
    (or per game line), sportsbook bets and pick'em legs alike."""
    scored = sorted(
        (p for ps in plays.values() for p in ps),
        key=lambda p: p["edge_pct"] * rcfg.confidence_weight.get(p["confidence"], 0.5),
        reverse=True,
    )
    out, seen = [], set()
    for p in scored:
        who = (p["sport"], p["subject"], p["matchup"])
        if who in seen:
            continue
        seen.add(who)
        out.append({**p, "rank": len(out) + 1,
                    "score": p["edge_pct"] * rcfg.confidence_weight.get(p["confidence"], 0.5)})
        if len(out) == rcfg.top_picks:
            break
    return out


def _game_lines(g) -> list[str]:
    bits = []
    if g.probable_pitchers:
        bits.append(f"Starters: {g.probable_pitchers.get('away') or 'TBD'} vs {g.probable_pitchers.get('home') or 'TBD'}")
    if g.lineups_posted:
        posted = [s for s, v in g.lineups_posted.items() if v]
        bits.append("Lineups posted" if len(posted) == 2 else "Lineups: " + (f"{posted[0]} only" if posted else "not yet posted"))
    if g.weather:
        bits.append("Weather: " + g.weather.describe())
    bits += g.notes
    return bits


def build_report(
    conn: sqlite3.Connection,
    settings: Settings,
    sports: list[Sport],
    *,
    now: datetime,
    generated_at: datetime | None = None,
    rcfg: ReportCfg | None = None,
    use_context: bool = True,
) -> Report:
    rcfg = rcfg or load_report_config()
    tz = settings.tz
    a = analyze(conn, settings, sports, now=now, use_context=use_context)
    res, cfg = a.result, a.cfg
    bankroll = cfg.bankroll.amount
    event_ids = sorted({p.event_id for p in a.props})
    events = events_by_id(conn, event_ids)
    warnings: list[dict] = []
    from bookerbud.context.apply import load_context_rules

    use_status = load_context_rules().use_provider_status

    # Freshness per sport
    runs = latest_run_ids(conn, [s.value for s in sports])
    freshness = []
    max_age = cfg.filters.max_line_age_minutes
    for sp in sports:
        run_id = runs.get(sp.value)
        if run_id is None:
            freshness.append({"sport": sp.value, "fetched_at": None, "age_min": None, "events": 0, "stale": True})
            continue
        info = run_info(conn, run_id)
        fetched = max((p.fetched_at for p in a.props if p.sport is sp), default=None)
        n_events = len({p.event_id for p in a.props if p.sport is sp})
        age = (now - fetched).total_seconds() / 60 if fetched else None
        ctx_at = latest_context_time(conn, sp.value)
        freshness.append({
            "sport": sp.value, "fetched_at": fetched.isoformat() if fetched else None,
            "age_min": age, "events": n_events, "stale": age is None or age > max_age,
            "context_at": ctx_at.isoformat() if ctx_at else None,
        })
        for n in json.loads(info.get("notices") or "[]"):
            if "Upgrade your API key" in n:
                continue  # expected on the free plan; covered by the plan note below
            warnings.append({"level": "warn", "text": f"{sp.value.upper()}: provider says: {n}"})
        if fetched and age is not None and age > max_age and n_events:
            warnings.append({"level": "warn", "text": (
                f"{sp.value.upper()} lines are {age / 60:.1f} h old, older than the {max_age:.0f}-minute limit, "
                "so its plays are hidden. Tap Refresh odds (or run `betedge daily`) for fresh prices.")})
        if n_events and use_context and ctx_at is None:
            warnings.append({"level": "warn", "text": f"No injury/lineup context for {sp.value.upper()} yet. Run `betedge context`."})
        sp_props = [p for p in a.props if p.sport is sp and p.market_kind.value == "player_prop"]
        if use_status and sp_props and not any(p.player_status for p in sp_props):
            warnings.append({"level": "info", "text": (
                f"SportsGameOdds sent no player injury statuses for {sp.value.upper()}"
                + (", so only the MLB roster check filters injuries." if sp is Sport.MLB else
                   ", so injured players are not filtered for this sport. Check news before betting."))})
    for f in freshness:
        if f["fetched_at"] is None:
            warnings.append({"level": "info", "text": f"No {f['sport'].upper()} snapshot yet."})

    # Quota
    q = quota_status(conn, settings, now)
    quota = {"used": q.used, "budget": q.budget, "limit": q.limit, "window_days": q.window_days,
             "pct_of_budget": (q.used / q.budget * 100) if q.budget else 0}
    if quota["pct_of_budget"] >= rcfg.quota_warn_pct:
        warnings.append({"level": "warn", "text": (
            f"Quota: {q.used} of {q.budget} objects used in the last {q.window_days} days. "
            "Fetch less often until it rolls off.")})

    if not any(p.bookmaker in settings.bookmakers.sharp for p in a.props) and a.props:
        warnings.append({"level": "info", "text": (
            "No Pinnacle prices on the free plan, so every fair line is a consensus of other books "
            "(tagged low confidence).")})

    # Plays
    from bookerbud.tracking.settle import load_tracking_config

    form_games = load_tracking_config().history.form_games
    plays: dict[str, list[dict]] = {}
    recommended: list[Edge] = []
    for sp in sports:
        sp_edges = [e for e in res.edges if e.sport == sp.value][: rcfg.top_plays_per_sport]
        recommended += sp_edges
        sp_plays = []
        for e in sp_edges:
            pd_ = play_dict(e, events, tz, bankroll)
            pd_["form"] = form_for(conn, e, events, form_games)
            pd_["why"] = text.why(e, pd_["form"])
            sp_plays.append(pd_)
        plays[sp.value] = sp_plays
    for sport_plays in plays.values():
        for i, p in enumerate(sport_plays):
            p["anchor"] = f"play-{p['sport']}-{i + 1}"
    # Record what's shown (for CLV/performance) and link each card to its
    # recommendation #, so "Mark placed" knows what you bet and placed cards show it.
    rec_day = now.astimezone(tz).date().isoformat()
    record_recommendations(conn, recommended, rec_day, datetime.now(UTC))
    ids = {(r["event_id"], r["bookmaker"], r["provider_market_id"], r["line"]): r["id"]
           for r in recommendations_for_day(conn, rec_day)}
    placed = {r["rec_id"]: dict(r) for r in conn.execute("SELECT * FROM bets WHERE rec_id IS NOT NULL")}
    for sport_plays in plays.values():
        for p in sport_plays:
            p["rec_id"] = ids.get((p["event_id"], p["book"], p["odd_id"], p["line"]))
            b = placed.get(p["rec_id"])
            p["placed"] = ({"stake": b["stake"], "price": text.american(b["price_american"]) if b["price_american"] is not None else "pick'em"}
                           if b else None)
    my_bets = bets_for_day(conn, now, tz)
    top_picks = pick_top(plays, rcfg)
    top_by_sport = {sp: pick_top({sp: ps}, rcfg) for sp, ps in plays.items()}
    top_anchors = {p["anchor"] for p in top_picks} | {p["anchor"] for ps in top_by_sport.values() for p in ps}
    for sport_plays in plays.values():
        for p in sport_plays:
            p["is_top"] = p["anchor"] in top_anchors  # their "Why this pick" opens by default

    # Slips
    payouts = load_payouts()
    slips = build_slips(res.pickem_legs, payouts, cfg, sizes=range(min(rcfg.slip_legs), max(rcfg.slip_legs) + 1),
                        top_per_size=rcfg.top_slips)
    slips.sort(key=lambda s: s.ev_pct, reverse=True)
    slip_rows = [slip_dict(s, events, tz, bankroll) for s in slips[: rcfg.top_slips]]
    if any(not s["verified"] for s in slip_rows):
        warnings.append({"level": "warn", "text": (
            "Some slips use a payout table that couldn't be verified automatically (Underdog). "
            "Check the payout in the app before entering.")})
    if not any(p.payout_type.value == "pickem" for p in a.props) and a.props:
        warnings.append({"level": "info", "text": (
            "No pick'em lines in the data (PrizePicks, Underdog and Sleeper aren't on the free plan), "
            "so there are no slips.")})

    # Games context
    games = []
    for eid in event_ids:
        ev = events.get(eid)
        if not ev:
            continue
        g = a.games.get(eid)
        games.append({
            "sport": ev.sport.value,
            "matchup": f"{ev.away_team} @ {ev.home_team}",
            "start": _local(ev.start_time, tz).isoformat(),
            "start_label": _local(ev.start_time, tz).strftime("%I:%M %p").lstrip("0"),
            "lines": _game_lines(g) if g else [],
        })
    games.sort(key=lambda g: g["start"])

    from bookerbud.tracking.performance import summarize

    performance = summarize(conn)
    all_plays = [p for ps in plays.values() for p in ps]
    totals = {
        "plays": len(all_plays),
        "best_edge": max((p["edge_pct"] for p in all_plays), default=None),
        "total_stake": round(sum(p["stake"] for p in all_plays if p["kind"] != "pickem"), 2),
        "slips": len(slip_rows),
    }
    return Report(
        generated_at=generated_at or now, as_of=now, tz=settings.timezone, sports=[s.value for s in sports],
        freshness=freshness, quota=quota, warnings=warnings, plays=plays, slips=slip_rows, games=games,
        skipped=dict(res.skipped), totals=totals, recommended=recommended, performance=performance,
        top_picks=top_picks, top_picks_by_sport=top_by_sport, my_bets=my_bets,
        settings={"bankroll": bankroll, "kelly_fraction": cfg.bankroll.kelly_fraction,
                  "max_bet_pct": cfg.bankroll.max_bet_pct, "min_edge_pct": cfg.filters.min_edge_pct,
                  "states": settings.states, "plan": settings.plan, "max_line_age": max_age,
                  "injury_filter": "all sports" if use_status else "MLB injured list only (NBA/NFL off)"},
    )
