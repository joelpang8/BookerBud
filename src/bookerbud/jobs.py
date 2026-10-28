"""Multi-step jobs shared by the CLI and the dashboard's Refresh button."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bookerbud.config import Settings
from bookerbud.models import Sport
from bookerbud.pipeline import fetch_and_store, quota_status, refresh_context
from bookerbud.providers import get_provider
from bookerbud.storage import connect


def mlb_context(settings: Settings):
    from bookerbud.context.http import JsonClient
    from bookerbud.context.mlb import MLBContext
    from bookerbud.context.weather import OpenMeteo
    from bookerbud.raw_log import RawLogger

    raw = RawLogger(settings.raw_dir)
    return MLBContext(JsonClient("mlb-statsapi", raw), OpenMeteo(JsonClient("open-meteo", raw)))


def write_report(settings: Settings, sports: list[Sport], *, now: datetime, use_context: bool = True):
    """Build the report, record its recommendations, and write html/md/json + latest.html.
    Returns (report, path to the dated html)."""
    from bookerbud.report.build import build_report, load_report_config, report_dir
    from bookerbud.report.html import render_html
    from bookerbud.report.markdown import render_markdown

    rcfg = load_report_config()
    out_dir = report_dir(settings, rcfg)
    conn = connect(settings.db_file)
    try:
        r = build_report(conn, settings, sports, now=now, generated_at=datetime.now(UTC), rcfg=rcfg,
                         use_context=use_context)
        stem = now.astimezone(settings.tz).strftime("%Y-%m-%d")  # build_report recorded the recommendations
    finally:
        conn.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_doc = render_html(r)
    path = out_dir / f"{stem}.html"
    path.write_text(html_doc)
    (out_dir / "latest.html").write_text(html_doc)
    (out_dir / f"{stem}.md").write_text(render_markdown(r))
    (out_dir / f"{stem}.json").write_text(r.to_json())
    return r, path


SETTLE_EVERY_S = 30 * 60
_last_settle: dict[str, float] = {}


def settle_finished(conn, settings: Settings, provider, *, now: datetime, warnings: list[str]) -> dict:
    """Grade finished games (results for picks and bets), at most every 30 minutes
    so frequent refreshes don't spend quota or burn a game's settle attempts while
    it is still being played."""
    from bookerbud.tracking.settle import settle

    last = _last_settle.get(str(settings.db_file))
    if last is not None and now.timestamp() - last < SETTLE_EVERY_S:
        return {}
    _last_settle[str(settings.db_file)] = now.timestamp()
    try:
        s = settle(conn, settings, provider, now=now)
    except Exception as exc:  # grading is a side job; never lose the odds over it
        warnings.append(f"grading results failed: {exc}")
        return {}
    warnings += s.warnings
    return dict(s.settled)


def refresh_odds(settings: Settings, sports: list[Sport] | None = None) -> dict:
    """What the dashboard's Refresh button runs: grade finished games, fetch odds
    (quota-guarded), refresh injuries/lineups/weather/rest, rebuild the dashboard."""
    sports = sports or [Sport(s) for s in settings.sports if s in {x.value for x in Sport}]
    now = datetime.now(UTC)
    day = now.astimezone(settings.tz).date()
    provider = get_provider(settings)
    conn = connect(settings.db_file)
    mlb = mlb_context(settings) if Sport.MLB in sports else None
    warnings: list[str] = []
    try:
        graded = settle_finished(conn, settings, provider, now=now, warnings=warnings)
        summary = fetch_and_store(conn, settings, provider, sports, day)
        try:
            ctx = refresh_context(conn, settings, provider, sports, day, mlb=mlb)
            warnings += ctx.warnings or []
        except Exception as exc:  # context is advisory; never lose the odds over it
            warnings.append(f"context refresh failed: {exc}")
        q = quota_status(conn, settings)
    finally:
        provider.close()
        conn.close()
        if mlb:
            mlb.client.close()
            mlb.weather.client.close()
    report, _ = write_report(settings, sports, now=datetime.now(UTC))
    return {
        "events": len(summary.result.events),
        "objects_used": summary.result.objects_used,
        "plays": report.totals["plays"],
        "quota": f"{q.used}/{q.budget}",
        "graded": graded,
        "warnings": warnings,
    }


def report_path(settings: Settings) -> Path:
    from bookerbud.report.build import load_report_config, report_dir

    return report_dir(settings, load_report_config())


def log_bet_action(settings: Settings, payload: dict) -> tuple[int, dict]:
    """The dashboard's Mark placed / Log a bet / Edit / Delete (payload "action":
    add (default), edit or delete with "id"). Saves the change, rebuilds the page
    from stored data (no API calls) and returns (HTTP code, body)."""
    from bookerbud.tracking.logbets import (
        BetError,
        bet_from_market,
        bet_from_rec,
        describe,
        edit,
        place,
        remove,
    )

    def num(key, cast):
        v = payload.get(key)
        if v is None or str(v).strip() == "":
            return None
        try:
            return cast(str(v).strip().replace("−", "-").replace("$", ""))
        except ValueError:
            raise BetError(f"{key.title()} must be a number, got {v!r}.") from None

    conn = connect(settings.db_file)
    try:
        stake, price, line = num("stake", float), num("price", lambda x: int(float(x))), num("line", float)
        action = payload.get("action") or "add"
        if action in ("edit", "delete"):
            bet_id = num("id", int)
            if bet_id is None:
                raise BetError("Which bet? (missing id)")
            if action == "delete":
                bet, text = remove(conn, bet_id), "Deleted: "
            else:
                bet, text = edit(conn, bet_id, stake=stake, price=price, line=line,
                                 side=payload.get("side") or None, book=payload.get("book") or None), "Updated: "
        elif action != "add":
            raise BetError(f"Unknown action {action!r}.")
        elif payload.get("rec_id"):
            bet = bet_from_rec(conn, int(payload["rec_id"]), line=line, price=price)
        else:
            bet = bet_from_market(conn, player=(payload.get("player") or "").strip() or None, team=None,
                                  stat=payload.get("stat") or None, side=payload.get("side") or None, bet_type=None,
                                  book=payload.get("book") or None, line=line, price=price)
        if action == "add":
            bet_id, bet = place(conn, bet, stake=stake, note=payload.get("note"))
            text = ""
    except BetError as exc:
        return 400, {"error": str(exc)}
    finally:
        conn.close()
    sports = [Sport(s) for s in settings.sports if s in {x.value for x in Sport}]
    write_report(settings, sports, now=datetime.now(UTC))
    return 200, {"ok": True, "id": bet_id, "text": text + describe(bet)}
