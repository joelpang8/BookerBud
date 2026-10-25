"""Plain-English labels and the two-sentence reason for each play."""

from __future__ import annotations

from bookerbud.edge.engine import Edge
from bookerbud.edge.odds import american_to_decimal
from bookerbud.normalize.stats import StatMatcher

BOOKS = {
    "draftkings": "DraftKings", "fanduel": "FanDuel", "prizepicks": "PrizePicks", "underdog": "Underdog",
    "sleeper": "Sleeper", "pinnacle": "Pinnacle", "betmgm": "BetMGM", "caesars": "Caesars", "espnbet": "ESPN BET",
    "bovada": "Bovada", "unibet": "Unibet", "pointsbet": "PointsBet", "williamhill": "William Hill",
}
PERIODS = {"1h": "1st half", "2h": "2nd half", "1q": "1st quarter", "2q": "2nd quarter", "3q": "3rd quarter",
           "4q": "4th quarter", "1p": "1st period", "reg": "regulation"}


def book(b: str) -> str:
    return BOOKS.get(b, b.title())


def american(v: int | None) -> str:
    return "" if v is None else (f"+{v}" if v > 0 else f"−{abs(v)}")


def line_str(x: float | None, signed: bool = False) -> str:
    if x is None:
        return ""
    s = f"{x:g}"
    if signed and x > 0:
        s = "+" + s
    return s.replace("-", "−")


def stat_label(e: Edge) -> str:
    return StatMatcher(e.sport).display(e.stat_type)


def market(e: Edge) -> str:
    """'Over 24.5 Points', 'Moneyline', 'Spread −4.5', 'Over 229.5 total'."""
    per = f" ({PERIODS.get(e.period, e.period)})" if e.period != "game" else ""
    if e.bet_type == "ml":
        return f"Moneyline{per}"
    if e.bet_type == "sp":
        return f"Spread {line_str(e.line, signed=True)}{per}"
    if e.bet_type == "yn":
        return f"{stat_label(e)}: {e.side.title()}{per}"
    if e.market_kind == "player_prop":
        return f"{e.side.title()} {line_str(e.line)} {stat_label(e)}{per}"
    if e.stat_type not in ("points", ""):  # e.g. total touchdowns, not the score
        name = e.stat_type.replace("_", " ")
        what = f"{name} ({e.team})" if e.team else f"total {name}"
    else:
        what = "team total" if e.team else "game total"
    return f"{e.side.title()} {line_str(e.line)} {what}{per}"


def subject(e: Edge) -> str:
    if e.player:
        return e.player
    if e.team:
        return e.team
    return "Game total"


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fair_phrase(e: Edge) -> str:
    moved = e.line_gap and e.fair_line is not None
    move = f", moved from {line_str(e.fair_line)} to {line_str(e.line)}" if moved else ""
    if e.fair_source == "consensus":
        names = ", ".join(book(b) for b in e.fair_books)
        return f"the no-vig average of {len(e.fair_books)} other books ({names}){move}"
    return f"{book(e.fair_source)}'s no-vig price{move}"


def _why_confidence(e: Edge) -> str:
    if e.confidence == "high":
        return "high confidence (sharp price at the same line)"
    if e.fair_source == "consensus":
        return "low confidence (no Pinnacle price, so the fair line is a consensus)"
    if e.line_gap:
        return f"medium confidence (the line had to be moved {line_str(e.line_gap)})"
    return f"{e.confidence} confidence"


def reason(e: Edge) -> str:
    """Exactly two sentences."""
    if e.payout_type == "pickem":
        first = (
            f"{book(e.bookmaker)} pays a fixed multiplier, so the bar is the {pct(e.implied_prob)} per-pick break-even "
            f"of its {e.breakeven_slip.replace('-', ' ')}-pick entry, and {_fair_phrase(e)} makes this pick "
            f"{pct(e.fair_prob)} to hit."
        )
    else:
        first = (
            f"{book(e.bookmaker)} is paying {american(e.price_american)}, which needs {pct(e.implied_prob)} to break even, "
            f"while {_fair_phrase(e)} puts it at {pct(e.fair_prob)}."
        )
    second = f"That is a {e.ev_pct:+.1f}% edge at {_why_confidence(e)}"
    notes = [n for n in e.context_notes if not n.startswith("in posted lineup")]
    if notes:
        second += "; " + "; ".join(notes)
    return f"{first} {second}."


def _spread_pts(probs: list[float]) -> float:
    return (max(probs) - min(probs)) * 100 if len(probs) > 1 else 0.0


def key_driver(e: Edge) -> str:
    """One line: the main reason this play shows up."""
    same = [m for m in e.market_prices if m["line"] == e.line and m["book"] != e.bookmaker]
    if e.payout_type != "pickem" and same and e.price_american is not None:
        others = sorted({american(m["price"]) for m in same}, key=lambda x: x)
        rng = others[0] if len(others) == 1 else f"{others[0]} to {others[-1]}"
        if all(american_to_decimal(e.price_american) > american_to_decimal(m["price"]) for m in same):
            return f"{book(e.bookmaker)} is the outlier: {american(e.price_american)} vs {rng} elsewhere"
    if e.line_gap and e.fair_line is not None:
        return f"Softer line: {line_str(e.line)} here vs {line_str(e.fair_line)} at the market"
    return f"Fair {pct(e.fair_prob)} vs {pct(e.implied_prob)} needed"


def why(e: Edge, form: dict | None) -> list[dict]:
    """Labelled reasoning bullets for the full breakdown."""
    out: list[dict] = []
    if e.payout_type == "pickem":
        out.append({"label": "Price", "text": (
            f"{book(e.bookmaker)} pays a fixed multiplier; its best entry ({(e.breakeven_slip or '').replace('-', ' ')}-pick) "
            f"needs each pick to hit {pct(e.implied_prob)} of the time.")})
    else:
        out.append({"label": "Price", "text": (
            f"{book(e.bookmaker)} pays {american(e.price_american)} on {market(e)}, which needs {pct(e.implied_prob)} to break even.")})

    probs = list(e.book_fair.values())
    if e.fair_source == "consensus":
        detail = ", ".join(f"{book(b)} {pct(p)}" for b, p in sorted(e.book_fair.items(), key=lambda kv: -kv[1]))
        spread = _spread_pts(probs)
        agree = (f"they agree within {spread:.1f} points" if spread <= 3 else
                 f"they disagree by {spread:.1f} points, so treat the fair value as rough")
        out.append({"label": "Fair value", "text": (
            f"Removing the margin from {len(probs)} other books and averaging gives {pct(e.fair_prob)} "
            f"({detail}); {agree}.")})
    else:
        out.append({"label": "Fair value", "text": f"{book(e.fair_source)}'s no-vig price puts it at {pct(e.fair_prob)}."})

    same = [m for m in e.market_prices if m["line"] == e.line and m["book"] != e.bookmaker]
    if e.payout_type != "pickem":
        if same:
            others = ", ".join(f"{book(m['book'])} {american(m['price'])}" for m in same[:5])
            best = all(american_to_decimal(e.price_american) >= american_to_decimal(m["price"]) for m in same)
            out.append({"label": "Line shopping", "text": (
                f"{'Best price at this line' if best else 'Not the best price at this line'}: "
                f"{book(e.bookmaker)} {american(e.price_american)} vs {others}.")})
        else:
            others_main = [m for m in e.market_prices if m["book"] != e.bookmaker][:4]
            mains = ", ".join(f"{book(m['book'])} {line_str(m['line'])} at {american(m['price'])}" for m in others_main)
            out.append({"label": "Line shopping", "text": "No other book has this exact line" + (f" (others: {mains})." if mains else ".")})

    if e.line_gap and e.fair_line is not None:
        out.append({"label": "Line", "text": (
            f"{'Alternate line. ' if e.is_alt_line else ''}The market's line is {line_str(e.fair_line)}; the fair chance at "
            f"{line_str(e.line)} was estimated from it with a stat-distribution model, so it carries more error.")})
    elif e.is_alt_line:
        out.append({"label": "Line", "text": "Alternate line at a number the market also prices directly."})

    if form:
        side = {"over": "Over", "under": "Under", "yes": "Yes", "no": "No"}.get(form["side"], "Covered")
        thr = line_str(form["threshold"])
        what = {"margin": "margin", "game total": "game total", "team points": "points"}.get(form["what"], stat_label(e))
        out.append({"label": "Recent form", "text": (
            f"{side} {thr} hit in {form['hits']} of the last {form['n']} games (average {what} {form['avg']:.1f}). "
            "Context only: the edge comes from prices, not recent results.")})
    else:
        out.append({"label": "Recent form", "text": "No game log stored yet for this market (run `betedge history` once to backfill)."})

    if e.context_notes:
        out.append({"label": "Context", "text": "; ".join(e.context_notes).capitalize() + "."})

    risks = []
    if e.fair_source == "consensus":
        risks.append("no Pinnacle price on the free plan, so the fair line is a consensus")
    if len(probs) <= 2 and e.fair_source == "consensus":
        risks.append(f"thin market (only {len(probs)} book{'s' if len(probs) != 1 else ''} to compare)")
    if e.line_gap:
        risks.append(f"line moved {line_str(e.line_gap)}")
    if e.ev_pct >= 10:
        risks.append("edges this large are often a stale or mistaken price, so re-check it in the app first")
    out.append({"label": "Risk", "text": (f"{e.confidence.title()} confidence: " + "; ".join(risks) + ".") if risks
                else f"{e.confidence.title()} confidence: sharp price at the same line, nothing unusual."})
    return out
