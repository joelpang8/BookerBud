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
from bookerbud.context.apply import load_context_rules
from bookerbud.edge.config import load_payouts
from bookerbud.edge.engine import EdgeResult
from bookerbud.edge.slips import build_slips
from bookerbud.models import Sport
from bookerbud.context.http import JsonClient
from bookerbud.context.mlb import MLBContext
from bookerbud.context.weather import OpenMeteo
from bookerbud.pipeline import (
    FetchSummary,
    analyze,
    QuotaExceeded,
    fetch_and_store,
    load_context,
    quota_status,
    refresh_context,
    replay_and_store,
)
from bookerbud.raw_log import RawLogger
from bookerbud.providers import ProviderError, get_provider
from bookerbud.storage import connect, flagged_by_label, flagged_summary, latest_runs, load_latest_props, query_player_props
from bookerbud.storage.tracking import get_recommendation, insert_bet, record_recommendations, recommendations_for_day

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
    no_context: Annotated[bool, typer.Option(help="Skip the injury/lineup/weather/rest refresh")] = False,
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
            # A folder holds odds, schedule and settle responses; only odds snapshots replay.
            files = sorted(f for p in replay for f in ([p] if p.is_file() else p.rglob("*_events_*.json")))
            _print_summary(replay_and_store(conn, settings, provider, sports, files))
            return
        target = date.fromisoformat(day) if day else datetime.now(settings.tz).date()
        try:
            _print_summary(fetch_and_store(conn, settings, provider, sports, target, include_started=include_live, force=force))
        except (ProviderError, QuotaExceeded) as exc:
            typer.secho(f"fetch failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from exc
        if not no_context:
            _context(conn, settings, provider, sports, target, backfill=True)
        q = quota_status(conn, settings)
        typer.echo(f"  quota: {q.used}/{q.budget} budget ({q.limit} plan limit) over the last {q.window_days} days")
    finally:
        provider.close()
        conn.close()


def _mlb_context(settings) -> MLBContext:
    raw = RawLogger(settings.raw_dir)
    return MLBContext(JsonClient("mlb-statsapi", raw), OpenMeteo(JsonClient("open-meteo", raw)))


def _context(conn, settings, provider, sports, day, *, backfill: bool) -> None:
    mlb = _mlb_context(settings) if Sport.MLB in sports else None
    rules = load_context_rules()
    try:
        summary = refresh_context(conn, settings, provider, sports, day, mlb=mlb,
                                  backfill_nba=backfill and rules.nba.backfill_yesterday)
    except Exception as exc:  # context is advisory; never lose the odds snapshot over it
        typer.secho(f"  context refresh failed: {exc}", fg=typer.colors.YELLOW, err=True)
        return
    finally:
        if mlb:
            mlb.client.close()
            mlb.weather.client.close()
    typer.echo(f"  context: {summary.games} games, {summary.players} player statuses"
               + (f", {summary.objects_used} quota objects (NBA schedule backfill)" if summary.objects_used else ""))
    for w in summary.warnings or []:
        typer.secho(f"  context warning: {w}", fg=typer.colors.YELLOW)


@app.command()
def context(
    sport: Annotated[str, typer.Option(help="nba, mlb, nfl, or all")] = "all",
    day: Annotated[str | None, typer.Option("--date", help="YYYY-MM-DD (default today)")] = None,
    no_backfill: Annotated[bool, typer.Option(help="Don't spend quota backfilling yesterday's NBA schedule")] = False,
) -> None:
    """Refresh injuries, MLB lineups/probable pitchers/weather and NBA rest for stored events."""
    settings = load_settings()
    provider = get_provider(settings)
    conn = connect(settings.db_file)
    try:
        target = date.fromisoformat(day) if day else datetime.now(settings.tz).date()
        _context(conn, settings, provider, _sports(sport), target, backfill=not no_backfill)
        games, _ = load_context(conn, _sports(sport))
        for g in games.values():
            bits = []
            if g.probable_pitchers:
                bits.append(f"SP {g.probable_pitchers.get('away') or 'TBD'} vs {g.probable_pitchers.get('home') or 'TBD'}")
            if g.lineups_posted:
                bits.append("lineups " + ("posted" if all(g.lineups_posted.values()) else "not yet posted"))
            if g.weather:
                bits.append(g.weather.describe())
            bits += g.notes
            typer.echo(f"  [{g.sport}] {g.away_team} @ {g.home_team}: {'; '.join(bits) or '-'}")
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
def unmatched(
    run_id: Annotated[int | None, typer.Option(help="Fetch run (default: latest run of each sport)")] = None,
    detail: Annotated[bool, typer.Option(help="One line per player instead of per stat label")] = False,
) -> None:
    """List rows the matcher flagged (unknown stat labels, unresolved players, ...)."""
    settings = load_settings()
    conn = connect(settings.db_file)
    df = flagged_summary(conn, run_id) if detail else flagged_by_label(conn, run_id)
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


def _as_of(settings, as_of: str | None) -> datetime:
    now = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=settings.tz)


def _run_engine(
    sport: str, as_of: str | None, all_books: bool, min_edge: float | None, use_context: bool = True
) -> tuple[EdgeResult, object]:
    settings = load_settings()
    conn = connect(settings.db_file)
    try:
        a = analyze(conn, settings, _sports(sport), now=_as_of(settings, as_of), use_context=use_context,
                    all_books=all_books, min_edge=min_edge)
    finally:
        conn.close()
    if not a.props:
        typer.echo("No snapshot to analyse. Run `betedge fetch` first.")
        raise typer.Exit(1)
    return a.result, a.cfg


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
    no_context: Annotated[bool, typer.Option(help="Ignore injury/lineup/weather/rest context")] = False,
    limit: int = 30,
) -> None:
    """+EV plays from the latest snapshot (research only)."""
    res, _ = _run_engine(sport, as_of, all_books, min_edge, not no_context)
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
            "notes": "; ".join(e.context_notes),
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


@app.command()
def report(
    sport: Annotated[str, typer.Option(help="nba, mlb, nfl, or all")] = "all",
    as_of: Annotated[str | None, typer.Option(help="Evaluate freshness as of this ISO time (for replays)")] = None,
    no_context: Annotated[bool, typer.Option(help="Ignore injury/lineup/weather/rest context")] = False,
) -> None:
    """Write today's dashboard (HTML) and Markdown report to the reports folder."""
    from bookerbud.report.build import build_report, load_report_config, report_dir
    from bookerbud.report.html import render_html
    from bookerbud.report.markdown import render_markdown

    settings = load_settings()
    rcfg = load_report_config()
    out_dir = report_dir(settings, rcfg)
    conn = connect(settings.db_file)
    try:
        now = _as_of(settings, as_of)
        r = build_report(conn, settings, _sports(sport), now=now, generated_at=datetime.now(UTC),
                         rcfg=rcfg, use_context=not no_context)
    finally:
        conn.close()
    stem = now.astimezone(settings.tz).strftime("%Y-%m-%d")
    conn = connect(settings.db_file)
    try:
        record_recommendations(conn, r.recommended, stem, datetime.now(UTC))
    finally:
        conn.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_doc = render_html(r)
    (out_dir / f"{stem}.html").write_text(html_doc)
    (out_dir / "latest.html").write_text(html_doc)
    (out_dir / f"{stem}.md").write_text(render_markdown(r))
    (out_dir / f"{stem}.json").write_text(r.to_json())
    typer.echo(f"{r.totals['plays']} plays, {r.totals['slips']} slips, "
               f"{sum(1 for w in r.warnings if w['level'] == 'warn')} warnings")
    typer.echo(f"  dashboard: {out_dir / f'{stem}.html'}  (also latest.html)")
    typer.echo(f"  markdown:  {out_dir / f'{stem}.md'}")
    typer.echo("  view on your phone: `betedge serve`")


def _american(v) -> str:
    return "pick'em" if v is None else (f"+{v}" if v > 0 else str(v))


def _rec_line(r) -> str:
    who = r["player"] or r["team"] or "Game total"
    line = "" if r["line"] is None else f" {r['line']:g}"
    price = "pick'em" if r["payout_type"] == "pickem" else (f"+{r['price_american']}" if (r["price_american"] or 0) > 0 else str(r["price_american"]))
    return (f"#{r['id']:<4} {r['sport'].upper()} {who} {r['side']}{line} {r['stat_type']} [{r['period']}] "
            f"@ {r['bookmaker']} {price}  edge {r['ev_pct']:+.1f}%  stake ${r['stake_suggested'] or 0:.2f}")


@app.command()
def log(
    rec_id: Annotated[int | None, typer.Argument(help="Recommendation # from `betedge log --list`")] = None,
    stake: Annotated[float | None, typer.Option(help="Dollars you staked")] = None,
    price: Annotated[int | None, typer.Option(help="American odds you actually got (default: the recommended price)")] = None,
    line: Annotated[float | None, typer.Option(help="Line you actually got (default: the recommended line)")] = None,
    list_: Annotated[bool, typer.Option("--list", help="Show today's recommendations with their #")] = False,
    day: Annotated[str | None, typer.Option("--date", help="Recommendation day for --list (YYYY-MM-DD)")] = None,
    player: Annotated[str | None, typer.Option(help="Log a bet that wasn't recommended: player name")] = None,
    team: Annotated[str | None, typer.Option(help="...or team (moneyline / spread / team total)")] = None,
    stat: Annotated[str | None, typer.Option(help="Canonical stat, e.g. points (default: points)")] = None,
    side: Annotated[str | None, typer.Option(help="over / under / home / away / yes / no")] = None,
    bet_type: Annotated[str | None, typer.Option(help="ou / sp / ml / yn")] = None,
    book: Annotated[str | None, typer.Option(help="Bookmaker id, e.g. draftkings")] = None,
    note: Annotated[str | None, typer.Option(help="Free-text note")] = None,
) -> None:
    """Record a bet you actually placed (BookerBud never places bets itself)."""
    settings = load_settings()
    conn = connect(settings.db_file)
    try:
        if list_:
            d = day or datetime.now(settings.tz).date().isoformat()
            rows = recommendations_for_day(conn, d)
            typer.echo("\n".join(_rec_line(r) for r in rows) if rows else f"No recommendations recorded for {d}. Run `betedge report`.")
            return
        if stake is None or stake <= 0:
            raise typer.BadParameter("--stake is required (dollars, > 0)")
        if rec_id is not None:
            r = get_recommendation(conn, rec_id)
            if r is None:
                raise typer.BadParameter(f"No recommendation #{rec_id}. See `betedge log --list`.")
            bet = {k: r[k] for k in ("sport", "league", "event_id", "start_time", "market_kind", "entity", "player",
                                    "player_key", "team", "opponent", "stat_type", "period", "bet_type", "side",
                                    "bookmaker", "payout_type", "provider_market_id", "is_alt_line", "fair_prob", "ev_pct")}
            bet.update(rec_id=rec_id, line=r["line"] if line is None else line,
                       price_american=r["price_american"] if price is None else price)
        else:
            bet = _bet_from_snapshot(conn, settings, player, team, stat, side, bet_type, book, line, price)
        bet.update(stake=stake, notes=note)
        if bet["payout_type"] != "pickem" and bet["price_american"] is None:
            raise typer.BadParameter("--price is required for sportsbook bets")
        bet_id = insert_bet(conn, bet)
        who = bet["player"] or bet["team"] or "Game total"
        typer.echo(f"Logged bet #{bet_id}: {who} {bet['side']} {bet['line'] if bet['line'] is not None else ''} "
                   f"{bet['stat_type']} @ {bet['bookmaker']} {_american(bet['price_american'])} for ${stake:.2f}")
    finally:
        conn.close()


def _bet_from_snapshot(conn, settings, player, team, stat, side, bet_type, book, line, price) -> dict:
    if not (player or team) or not side or not book:
        raise typer.BadParameter("Give a recommendation #, or --player/--team with --side and --book")
    from bookerbud.normalize.names import normalize_name

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
        raise typer.BadParameter("No matching market in the latest snapshot. Check --book/--side/--stat.")
    if len(keys) > 1:
        lines = sorted({f"{p.player or p.team} {p.stat_type} [{p.period}] {p.bet_type} {p.line}" for p in cands})
        raise typer.BadParameter("Several markets match; narrow with --stat/--bet-type:\n  " + "\n  ".join(lines[:10]))
    p = next(c for c in cands if c.period == "game") if any(c.period == "game" for c in cands) else cands[0]
    return {
        "sport": p.sport.value, "league": p.league, "event_id": p.event_id, "start_time": p.start_time.isoformat(),
        "market_kind": p.market_kind.value, "entity": p.entity, "player": p.player, "player_key": p.player_key,
        "team": p.team, "opponent": p.opponent, "stat_type": p.stat_type, "period": p.period, "bet_type": p.bet_type,
        "side": p.side, "bookmaker": p.bookmaker, "payout_type": p.payout_type.value,
        "provider_market_id": p.provider_market_id, "is_alt_line": 0, "line": p.line if line is None else line,
        "price_american": p.price_american if price is None else price, "rec_id": None,
    }


@app.command()
def bets(limit: int = 30) -> None:
    """Your logged bets, newest first, with closing-line value and results once settled."""
    settings = load_settings()
    conn = connect(settings.db_file)
    df = pd.read_sql_query(
        """SELECT id, start_time, sport, COALESCE(player, team, 'total') AS who, side, line,
                  stat_type, bookmaker AS book, price_american AS price, stake, clv_pct AS clv, result, profit
           FROM bets ORDER BY id DESC LIMIT ?""", conn, params=[limit])
    conn.close()
    if not df.empty:
        df.insert(1, "day", pd.to_datetime(df.pop("start_time"), utc=True).dt.tz_convert(settings.tz).dt.strftime("%m-%d"))
        df["price"] = df["price"].map(lambda v: "" if pd.isna(v) else (f"+{int(v)}" if v > 0 else str(int(v))))
        df["clv"] = df["clv"].map(lambda v: "" if pd.isna(v) else f"{v:+.1f}%")
    typer.echo("No bets logged yet. `betedge log --list`, then `betedge log <#> --stake 20`." if df.empty else df.to_string(index=False))


@app.command("settle")
def settle_cmd() -> None:
    """Record closing lines, CLV and results for finished games (recommendations and bets)."""
    from bookerbud.tracking.settle import settle as run_settle

    settings = load_settings()
    provider = get_provider(settings)
    conn = connect(settings.db_file)
    try:
        s = run_settle(conn, settings, provider)
    except ProviderError as exc:
        typer.secho(f"settle failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    finally:
        provider.close()
        conn.close()
    if not s.pending:
        typer.echo("settle: nothing to settle yet (games settle about 4 h after they start).")
        return
    typer.echo(f"settle: {s.pending} rows pending, {s.events} games fetched ({s.objects_used} quota objects), "
               f"results {dict(s.settled) or '{}'}, CLV recorded for {s.clv_rows}")
    for w in s.warnings:
        typer.secho(f"  {w}", fg=typer.colors.YELLOW)


def _fmt_pct(x, signed=False, ratio=True) -> str:
    if x is None or (isinstance(x, float) and x != x):
        return "–"
    v = x * 100 if ratio else x
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


@app.command()
def performance(
    since: Annotated[str | None, typer.Option(help="Only games on/after YYYY-MM-DD")] = None,
    by: Annotated[str, typer.Option(help="Breakdown: sport, stat or book")] = "sport",
) -> None:
    """ROI, CLV, hit rate and edge-bucket calibration (recommendations and your bets)."""
    from bookerbud.tracking.performance import summarize

    settings = load_settings()
    conn = connect(settings.db_file)
    p = summarize(conn, since)
    conn.close()
    r, b = p["recs"], p["bets"]
    typer.secho("Recommendations (every play the report showed)", bold=True)
    typer.echo(f"  {r['n']} plays, {r['graded']} graded ({p['pending']} pending) | hit {_fmt_pct(r['hit_rate'])} | "
               f"CLV {_fmt_pct(r['clv'], True, False)} over {r['clv_n']} | beat close {_fmt_pct(r['beat_close'])} | "
               f"flat ROI {_fmt_pct(r['flat_roi'], True)} ({r['flat_units']:+.2f} u)")
    typer.secho("Your bets", bold=True)
    if b["n"]:
        typer.echo(f"  {b['n']} bets, ${b['staked']:,.2f} settled stake, profit ${b['profit']:+,.2f}, ROI {_fmt_pct(b['roi'], True)} | "
                   f"hit {_fmt_pct(b['hit_rate'])} | CLV {_fmt_pct(b['clv'], True, False)} | beat close {_fmt_pct(b['beat_close'])}")
    else:
        typer.echo("  none logged yet (`betedge log --list`, then `betedge log <#> --stake 20`)")
    dim = {"stat": "stat", "book": "book", "sport": "sport"}.get(by, "sport")
    rows = p["by"][dim]["recs"]
    if rows:
        typer.secho(f"\nBy {dim} (recommendations)", bold=True)
        typer.echo(pd.DataFrame([{dim: x["key"], "plays": x["n"], "graded": x["graded"], "hit": _fmt_pct(x["hit_rate"]),
                                  "CLV": _fmt_pct(x["clv"], True, False), "flat ROI": _fmt_pct(x["flat_roi"], True)}
                                 for x in rows]).to_string(index=False))
    if p["edge_buckets"]:
        typer.secho("\nEdge buckets (is a bigger predicted edge actually better?)", bold=True)
        typer.echo(pd.DataFrame([{"edge": x["bucket"], "plays": x["n"], "graded": x["graded"],
                                  "predicted win": _fmt_pct(x["predicted"]), "actual win": _fmt_pct(x["hit_rate"]),
                                  "CLV": _fmt_pct(x["clv"], True, False), "flat ROI": _fmt_pct(x["flat_roi"], True)}
                                 for x in p["edge_buckets"]]).to_string(index=False))
    if p["calibration"]:
        typer.secho("\nProbability calibration", bold=True)
        typer.echo(pd.DataFrame([{"fair prob": f"{x['lo'] * 100:.0f}–{x['hi'] * 100:.0f}%", "n": x["n"],
                                  "predicted": _fmt_pct(x["predicted"]), "actual": _fmt_pct(x["actual"])}
                                 for x in p["calibration"]]).to_string(index=False))
    if r["graded"] and not r["clv_n"]:
        typer.secho("\nCLV is blank: closing odds are off, so it needs a second `betedge fetch` before game time.",
                    fg=typer.colors.YELLOW)
    if r["graded"] < 100:
        typer.secho(f"\nOnly {r['graded']} graded plays: treat these numbers as noise until there are a few hundred.",
                    fg=typer.colors.YELLOW)


@app.command()
def daily(
    sport: Annotated[str, typer.Option(help="nba, mlb, nfl, or all")] = "all",
    force: Annotated[bool, typer.Option(help="Allow spending past the quota budget, up to the plan's hard limit")] = False,
) -> None:
    """The whole routine: settle finished games, fetch odds + context, write the dashboard."""
    settle_cmd()
    fetch(sport=sport, day=None, include_live=False, force=force, no_context=False, replay=None)
    report(sport=sport, as_of=None, no_context=False)


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8765,
    host: Annotated[str, typer.Option(help="0.0.0.0 = reachable from your phone on the same Wi-Fi; 127.0.0.1 = this computer only")] = "0.0.0.0",
) -> None:
    """Serve the dashboard to your phone/computer over your home network (Ctrl+C to stop)."""
    from bookerbud.report.build import load_report_config, report_dir
    from bookerbud.report.serve import lan_addresses, make_server

    out_dir = report_dir(load_settings(), load_report_config())
    if not (out_dir / "latest.html").exists():
        typer.echo("No dashboard yet. Run `betedge report` first.")
        raise typer.Exit(1)
    server = make_server(out_dir, host, port)
    typer.echo(f"Serving {out_dir} (read-only). On this computer: http://localhost:{port}/")
    if host == "0.0.0.0":
        for ip in lan_addresses():
            typer.echo(f"  On your phone (same Wi-Fi): http://{ip}:{port}/")
        typer.secho("  Anyone on this Wi-Fi network can open it. Use --host 127.0.0.1 to keep it private.",
                    fg=typer.colors.YELLOW)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    app()
