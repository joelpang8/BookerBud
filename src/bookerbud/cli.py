"""`betedge` command-line interface. Research only: nothing here places bets or
logs into any betting account."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer

from bookerbud.config import load_settings
from bookerbud.edge.config import load_availability, load_edge_config, load_payouts
from bookerbud.edge.engine import EdgeResult, find_edges
from bookerbud.edge.slips import build_slips
from bookerbud.models import Sport
from bookerbud.pipeline import FetchSummary, QuotaExceeded, fetch_and_store, quota_status, replay_and_store
from bookerbud.providers import ProviderError, get_provider
from bookerbud.storage import connect, flagged_summary, latest_runs, load_latest_props, query_player_props

app = typer.Typer(help="BookerBud: daily +EV research. Never places bets.", no_args_is_help=True)


def _sports(sport: str) -> list[Sport]:
    if sport.lower() == "all":
        return list(Sport)
    try:
        return [Sport(sport.lower())]
    except ValueError:
        raise typer.BadParameter(f"sport must be one of {[s.value for s in Sport]} or 'all'") from None


def _print_summary(s: FetchSummary) -> None:
    r, rep = s.result, s.report
    by_sport: dict[str, int] = {}
    for e in r.events:
        by_sport[e.sport.value] = by_sport.get(e.sport.value, 0) + 1
    typer.echo(
        f"[{','.join(x.value for x in s.sports)}] run {s.run_id}: {len(r.events)} events {by_sport}, "
        f"{rep.total} prop rows ({rep.markets} markets, {rep.single_book_markets} offered by only one book), "
        f"{r.requests_made} API requests, {r.objects_used} quota objects"
    )
    if r.truncated:
        typer.secho("  WARNING: stopped early to stay inside the quota budget; slate is incomplete.", fg=typer.colors.RED)
    for n in r.notices:
        typer.secho(f"  provider notice: {n}", fg=typer.colors.YELLOW)
    if rep.flags:
        flags = {str(f): n for f, n in rep.flags.items()}
        typer.secho(f"  flagged rows: {flags}  (see `betedge unmatched`)", fg=typer.colors.YELLOW)
    if r.skipped_markets:
        typer.echo(f"  skipped out-of-scope markets: {dict(r.skipped_markets)}")
    by_book: dict[str, int] = {}
    for p in r.props:
        by_book[p.bookmaker] = by_book.get(p.bookmaker, 0) + 1
    typer.echo(f"  rows by book: {by_book or '{}'}")


@app.command()
def fetch(
    sport: Annotated[str, typer.Option(help="nba, mlb, nfl, or all")] = "all",
    day: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD in the configured timezone (default today)")] = None,
    include_live: Annotated[bool, typer.Option(help="Also include games that already started")] = False,
    force: Annotated[bool, typer.Option(help="Allow spending past the quota budget, up to the plan's hard limit")] = False,
    replay: Annotated[
        list[Path] | None, typer.Option(help="Re-ingest logged raw response file(s)/dir instead of calling the API")
    ] = None,
) -> None:
    """Fetch today's events, game lines and player props (one API call for all
    requested sports) and store a snapshot."""
    settings = load_settings()
    sports = _sports(sport)
    provider = get_provider(settings)
    conn = connect(settings.db_file)
    try:
        if replay:
            files = sorted(f for p in replay for f in ([p] if p.is_file() else p.rglob("*.json")))
            _print_summary(replay_and_store(conn, settings, provider, sports, files))
            return
        target = date.fromisoformat(day) if day else datetime.now(settings.tz).date()
        try:
            _print_summary(fetch_and_store(conn, settings, provider, sports, target, include_started=include_live, force=force))
        except (ProviderError, QuotaExceeded) as exc:
            typer.secho(f"fetch failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        q = quota_status(conn, settings)
        typer.echo(f"  quota: {q.used}/{q.budget} budget ({q.limit} plan limit) over the last {q.window_days} days")
    finally:
        provider.close()
        conn.close()


@app.command()
def quota() -> None:
    """Local quota accounting (no API call). `betedge usage` asks the provider."""
    settings = load_settings()
    conn = connect(settings.db_file)
    q = quota_status(conn, settings)
    conn.close()
    typer.echo(
        f"{q.used} objects used in the last {q.window_days} days; budget {q.budget} "
        f"({settings.quota.budget_fraction:.0%} of the {q.limit} plan limit); {q.remaining} left in budget."
    )


def _fmt_price(row: pd.Series) -> str:
    if row["payout_type"] == "pickem":
        return "pick'em"
    if pd.isna(row["price_american"]):
        return "-"
    v = int(row["price_american"])
    return f"+{v}" if v > 0 else str(v)


@app.command("show-props")
def show_props(
    player: Annotated[str, typer.Option(help="Player name (partial, accent/case-insensitive)")],
    sport: Annotated[str | None, typer.Option(help="nba, mlb, nfl")] = None,
    stat: Annotated[str | None, typer.Option(help="Canonical stat type, e.g. points, total_bases")] = None,
    book: Annotated[str | None, typer.Option(help="Only this bookmaker")] = None,
    alt: Annotated[bool, typer.Option(help="Include alternate lines")] = False,
    history: Annotated[bool, typer.Option(help="Show every snapshot (line movement), not just the latest")] = False,
) -> None:
    """Show a player's props lined up across books."""
    settings = load_settings()
    conn = connect(settings.db_file)
    df = query_player_props(conn, player, sport=sport, stat_type=stat, bookmaker=book, include_alt=alt, history=history)
    conn.close()
    if df.empty:
        typer.echo(f"No props found for {player!r}. Run `betedge fetch` first?")
        raise typer.Exit(1)

    tz = settings.tz
    df["price"] = df.apply(_fmt_price, axis=1)
    df["updated"] = pd.to_datetime(df["book_updated_at"], utc=True).dt.tz_convert(tz).dt.strftime("%m-%d %H:%M")
    df["fetched"] = pd.to_datetime(df["fetched_at"], utc=True).dt.tz_convert(tz).dt.strftime("%m-%d %H:%M")
    df["avail"] = df["available"].map({1: "", 0: "closed"})
    df["alt"] = df["is_alt_line"].map({1: "alt", 0: ""})
    cols = ["bookmaker", "side", "line", "price", "avail", "alt", "updated"]
    if history:
        cols.insert(0, "fetched")
    cols.append("flags")

    keys = ["event_id", "player", "stat_type", "period"]
    for (_, name, stat_type, period), g in df.groupby(keys, sort=False):
        r = g.iloc[0]
        start = pd.Timestamp(r["start_time"]).tz_convert(tz).strftime("%a %m-%d %I:%M %p %Z")
        status = f" [{r['player_status']}]" if r["player_status"] else ""
        typer.secho(
            f"\n{name}{status} — {stat_type} ({period})  |  {r['team'] or '?'} vs {r['opponent'] or '?'}  |  {start}",
            bold=True,
        )
        typer.echo(g[cols].to_string(index=False))


@app.command()
def unmatched(run_id: Annotated[int | None, typer.Option(help="Fetch run (default: latest run of each sport)")] = None) -> None:
    """List rows the matcher flagged (unknown stat labels, unresolved players, ...)."""
    settings = load_settings()
    conn = connect(settings.db_file)
    df = flagged_summary(conn, run_id)
    conn.close()
    typer.echo("Nothing flagged." if df.empty else df.to_string(index=False))


@app.command()
def runs(limit: int = 10) -> None:
    """Recent fetch runs with counts, provider notices and raw-log paths."""
    settings = load_settings()
    conn = connect(settings.db_file)
    df = latest_runs(conn, limit)
    conn.close()
    if df.empty:
        typer.echo("No runs yet.")
        return
    df["raw_files"] = df["raw_paths"].map(lambda s: len(json.loads(s or "[]")))
    typer.echo(df[["run_id", "sport", "source", "started_at", "requests_made", "objects_used", "n_events", "n_props", "n_flagged", "notices", "raw_files"]].to_string(index=False))


@app.command()
def usage() -> None:
    """Show the odds provider's quota/rate-limit usage (one API request)."""
    settings = load_settings()
    provider = get_provider(settings)
    try:
        typer.echo(json.dumps(provider.usage(), indent=2))
    except ProviderError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        provider.close()


def _run_engine(sport: str, as_of: str | None, all_books: bool, min_edge: float | None) -> tuple[EdgeResult, object]:
    settings = load_settings()
    cfg = load_edge_config()
    if min_edge is not None:
        cfg.filters.min_edge_pct = min_edge
    conn = connect(settings.db_file)
    props = load_latest_props(conn, [s.value for s in _sports(sport)])
    conn.close()
    if not props:
        typer.echo("No snapshot to analyse. Run `betedge fetch` first.")
        raise typer.Exit(1)
    now = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=settings.tz)
    res = find_edges(
        props, cfg, now=now, sharp_books=settings.bookmakers.sharp, states=settings.states,
        availability=load_availability(), payouts=load_payouts(), all_books=all_books,
    )
    return res, cfg


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _odds(v: int | None, payout_type: str) -> str:
    if payout_type == "pickem":
        return "pick'em"
    return "-" if v is None else (f"+{v}" if v > 0 else str(v))


def _describe(e) -> str:
    if e.market_kind == "player_prop":
        who = e.player
    elif e.bet_type in ("ml", "sp"):
        who = e.team
    else:
        who = f"{e.team} team total" if e.team else "Game total"
    period = "" if e.period == "game" else f" [{e.period}]"
    line = "" if e.line is None else f" {e.line:+g}" if e.bet_type == "sp" else f" {e.line:g}"
    side = {"home": "", "away": ""}.get(e.side, e.side + " ")
    stat = {"ml": "ML", "sp": "spread"}.get(e.bet_type, e.stat_type)
    return f"{who} {side}{stat}{line}{period}".replace("  ", " ")


@app.command()
def edges(
    sport: Annotated[str, typer.Option(help="nba, mlb, nfl, or all")] = "all",
    book: Annotated[str | None, typer.Option(help="Only this bookmaker")] = None,
    min_edge: Annotated[float | None, typer.Option(help="Override filters.min_edge_pct")] = None,
    all_books: Annotated[bool, typer.Option(help="Include books not usable in your states")] = False,
    as_of: Annotated[str | None, typer.Option(help="Evaluate freshness as of this ISO time (for replays)")] = None,
    limit: int = 30,
) -> None:
    """+EV plays from the latest snapshot (research only)."""
    res, _ = _run_engine(sport, as_of, all_books, min_edge)
    rows = [e for e in res.edges if not book or e.bookmaker == book][:limit]
    if not rows:
        typer.echo("No plays pass the filters.")
    else:
        df = pd.DataFrame([{
            "sport": e.sport, "play": _describe(e), "book": e.bookmaker, "price": _odds(e.price_american, e.payout_type),
            "fair": _pct(e.fair_prob), "implied": _pct(e.implied_prob), "edge%": f"{e.ev_pct:+.1f}",
            "stake": f"${e.stake:.2f}" if e.payout_type != "pickem" else f"(vs {e.breakeven_slip})",
            "conf": e.confidence, "fair from": e.fair_source if e.fair_source != "consensus" else f"consensus({len(e.fair_books)})",
            "gap": f"{e.line_gap:g}" if e.line_gap else "", "states": ",".join(e.usable_in) or "-",
            "status": e.player_status or "",
        } for e in rows])
        typer.echo(df.to_string(index=False))
    typer.echo(f"\nskipped: {dict(res.skipped)}")


@app.command()
def slips(
    sport: Annotated[str, typer.Option(help="nba, mlb, nfl, or all")] = "all",
    app_name: Annotated[str | None, typer.Option("--app", help="prizepicks / underdog")] = None,
    legs: Annotated[str, typer.Option(help="Leg counts, e.g. 2-3 or 2-6")] = "2-3",
    all_books: Annotated[bool, typer.Option(help="Include apps not usable in your states")] = False,
    as_of: Annotated[str | None, typer.Option(help="Evaluate freshness as of this ISO time (for replays)")] = None,
    top: int = 3,
) -> None:
    """Best pick'em slips (same-game legs excluded or penalised per config)."""
    res, cfg = _run_engine(sport, as_of, all_books, None)
    lo, _, hi = legs.partition("-")
    sizes = range(int(lo), int(hi or lo) + 1)
    payouts = load_payouts()
    if app_name:
        payouts = {app_name: payouts[app_name]}
    found = build_slips(res.pickem_legs, payouts, cfg, sizes=sizes, top_per_size=top)
    if not found:
        typer.echo("No +EV slips. (On the free SGO tier there are no pick'em lines to build from.)")
        return
    for sl in found:
        tag = "" if sl.verified_table else "  [payout table UNVERIFIED - check in app]"
        typer.secho(
            f"\n{sl.app} {sl.kind} {sl.size}-pick: EV {sl.ev_pct:+.1f}%, P(cash) {_pct(sl.win_prob)}, "
            f"stake ${sl.stake:.2f}{tag}", bold=True,
        )
        for leg, p in zip(sl.legs, sl.probs):
            typer.echo(f"  {_describe(leg):40} fair {_pct(p)}  ({leg.confidence}, {','.join(leg.usable_in)})")


if __name__ == "__main__":
    app()
