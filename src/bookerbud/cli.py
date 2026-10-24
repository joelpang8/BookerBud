"""`betedge` command-line interface. Research only: nothing here places bets or
logs into any betting account."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer

from bookerbud.config import load_settings
from bookerbud.models import Sport
from bookerbud.pipeline import FetchSummary, fetch_and_store, replay_and_store
from bookerbud.providers import ProviderError, get_provider
from bookerbud.storage import connect, flagged_summary, latest_runs, query_player_props

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
    typer.echo(
        f"[{s.sport.value}] run {s.run_id}: {len(r.events)} events, {rep.total} prop rows "
        f"({rep.markets} markets, {rep.single_book_markets} offered by only one book), "
        f"{r.requests_made} API requests"
    )
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
    replay: Annotated[
        list[Path] | None, typer.Option(help="Re-ingest logged raw response file(s)/dir instead of calling the API")
    ] = None,
) -> None:
    """Fetch today's events, game lines and player props, and store a snapshot."""
    settings = load_settings()
    sports = _sports(sport)
    provider = get_provider(settings)
    conn = connect(settings.db_file)
    try:
        if replay:
            files = sorted(f for p in replay for f in ([p] if p.is_file() else p.rglob("*.json")))
            if len(sports) != 1:
                raise typer.BadParameter("--replay needs a single --sport")
            _print_summary(replay_and_store(conn, settings, provider, sports[0], files))
            return
        target = date.fromisoformat(day) if day else datetime.now(settings.tz).date()
        for sp in sports:
            try:
                _print_summary(fetch_and_store(conn, settings, provider, sp, target, include_started=include_live))
            except ProviderError as exc:
                typer.secho(f"[{sp.value}] fetch failed: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1) from exc
    finally:
        provider.close()
        conn.close()


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
    typer.echo(df[["run_id", "sport", "source", "started_at", "requests_made", "n_events", "n_props", "n_flagged", "notices", "raw_files"]].to_string(index=False))


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


if __name__ == "__main__":
    app()
