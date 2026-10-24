"""Find +EV plays: fair probability per prop, EV vs. the offered price/payout,
Kelly stake, and the configured filters.

Fair probability, in order of preference:
  1. sharp book (Pinnacle) two-sided price -> devig -> move to the target line
     (confidence: high if same line, medium if moved)
  2. consensus: every other priced book's two-sided pair (target book and
     pick'em apps excluded), each de-vigged and moved to the target line, then
     averaged (confidence: low)

Every market is reduced to "over/under of a threshold" so one code path handles
totals, props and spreads:
  ou  over L / under L         -> threshold L
  sp  home at L (e.g. -4.5)    -> home margin over -L;  away at L -> under L
  ml / yn                      -> no threshold (exact pairing only)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean

from bookerbud.edge.config import EdgeConfig, SlipTable
from bookerbud.edge.devig import devig
from bookerbud.edge.ev import breakeven_leg_prob, sportsbook_ev
from bookerbud.edge.kelly import kelly_binary, stake
from bookerbud.edge.lines import Outcome, StatModel, adjust
from bookerbud.edge.odds import american_to_decimal, implied_prob
from bookerbud.models import MarketKind, PayoutType, Prop
from bookerbud.normalize import group_markets

OVER_SIDES = {"over", "home", "yes"}


@dataclass
class Pair:
    """One book's two-sided price at one threshold, de-vigged."""

    book: str
    threshold: float | None
    p_over: float  # conditional on no push
    is_main: bool


@dataclass
class Fair:
    outcome: Outcome  # in over/under terms at the target threshold
    source: str  # "pinnacle" | "consensus"
    books: list[str]
    source_threshold: float | None
    confidence: str  # high | medium | low


@dataclass
class Edge:
    sport: str
    event_id: str
    start_time: datetime
    market_kind: str
    player: str | None
    team: str | None
    opponent: str | None
    stat_type: str
    period: str
    bet_type: str
    side: str
    line: float | None
    bookmaker: str
    payout_type: str
    price_american: int | None
    implied_prob: float  # vigged break-even (sportsbook) or per-leg break-even (pick'em)
    fair_prob: float  # P(win)
    push_prob: float
    fair_source: str
    fair_books: list[str]
    fair_line: float | None  # source line in the bet's own terms
    line_gap: float
    confidence: str
    ev_pct: float  # sportsbook: EV % of stake; pick'em: leg edge % vs. break-even
    kelly_full: float
    stake: float
    breakeven_slip: str | None
    usable_in: list[str]
    player_status: str | None
    fetched_at: datetime
    deeplink: str | None = None
    flags: list[str] = field(default_factory=list)
    context_notes: list[str] = field(default_factory=list)  # Phase 3: injuries, lineups, weather, rest
    is_alt_line: bool = False

    @property
    def edge_pp(self) -> float:
        """Fair minus implied, in percentage points."""
        return (self.fair_prob - self.implied_prob) * 100


@dataclass
class EdgeResult:
    edges: list[Edge]
    pickem_legs: list[Edge]  # every priced pick'em leg (for slips), before min-edge filter
    skipped: Counter


def to_threshold(bet_type: str, side: str, line: float | None) -> tuple[bool, float | None]:
    is_over = side in OVER_SIDES
    if bet_type == "sp" and line is not None:
        return is_over, -line if side == "home" else line
    if bet_type in ("ml", "yn"):
        return is_over, None
    return is_over, line


def from_threshold(bet_type: str, side: str, threshold: float | None) -> float | None:
    if bet_type == "sp" and threshold is not None and side == "home":
        return -threshold
    return threshold


def model_key(p: Prop) -> str:
    if p.market_kind is MarketKind.PLAYER_PROP:
        return p.stat_type
    if p.bet_type == "sp":
        return "game_spread"
    return "game_total" if p.entity == "all" else "team_total"


def book_pairs(rows: list[Prop], method: str) -> dict[str, list[Pair]]:
    """De-vigged two-sided pairs per (priced) book."""
    sides: dict[tuple[str, float | None, bool], Prop] = {}
    for r in rows:
        if r.payout_type is PayoutType.PICKEM or r.price_american is None or not r.available:
            continue
        is_over, thr = to_threshold(r.bet_type, r.side, r.line)
        key = (r.bookmaker, None if thr is None else round(thr, 3), is_over)
        if key not in sides or (r.is_main_line and not sides[key].is_main_line):
            sides[key] = r
    out: dict[str, list[Pair]] = defaultdict(list)
    for (book, thr, is_over), over_row in sides.items():
        if not is_over or (book, thr, False) not in sides:
            continue
        under_row = sides[(book, thr, False)]
        try:
            p_over, _ = devig([over_row.price_american, under_row.price_american], method)
        except ValueError:
            continue
        main = bool(over_row.is_main_line or under_row.is_main_line or not over_row.is_alt_line)
        out[book].append(Pair(book, thr, p_over, main))
    return out


def _closest(pairs: list[Pair], target: float | None) -> Pair | None:
    if not pairs:
        return None
    if target is None:
        exact = [p for p in pairs if p.threshold is None]
        return exact[0] if exact else None
    usable = [p for p in pairs if p.threshold is not None]
    if not usable:
        return None
    return min(usable, key=lambda p: (abs(p.threshold - target), not p.is_main))


def _move(pair: Pair, target: float | None, model: StatModel) -> Outcome | None:
    if target is None:
        return Outcome(pair.p_over, 0.0, 1 - pair.p_over)
    return adjust(model, pair.threshold, pair.p_over, target)


def fair_for(
    target_book: str,
    target_threshold: float | None,
    pairs: dict[str, list[Pair]],
    sharp_books: list[str],
    model: StatModel,
    cfg: EdgeConfig,
) -> tuple[Fair | None, str | None]:
    """(fair, skip_reason)."""
    gap_hit = False
    for sb in sharp_books:
        pair = _closest(pairs.get(sb, []), target_threshold)
        if pair is None:
            continue
        moved = _move(pair, target_threshold, model)
        if moved is None:
            gap_hit = True
            continue
        exact = target_threshold is None or abs(pair.threshold - target_threshold) < 1e-9
        return Fair(moved, sb, [sb], pair.threshold, "high" if exact else "medium"), None
    if cfg.fair.require_sharp:
        return None, "line_gap_too_large" if gap_hit else "no_sharp_price"

    outcomes, books, thresholds = [], [], []
    for book, bp in pairs.items():
        if book == target_book or book in sharp_books:
            continue
        pair = _closest(bp, target_threshold)
        if pair is None:
            continue
        moved = _move(pair, target_threshold, model)
        if moved is None:
            gap_hit = True
            continue
        outcomes.append(moved)
        books.append(book)
        thresholds.append(pair.threshold)
    if len(outcomes) < cfg.fair.min_consensus_books:
        return None, "line_gap_too_large" if gap_hit and outcomes == [] else "not_enough_fair_books"
    avg = Outcome(fmean(o.over for o in outcomes), fmean(o.push for o in outcomes), fmean(o.under for o in outcomes))
    src_thr = None if target_threshold is None else min(thresholds, key=lambda t: abs(t - target_threshold))
    return Fair(avg, "consensus", sorted(books), src_thr, "low"), None


def pickem_breakeven(table: SlipTable | None) -> tuple[float, str] | None:
    """Lowest per-leg break-even across the app's slip types (its most generous structure)."""
    if table is None or not table.slips:
        return None
    best = None
    for kind, sizes in table.slips.items():
        for n, payouts in sizes.items():
            be = breakeven_leg_prob(int(n), payouts)
            if best is None or be < best[0]:
                best = (be, f"{kind}-{n}")
    return best


def find_edges(
    props: list[Prop],
    cfg: EdgeConfig,
    *,
    now: datetime,
    sharp_books: list[str],
    states: list[str],
    availability: dict[str, dict[str, str | None]],
    payouts: dict[str, SlipTable],
    all_books: bool = False,
) -> EdgeResult:
    skipped: Counter = Counter()
    edges: list[Edge] = []
    legs: list[Edge] = []
    max_age = cfg.filters.max_line_age_minutes * 60
    breakevens = {app: pickem_breakeven(t) for app, t in payouts.items()}

    for _, rows in group_markets(props).items():
        pairs = book_pairs(rows, cfg.devig_method)
        model = cfg.model_for(rows[0].sport.value, model_key(rows[0]))
        for r in rows:
            if r.bookmaker not in cfg.filters.bet_books:
                continue
            if not r.available:
                skipped["closed"] += 1
                continue
            if (now - r.fetched_at).total_seconds() > max_age:
                skipped["stale"] += 1
                continue
            is_pickem = r.payout_type is PayoutType.PICKEM
            if not is_pickem and r.price_american is None:
                skipped["unpriced"] += 1
                continue
            if r.bet_type in ("ou", "sp") and r.line is None:
                skipped["no_line"] += 1
                continue
            usable = [s for s in states if (availability.get(s) or {}).get(r.bookmaker)]
            if not usable and not all_books:
                skipped["not_usable_in_your_states"] += 1
                continue

            is_over, thr = to_threshold(r.bet_type, r.side, r.line)
            fair, reason = fair_for(r.bookmaker, thr, pairs, sharp_books, model, cfg)
            if fair is None:
                skipped[reason] += 1
                continue
            win = fair.outcome.over if is_over else fair.outcome.under
            push = fair.outcome.push
            fair_line = from_threshold(r.bet_type, r.side, fair.source_threshold)
            gap = 0.0 if r.line is None or fair_line is None else abs(r.line - fair_line)

            if is_pickem:
                be = breakevens.get(r.bookmaker)
                p_cond = win / (win + (1 - win - push)) if push else win  # ties void the leg
                implied, be_label = (be[0], be[1]) if be else (float("nan"), None)
                ev_pct = (p_cond / implied - 1) * 100 if be else float("nan")
                kelly_full, stake_amt, win = 0.0, 0.0, p_cond
            else:
                dec = american_to_decimal(r.price_american)
                implied, be_label = implied_prob(r.price_american), None
                ev_pct = sportsbook_ev(win, push, dec) * 100
                kelly_full = kelly_binary(win, dec, push)
                b = cfg.bankroll
                stake_amt = stake(kelly_full, b.amount, b.kelly_fraction, b.max_bet_pct)

            edge = Edge(
                sport=r.sport.value, event_id=r.event_id, start_time=r.start_time, market_kind=r.market_kind.value,
                player=r.player, team=r.team, opponent=r.opponent, stat_type=r.stat_type, period=r.period,
                bet_type=r.bet_type, side=r.side, line=r.line, bookmaker=r.bookmaker, payout_type=r.payout_type.value,
                price_american=r.price_american, implied_prob=implied, fair_prob=win, push_prob=push,
                fair_source=fair.source, fair_books=fair.books, fair_line=fair_line, line_gap=gap,
                confidence=fair.confidence, ev_pct=ev_pct, kelly_full=kelly_full, stake=stake_amt,
                breakeven_slip=be_label, usable_in=usable, player_status=r.player_status,
                fetched_at=r.fetched_at, deeplink=r.deeplink, flags=[f.value for f in r.flags],
                is_alt_line=r.is_alt_line,
            )
            if is_pickem:
                if be is None:
                    skipped["pickem_no_payout_table"] += 1
                else:
                    legs.append(edge)
            if ev_pct != ev_pct:  # NaN: no payout table to judge a pick'em leg
                continue
            if ev_pct > cfg.filters.max_ev_pct:
                skipped["suspicious_ev"] += 1
                continue
            if ev_pct < cfg.filters.min_edge_pct:
                skipped["below_min_edge"] += 1
                continue
            edges.append(edge)

    edges.sort(key=lambda e: e.ev_pct, reverse=True)
    return EdgeResult(edges, legs, skipped)
