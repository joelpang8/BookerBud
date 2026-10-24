"""Markdown version of the daily report."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bookerbud.report.build import Report

SPORT = {"nba": "NBA", "mlb": "MLB", "nfl": "NFL"}


def _cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _age(m: float | None) -> str:
    if m is None:
        return "never"
    return f"{m:.0f} min ago" if m < 90 else f"{m / 60:.1f} h ago"


def render_markdown(r: Report) -> str:
    tz = ZoneInfo(r.tz)
    as_of = r.as_of.astimezone(tz)
    gen = r.generated_at.astimezone(tz)
    out = [f"# BookerBud — {as_of:%A %B} {as_of.day}, {as_of.year}", ""]
    out.append(f"_Research only. Nothing here places bets. Generated {gen.strftime('%I:%M %p').lstrip('0')} {gen.tzname()}._")
    out.append("")
    out.append("## Data freshness")
    for f in r.freshness:
        state = "stale" if f["stale"] else "fresh"
        out.append(f"- **{SPORT.get(f['sport'], f['sport'])}**: odds {_age(f['age_min'])} ({state}), {f['events']} games")
    q = r.quota
    out.append(f"- **Quota**: {q['used']} / {q['budget']} objects over {q['window_days']} days (plan limit {q['limit']})")
    out.append("")
    if r.warnings:
        out.append("## Warnings")
        out += [f"- {'⚠️ ' if w['level'] == 'warn' else ''}{w['text']}" for w in r.warnings]
        out.append("")
    s = r.settings
    out.append(f"Stakes: {s['kelly_fraction']:g}× Kelly on a ${s['bankroll']:,.0f} bankroll, max {s['max_bet_pct']:g}% per bet. "
               f"Minimum edge {s['min_edge_pct']:g}%. States: {', '.join(s['states'])}. "
               f"Injury filter: {s.get('injury_filter', '')}.")
    out.append("")
    for sport, plays in r.plays.items():
        out.append(f"## {SPORT.get(sport, sport)} — top plays")
        if not plays:
            out.append("_No plays pass the filters._")
            out.append("")
            continue
        out.append("| Play | Book · price | Fair | Implied | Edge | Stake | Confidence |")
        out.append("|---|---|---|---|---|---|---|")
        for p in plays:
            stake = f"${p['stake']:.2f}" if p["kind"] != "pickem" else "per slip"
            out.append("| " + " | ".join(_cell(x) for x in [
                f"**{p['subject']}** {p['market']}" + (" (alt)" if p["alt"] else "") + f"<br>{p['matchup']} {p['start_label']}",
                f"{p['book_label']} {p['price']}",
                f"{p['fair'] * 100:.1f}%", f"{p['implied'] * 100:.1f}%", f"{p['edge_pct']:+.1f}%", stake,
                p["confidence"],
            ]) + " |")
        out.append("")
        for p in plays:
            out.append(f"- **{p['subject']} {p['market']} ({p['book_label']})**: {p['reason']}")
        out.append("")
    out.append("## Best pick'em slips (2–3 legs)")
    if not r.slips:
        out.append("_No +EV slips today._")
    for sl in r.slips:
        flag = "" if sl["verified"] else " ⚠️ payout table unverified"
        out.append(f"### {sl['app_label']} {sl['kind']} {sl['size']}-pick: EV {sl['ev_pct']:+.1f}%, "
                   f"cash {sl['win_prob'] * 100:.0f}%, stake ${sl['stake']:.2f}{flag}")
        out += [f"- {l['subject']} {l['market']} ({l['matchup']}): fair {l['fair'] * 100:.1f}%" for l in sl["legs"]]
        out.append("")
    if r.games:
        out.append("## Games")
        for g in r.games:
            extra = f": {'; '.join(g['lines'])}" if g["lines"] else ""
            out.append(f"- {SPORT.get(g['sport'], g['sport'])} {g['start_label']} {g['matchup']}{extra}")
        out.append("")
    perf = r.performance
    if perf:
        rc, b = perf["recs"], perf["bets"]

        def pc(x, signed=False, ratio=True):
            if x is None:
                return "–"
            v = x * 100 if ratio else x
            return f"{v:+.1f}%" if signed else f"{v:.1f}%"

        out.append("## Performance")
        out.append(f"- **Recommendations**: {rc['n']} plays, {rc['graded']} graded ({perf['pending']} pending), "
                   f"hit {pc(rc['hit_rate'])}, CLV {pc(rc['clv'], True, False)}, beat close {pc(rc['beat_close'])}, "
                   f"flat ROI {pc(rc['flat_roi'], True)}")
        if b["n"]:
            out.append(f"- **Your bets**: {b['n']} bets, profit ${b['profit']:+,.2f} on ${b['staked']:,.2f}, "
                       f"ROI {pc(b['roi'], True)}, CLV {pc(b['clv'], True, False)}, beat close {pc(b['beat_close'])}")
        else:
            out.append("- **Your bets**: none logged yet")
        if perf["edge_buckets"]:
            out.append("")
            out.append("| Predicted edge | Plays | Pred. win | Actual win | CLV | Flat ROI |")
            out.append("|---|---|---|---|---|---|")
            out += [f"| {x['bucket']} | {x['n']} | {pc(x['predicted'])} | {pc(x['hit_rate'])} | "
                    f"{pc(x['clv'], True, False)} | {pc(x['flat_roi'], True)} |" for x in perf["edge_buckets"]]
        out.append("")
    if r.skipped:
        out.append("<details><summary>Filtered out</summary>\n")
        out += [f"- {k}: {v}" for k, v in sorted(r.skipped.items(), key=lambda kv: -kv[1])]
        out.append("\n</details>")
    out.append("")
    out.append("_Weather data by Open-Meteo.com (CC BY 4.0). MLB data © MLB Advanced Media, individual non-commercial use._")
    return "\n".join(out) + "\n"
