"""Kelly stake sizing. Full Kelly maximises expected log bankroll; we scale it
by a fraction (default 0.25) and cap it at a share of bankroll."""

from __future__ import annotations

import math
from collections.abc import Sequence


def kelly_binary(p_win: float, decimal_odds: float, p_push: float = 0.0) -> float:
    """Full-Kelly fraction for a bet paying (decimal - 1) with a refund on push.

    Maximising p*ln(1 + b f) + q*ln(1 - f) (push term is constant) gives
    f* = (b p - q) / (b (p + q)), where b = decimal - 1 and q = P(lose)."""
    b = decimal_odds - 1
    q = 1 - p_win - p_push
    f = (b * p_win - q) / (b * (p_win + q))
    return max(f, 0.0)


def kelly_multi(outcomes: Sequence[tuple[float, float]]) -> float:
    """Full-Kelly fraction for a bet with several outcomes (probability, total
    return multiple incl. stake), e.g. a flex slip. Solves
    sum p_k (m_k - 1) / (1 + f (m_k - 1)) = 0 by bisection."""
    if abs(sum(p for p, _ in outcomes) - 1) > 1e-6:
        raise ValueError("outcome probabilities must sum to 1")

    def slope(f: float) -> float:
        return sum(p * (m - 1) / (1 + f * (m - 1)) for p, m in outcomes)

    if slope(0.0) <= 0:  # non-positive EV
        return 0.0
    worst = min(m for _, m in outcomes)
    hi = 1.0 if worst >= 1 else (1 / (1 - worst)) * (1 - 1e-9)
    if worst >= 1:  # can't lose: unbounded; never happens with real payouts
        return 1.0
    lo = 0.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if slope(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def stake(full_kelly: float, bankroll: float, fraction: float = 0.25, max_pct: float = 2.0) -> float:
    """Dollar stake: fractional Kelly capped at max_pct% of bankroll, rounded to cents."""
    if bankroll <= 0 or not math.isfinite(full_kelly):
        return 0.0
    f = min(max(full_kelly, 0.0) * fraction, max_pct / 100)
    return round(f * bankroll, 2)
