"""Plain-English labels and the two-sentence reason for each play."""

from __future__ import annotations

from bookerbud.edge.engine import Edge
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
