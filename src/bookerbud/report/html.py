"""Responsive HTML dashboard for the daily report (phone + desktop, light + dark).

Server-rendered: every play is real markup, so the page reads fine with no
JavaScript. A small inline script adds sport/book/confidence/state filters, a
cards/table toggle, and live "x min ago" ages. No external scripts; Google
Fonts is the only network request, with system fallbacks for offline use.

standalone=True  -> full HTML document (what `betedge report` writes)
standalone=False -> page body only (for hosts that supply their own skeleton)
"""

from __future__ import annotations

import math
from html import escape
from zoneinfo import ZoneInfo

from bookerbud.report.build import Report

SPORT = {"nba": "NBA", "mlb": "MLB", "nfl": "NFL"}
CONF = {"high": "High", "medium": "Medium", "low": "Low"}

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700'
    '&family=IBM+Plex+Mono:wght@500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">'
)

CSS = r"""
/* Layout: a rack of betting-slip tickets under a sticky filter rail; summary board on top. */
:root {
  --bg: #eef2ef; --surface: #ffffff; --ink: #142019; --muted: #5a6a62; --line: #d2dbd5; --chip: #e2e9e4;
  --accent: #2346b5; --accent-ink: #ffffff;
  --good: #17784a; --good-soft: #dcf0e4; --warn: #965800; --warn-soft: #f9e9cc; --bad: #b3261e;
  --display: "Barlow Condensed", "Arial Narrow", "Roboto Condensed", system-ui, sans-serif;
  --body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d1411; --surface: #151e1a; --ink: #e2ebe6; --muted: #8ca096; --line: #27342e; --chip: #1d2823;
    --accent: #93a9ff; --accent-ink: #0d1411;
    --good: #5bcf8c; --good-soft: #163325; --warn: #e9b25a; --warn-soft: #33281a; --bad: #ff8a80;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0d1411; --surface: #151e1a; --ink: #e2ebe6; --muted: #8ca096; --line: #27342e; --chip: #1d2823;
  --accent: #93a9ff; --accent-ink: #0d1411;
  --good: #5bcf8c; --good-soft: #163325; --warn: #e9b25a; --warn-soft: #33281a; --bad: #ff8a80;
  color-scheme: dark;
}
body { background: var(--bg); color: var(--ink); font-family: var(--body); line-height: 1.45; }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--bg); }
.wrap { max-width: 1240px; margin: 0 auto; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
a { color: var(--accent); }
button { font: inherit; color: inherit; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.mono, .num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.eyebrow { font-size: .72rem; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }

/* Masthead */
.mast { padding-block: 20px 8px; display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline; justify-content: space-between; }
.mast h1 { font-family: var(--display); font-weight: 700; font-size: clamp(1.9rem, 6vw, 2.6rem); line-height: 1; margin: 0; letter-spacing: .01em; text-wrap: balance; }
.mast h1 span { color: var(--muted); font-weight: 600; }
.mast .sub { color: var(--muted); font-size: .85rem; }

/* Summary board */
.board { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--line); border-radius: 10px; background: var(--surface); margin-block: 8px 12px; }
.board > div { padding: 12px 14px; display: grid; gap: 2px; min-width: 0; }
.board > div + div { border-left: 1px solid var(--line); }
.board b { font-family: var(--display); font-size: 1.7rem; line-height: 1.05; font-weight: 700; font-variant-numeric: tabular-nums; }
.board small { color: var(--muted); font-size: .78rem; }
.board .pos { color: var(--good); }
@media (max-width: 640px) {
  .board { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .board > div:nth-child(3) { border-left: 0; }
  .board > div:nth-child(n+3) { border-top: 1px solid var(--line); }
}

/* Freshness + warnings */
.status { display: flex; flex-wrap: wrap; gap: 6px; margin-block: 0 10px; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px; font-size: .78rem; background: var(--chip); color: var(--ink); white-space: nowrap; }
.pill i { width: 7px; height: 7px; border-radius: 50%; background: var(--good); display: inline-block; }
.pill.stale i { background: var(--warn); }
.pill.none i { background: var(--muted); }
.alerts { border: 1px solid var(--line); border-radius: 10px; background: var(--surface); margin-block: 0 14px; }
.alerts summary { cursor: pointer; padding: 10px 14px; font-weight: 600; font-size: .9rem; list-style: none; display: flex; gap: 8px; align-items: center; }
.alerts summary::-webkit-details-marker { display: none; }
.alerts summary::after { content: "Show"; margin-left: auto; color: var(--accent); font-weight: 500; font-size: .82rem; }
.alerts[open] summary::after { content: "Hide"; }
.alerts ul { list-style: none; margin: 0; padding: 0 14px 12px; display: grid; gap: 8px; }
.alerts li { display: grid; grid-template-columns: auto 1fr; gap: 8px; font-size: .88rem; }
.alerts li::before { content: ""; width: 8px; height: 8px; border-radius: 50%; margin-top: .45em; background: var(--muted); }
.alerts li.warn::before { background: var(--warn); }
.count { font-family: var(--mono); font-size: .75rem; background: var(--warn-soft); color: var(--warn); padding: 1px 7px; border-radius: 999px; }

/* Filter rail */
.rail { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--bg); border-bottom: 1px solid var(--line); padding-block: 8px; }
.rail .row { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; padding-block: 2px; }
.rail .row::-webkit-scrollbar { display: none; }
.rail .row + .row { margin-top: 6px; }
.seg, .chip { border: 1px solid var(--line); background: var(--surface); border-radius: 8px; padding: 6px 12px; cursor: pointer; white-space: nowrap; font-size: .85rem; }
.seg { font-family: var(--display); font-weight: 700; font-size: 1.02rem; letter-spacing: .03em; padding: 3px 14px; }
.seg span, .chip span { color: var(--muted); font-family: var(--mono); font-size: .72rem; margin-left: 4px; }
.seg[aria-pressed="true"], .chip[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.seg[aria-pressed="true"] span, .chip[aria-pressed="true"] span { color: inherit; opacity: .8; }
.rail .sep { flex: 0 0 1px; background: var(--line); margin-inline: 2px; }
.rail .view { margin-left: auto; }

/* Sport sections */
.sport { padding-block: 18px 6px; }
.sport > header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; margin-bottom: 10px; }
.sport h2 { font-family: var(--display); font-weight: 700; font-size: 1.6rem; margin: 0; letter-spacing: .02em; }
.sport > header .meta { color: var(--muted); font-size: .82rem; }
.empty { color: var(--muted); font-size: .9rem; padding: 14px; border: 1px dashed var(--line); border-radius: 10px; }

/* Tickets */
.rack { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 330px), 1fr)); gap: 12px; }
.ticket { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; display: grid; gap: 10px; min-width: 0; }
.t-top { display: flex; justify-content: space-between; gap: 8px; font-size: .78rem; color: var(--muted); }
.t-top .match { font-weight: 600; color: var(--ink); }
.t-who h3 { margin: 0; font-size: 1.12rem; font-weight: 600; line-height: 1.2; }
.t-who p { margin: 2px 0 0; font-size: .95rem; }
.t-who .alt { font-size: .7rem; border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; color: var(--muted); margin-left: 6px; vertical-align: 2px; }
.t-book { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-size: .9rem; }
.t-book .price { font-family: var(--mono); font-size: 1.05rem; }
.scale { display: grid; gap: 4px; }
.track { position: relative; height: 8px; border-radius: 4px; background: var(--chip); }
.track .span { position: absolute; top: 0; bottom: 0; background: var(--good-soft); border-radius: 4px; }
.track .mk { position: absolute; top: -3px; width: 14px; height: 14px; margin-left: -7px; border-radius: 50%; border: 2px solid var(--muted); background: var(--surface); }
.track .mk.fair { border-color: var(--good); background: var(--good); }
.scale .ticks { display: flex; justify-content: space-between; font-family: var(--mono); font-size: .68rem; color: var(--muted); }
.scale .legend { display: flex; gap: 14px; font-size: .78rem; color: var(--muted); }
.scale .legend b { color: var(--ink); font-family: var(--mono); font-weight: 500; }
.perf { position: relative; border-top: 2px dashed var(--line); margin-inline: -16px; }
.perf::before, .perf::after { content: ""; position: absolute; top: -9px; width: 16px; height: 16px; border-radius: 50%; background: var(--bg); border: 1px solid var(--line); }
.perf::before { left: -9px; clip-path: inset(0 0 0 50%); }
.perf::after { right: -9px; clip-path: inset(0 50% 0 0); }
.t-foot { display: grid; grid-template-columns: 1fr auto auto; gap: 12px; align-items: end; }
.edge { font-family: var(--display); font-weight: 700; font-size: 2.1rem; line-height: .9; color: var(--good); font-variant-numeric: tabular-nums; }
.edge small, .stake small { display: block; font-family: var(--body); font-size: .7rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
.stake { font-family: var(--mono); font-size: 1rem; text-align: right; }
.stake em { display: block; font-style: normal; font-size: .72rem; color: var(--muted); }
.conf { font-size: .75rem; font-weight: 600; padding: 3px 9px; border-radius: 999px; border: 1px solid transparent; }
.conf.high { background: var(--good-soft); color: var(--good); }
.conf.medium { background: var(--warn-soft); color: var(--warn); }
.conf.low { border-color: var(--line); color: var(--muted); }
.reason { margin: 0; font-size: .86rem; color: var(--muted); }
.notes { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; padding: 0; list-style: none; }
.notes li { font-size: .75rem; padding: 2px 8px; border-radius: 6px; background: var(--warn-soft); color: var(--warn); }
.notes li.ok { background: var(--chip); color: var(--muted); }
.t-src { font-size: .75rem; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 10px; }

/* Table view */
.tablewrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: .86rem; }
th, td { padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: .7rem; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; white-space: nowrap; }
td .edgev { color: var(--good); font-weight: 600; }
tr:last-child td { border-bottom: 0; }
#app[data-view="cards"] .tablewrap, #app[data-view="table"] .rack { display: none; }
/* Phones always get cards; the table is a wide-screen view. */
@media (max-width: 760px) {
  [data-f="view"] { display: none; }
  #app[data-view="table"] .rack { display: grid; }
  #app[data-view="table"] .tablewrap { display: none; }
}

/* Slips */
.slip { gap: 8px; }
.slip ol { margin: 0; padding-left: 1.2em; display: grid; gap: 6px; font-size: .9rem; }
.slip ol small { display: block; color: var(--muted); font-size: .76rem; }
.badge { font-size: .72rem; padding: 2px 8px; border-radius: 6px; background: var(--warn-soft); color: var(--warn); font-weight: 600; }

/* Games */
.games { display: grid; gap: 8px; }
.games details { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
.games summary { cursor: pointer; padding: 10px 14px; display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.games summary .t { font-family: var(--mono); font-size: .8rem; color: var(--muted); min-width: 4.8em; }
.games ul { margin: 0; padding: 0 14px 12px 32px; font-size: .86rem; color: var(--muted); }

.banner { margin: 12px 0 0; padding: 10px 14px; border-radius: 10px; background: var(--warn-soft); color: var(--warn); font-weight: 600; font-size: .88rem; }
footer { color: var(--muted); font-size: .78rem; padding-block: 28px 32px; display: grid; gap: 4px; }
@media (prefers-reduced-motion: no-preference) { .seg, .chip { transition: background-color .12s, color .12s; } }
"""

JS = r"""
(function () {
  var app = document.getElementById('app');
  if (!app) return;
  var KEY = 'bookerbud.dashboard.v1';
  var st = { sport: 'all', book: '', conf: '', state: '', view: 'cards' };
  try { var saved = JSON.parse(localStorage.getItem(KEY) || '{}'); for (var k in saved) if (k in st) st[k] = saved[k]; } catch (e) {}
  function save() { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {} }
  function apply() {
    app.setAttribute('data-view', st.view);
    document.querySelectorAll('[data-play]').forEach(function (el) {
      var d = el.dataset;
      var ok = (st.sport === 'all' || d.sport === st.sport) && (!st.book || d.book === st.book) &&
               (!st.conf || d.conf === st.conf) && (!st.state || (' ' + d.states + ' ').indexOf(' ' + st.state + ' ') > -1);
      el.hidden = !ok;
    });
    document.querySelectorAll('section.sport[data-sport]').forEach(function (sec) {
      sec.hidden = !(st.sport === 'all' || sec.dataset.sport === st.sport);
      var any = sec.querySelector('.rack [data-play]:not([hidden])');
      var msg = sec.querySelector('.filtered-empty');
      if (msg) msg.hidden = !!any || !sec.querySelector('.rack [data-play]');
    });
    document.querySelectorAll('[data-f]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(st[b.dataset.f] === b.dataset.v));
    });
  }
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-f]');
    if (!b) return;
    var f = b.dataset.f, v = b.dataset.v;
    if (f === 'sport' || f === 'view') st[f] = v; else st[f] = (st[f] === v ? '' : v);
    save(); apply();
  });
  function ages() {
    document.querySelectorAll('[data-ts]').forEach(function (el) {
      var t = Date.parse(el.dataset.ts); if (isNaN(t)) return;
      var m = Math.max(0, Math.round((Date.now() - t) / 60000));
      el.textContent = m < 1 ? 'just now' : m < 90 ? m + ' min ago' : (m / 60).toFixed(1) + ' h ago';
    });
  }
  apply(); ages(); setInterval(ages, 60000);
})();
"""


def _e(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _age(m: float | None) -> str:
    if m is None:
        return "never"
    return "just now" if m < 1 else f"{m:.0f} min ago" if m < 90 else f"{m / 60:.1f} h ago"


def _scale(implied: float, fair: float) -> str:
    """Zoomed probability scale: both marks drawn on one linear axis whose end
    labels are the axis bounds (5-point steps), so the gap is visible."""
    lo = math.floor((min(implied, fair) * 100 - 3) / 5) * 5
    hi = math.ceil((max(implied, fair) * 100 + 3) / 5) * 5
    lo, hi = max(lo, 0), min(hi, 100)
    def pos(p: float) -> float:
        return (p * 100 - lo) / (hi - lo) * 100
    a, b = sorted((pos(implied), pos(fair)))
    return (
        '<div class="scale" aria-label="Fair vs implied probability">'
        f'<div class="track"><div class="span" style="left:{a:.1f}%;width:{b - a:.1f}%"></div>'
        f'<div class="mk" style="left:{pos(implied):.1f}%" title="Implied {_pct(implied)}"></div>'
        f'<div class="mk fair" style="left:{pos(fair):.1f}%" title="Fair {_pct(fair)}"></div></div>'
        f'<div class="ticks"><span>{lo}%</span><span>{hi}%</span></div>'
        f'<div class="legend"><span>Fair <b>{_pct(fair)}</b></span><span>Needs <b>{_pct(implied)}</b></span></div>'
        "</div>"
    )


def _attrs(p: dict) -> str:
    return (f'data-play data-sport="{_e(p["sport"])}" data-book="{_e(p["book"])}" '
            f'data-conf="{_e(p["confidence"])}" data-states="{_e(" ".join(p["states"]))}"')


def _ticket(p: dict) -> str:
    alt = '<span class="alt">alt line</span>' if p["alt"] else ""
    stake = (f'<div class="stake"><small>Stake</small>${p["stake"]:.2f}<em>{p["stake_pct"]:.1f}% of bankroll</em></div>'
             if p["kind"] != "pickem" else f'<div class="stake"><small>Stake</small>per slip<em>vs {_e(p["breakeven_slip"])}</em></div>')
    notes = "".join(f'<li class="{"ok" if n.startswith(("in posted", "status: probable")) else ""}">{_e(n)}</li>'
                    for n in p["notes"])
    src = f'Fair from {_e(p["source"])}'
    if p["line_gap"]:
        src += f' · line moved {p["line_gap"]:g}'
    states = ", ".join(p["states"]) or "not in your states"
    link = f' · <a href="{_e(p["deeplink"])}" rel="noopener" target="_blank">Open in {_e(p["book_label"])}</a>' if p.get("deeplink") else ""
    return f"""<article class="ticket" {_attrs(p)}>
<div class="t-top"><span class="match">{_e(p["matchup"])}</span><span class="mono">{_e(p["start_label"])}</span></div>
<div class="t-who"><h3>{_e(p["subject"])}</h3><p>{_e(p["market"])}{alt}</p></div>
<div class="t-book"><span>{_e(p["book_label"])}</span><span class="price">{_e(p["price"])}</span></div>
{_scale(p["implied"], p["fair"])}
<div class="perf" aria-hidden="true"></div>
<div class="t-foot"><div class="edge"><small>Edge</small>{p["edge_pct"]:+.1f}%</div>{stake}<span class="conf {p["confidence"]}">{CONF.get(p["confidence"], p["confidence"])}</span></div>
{f'<ul class="notes">{notes}</ul>' if notes else ''}
<p class="reason">{_e(p["reason"])}</p>
<div class="t-src"><span>{src}</span><span>Usable in {_e(states)}</span>{link}</div>
</article>"""


def _row(p: dict) -> str:
    stake = f'${p["stake"]:.2f}' if p["kind"] != "pickem" else "per slip"
    return (f'<tr {_attrs(p)}><td class="mono">{_e(p["start_label"])}</td>'
            f'<td><b>{_e(p["subject"])}</b><br>{_e(p["market"])}{" · alt" if p["alt"] else ""}<br><small>{_e(p["matchup"])}</small></td>'
            f'<td>{_e(p["book_label"])}<br><span class="mono">{_e(p["price"])}</span></td>'
            f'<td class="num mono">{_pct(p["fair"])}</td><td class="num mono">{_pct(p["implied"])}</td>'
            f'<td class="num mono"><span class="edgev">{p["edge_pct"]:+.1f}%</span></td>'
            f'<td class="num mono">{stake}</td><td><span class="conf {p["confidence"]}">{CONF.get(p["confidence"])}</span></td>'
            f'<td>{_e(p["source"])}{f" · moved {p["line_gap"]:g}" if p["line_gap"] else ""}</td>'
            f'<td>{_e("; ".join(p["notes"]))}</td></tr>')


def _slip(s: dict) -> str:
    legs = "".join(
        f'<li>{_e(l["subject"])}, {_e(l["market"])}<small>{_e(l["matchup"])} · fair {_pct(l["fair"])} · {_e(l["confidence"])} confidence</small></li>'
        for l in s["legs"]
    )
    pays = ", ".join(f"{k} hits {v:g}×" for k, v in s["payouts"].items())
    badge = "" if s["verified"] else '<span class="badge">Payout unverified, check in app</span>'
    return f"""<article class="ticket slip">
<div class="t-top"><span class="match">{_e(s["app_label"])} · {_e(s["kind"].title())} {s["size"]}-pick</span>{badge}</div>
<ol>{legs}</ol>
<div class="perf" aria-hidden="true"></div>
<div class="t-foot"><div class="edge"><small>EV</small>{s["ev_pct"]:+.1f}%</div><div class="stake"><small>Stake</small>${s["stake"]:.2f}<em>cashes {_pct(s["win_prob"])}</em></div><span class="conf low">{_e(pays)}</span></div>
</article>"""


def render_html(r: Report, *, standalone: bool = True, banner: str | None = None) -> str:
    tz = ZoneInfo(r.tz)
    as_of = r.as_of.astimezone(tz)
    gen = r.generated_at.astimezone(tz)
    all_plays = [p for ps in r.plays.values() for p in ps]
    t = r.totals
    best = f'+{t["best_edge"]:.1f}%' if t["best_edge"] is not None else "–"
    bankroll = r.settings["bankroll"]
    stake_pct = t["total_stake"] / bankroll * 100 if bankroll else 0
    fresh_ages = [f["age_min"] for f in r.freshness if f["age_min"] is not None]
    newest = min(fresh_ages) if fresh_ages else None
    newest_ts = max((f["fetched_at"] for f in r.freshness if f["fetched_at"]), default=None)

    pills = []
    for f in r.freshness:
        cls = "none" if f["fetched_at"] is None else "stale" if f["stale"] else ""
        age = f'<span data-ts="{_e(f["fetched_at"])}">{_age(f["age_min"])}</span>' if f["fetched_at"] else "no data"
        pills.append(f'<span class="pill {cls}"><i></i>{SPORT.get(f["sport"], f["sport"])} odds {age}</span>')
    q = r.quota
    pills.append(f'<span class="pill"><i></i>Quota {q["used"]:,}/{q["budget"]:,} · {q["window_days"]} days</span>')

    warns = r.warnings
    n_warn = sum(1 for w in warns if w["level"] == "warn")
    alerts = ""
    if warns:
        items = "".join(f'<li class="{_e(w["level"])}">{_e(w["text"])}</li>' for w in warns)
        label = f'<span class="count">{n_warn}</span> Warnings and notes' if n_warn else "Notes"
        alerts = f'<details class="alerts"{" open" if n_warn else ""}><summary>{label}</summary><ul>{items}</ul></details>'

    counts = {s: len(ps) for s, ps in r.plays.items()}
    books = sorted({(p["book"], p["book_label"]) for p in all_plays})
    segs = [f'<button class="seg" data-f="sport" data-v="all" aria-pressed="true">All<span>{len(all_plays)}</span></button>']
    segs += [f'<button class="seg" data-f="sport" data-v="{s}" aria-pressed="false">{SPORT.get(s, s)}<span>{counts.get(s, 0)}</span></button>'
             for s in r.sports]
    chips = [f'<button class="chip" data-f="book" data-v="{_e(b)}" aria-pressed="false">{_e(l)}</button>' for b, l in books]
    chips += ['<span class="sep"></span>'] + [
        f'<button class="chip" data-f="conf" data-v="{c}" aria-pressed="false">{CONF[c]}</button>' for c in ("high", "medium", "low")
    ]
    chips += ['<span class="sep"></span>'] + [
        f'<button class="chip" data-f="state" data-v="{_e(s)}" aria-pressed="false">{_e(s)}</button>' for s in r.settings["states"]
    ]
    views = ('<button class="chip view" data-f="view" data-v="cards" aria-pressed="true">Cards</button>'
             '<button class="chip" data-f="view" data-v="table" aria-pressed="false">Table</button>')

    sections = []
    for sport in r.sports:
        plays = r.plays.get(sport, [])
        fr = next((f for f in r.freshness if f["sport"] == sport), {})
        meta = f'{len(plays)} plays · {fr.get("events", 0)} games'
        if not plays:
            body = '<p class="empty">No plays pass the filters for this sport today.</p>'
        else:
            body = (f'<div class="rack">{"".join(_ticket(p) for p in plays)}</div>'
                    '<p class="empty filtered-empty" hidden>No plays match these filters.</p>'
                    '<div class="tablewrap"><table><thead><tr><th>Time</th><th>Play</th><th>Book</th>'
                    '<th class="num">Fair</th><th class="num">Needs</th><th class="num">Edge</th><th class="num">Stake</th>'
                    f'<th>Conf.</th><th>Fair from</th><th>Notes</th></tr></thead><tbody>{"".join(_row(p) for p in plays)}</tbody></table></div>')
        sections.append(f'<section class="sport" data-sport="{_e(sport)}"><header><h2>{SPORT.get(sport, sport)}</h2>'
                        f'<span class="meta">{meta}</span></header>{body}</section>')

    slips = ("".join(_slip(s) for s in r.slips) if r.slips else "")
    slip_section = (
        '<section class="sport"><header><h2>Pick\'em slips</h2><span class="meta">Best 2–3 leg entries · same-game legs excluded</span></header>'
        + (f'<div class="rack">{slips}</div>' if slips else
           '<p class="empty">No +EV slips today. PrizePicks, Underdog and Sleeper lines aren\'t on the free data plan, '
           'so this stays empty unless pick\'em lines are added.</p>')
        + "</section>"
    )

    games = "".join(
        f'<details><summary><span class="t">{_e(g["start_label"])}</span><b>{_e(g["matchup"])}</b>'
        f'<span class="eyebrow">{SPORT.get(g["sport"], g["sport"])}</span></summary>'
        + (f'<ul>{"".join(f"<li>{_e(x)}</li>" for x in g["lines"])}</ul>' if g["lines"] else "<ul><li>No context yet.</li></ul>")
        + "</details>"
        for g in r.games
    )
    games_section = (f'<section class="sport"><header><h2>Games</h2><span class="meta">Starters, lineups, weather and rest</span>'
                     f'</header><div class="games">{games}</div></section>') if games else ""

    s = r.settings
    body = f"""<div id="app" data-view="cards">
<div class="wrap">
{f'<p class="banner">{_e(banner)}</p>' if banner else ""}
<header class="mast"><h1>BookerBud <span>{as_of.strftime("%a %b")} {as_of.day}</span></h1>
<span class="sub">Research only. Nothing here places a bet.</span></header>
<div class="board">
<div><span class="eyebrow">Plays</span><b>{t["plays"]}</b><small>edge ≥ {s["min_edge_pct"]:g}%</small></div>
<div><span class="eyebrow">Best edge</span><b class="pos">{best}</b><small>expected return</small></div>
<div><span class="eyebrow">Suggested stake</span><b>${t["total_stake"]:,.0f}</b><small>{stake_pct:.1f}% of ${bankroll:,.0f}</small></div>
<div><span class="eyebrow">Odds updated</span><b {f'data-ts="{_e(newest_ts)}"' if newest_ts else ""}>{_age(newest)}</b><small>hidden after {s["max_line_age"]:.0f} min</small></div>
</div>
<div class="status">{"".join(pills)}</div>
{alerts}
</div>
<nav class="rail" aria-label="Filters"><div class="wrap"><div class="row">{"".join(segs)}{views}</div><div class="row">{"".join(chips)}</div></div></nav>
<main class="wrap">
{"".join(sections)}
{slip_section}
{games_section}
</main>
<footer class="wrap">
<span>Stakes are {s["kelly_fraction"]:g}× Kelly on a ${bankroll:,.0f} bankroll, capped at {s["max_bet_pct"]:g}% per bet. States: {_e(", ".join(s["states"]))}. Data plan: {_e(s["plan"])}.</span>
<span>Generated {gen.strftime("%I:%M %p").lstrip("0")} {_e(gen.tzname())} from the latest snapshot. Weather data by Open-Meteo.com (CC BY 4.0). MLB data © MLB Advanced Media, individual non-commercial use.</span>
</footer>
</div>"""

    title = f"<title>BookerBud {as_of.strftime('%b')} {as_of.day}</title>"
    if not standalone:
        return (f'{title}\n<meta name="claude:viewport-fit-sides" content="cover">\n{FONTS}\n'
                f"<style>{CSS}</style>\n{body}\n<script>{JS}</script>\n")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
{title}
{FONTS}
<style>:root {{ padding-top: env(safe-area-inset-top, 0px); padding-bottom: env(safe-area-inset-bottom, 0px); }} body {{ margin: 0; font-size: 14px; }} [hidden] {{ display: none !important; }} img {{ max-width: 100%; }}
{CSS}</style>
</head>
<body>
{body}
<script>{JS}</script>
</body>
</html>
"""
