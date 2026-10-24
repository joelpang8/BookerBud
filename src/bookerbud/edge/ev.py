"""Expected value for sportsbook bets and pick'em slips (per 1 unit staked)."""

from __future__ import annotations

from collections.abc import Sequence


def sportsbook_ev(p_win: float, p_push: float, decimal_odds: float) -> float:
    """Win pays (decimal - 1), push refunds, loss costs 1."""
    p_lose = 1 - p_win - p_push
    return p_win * (decimal_odds - 1) - p_lose


def hit_distribution(probs: Sequence[float]) -> list[float]:
    """Poisson-binomial: P(exactly k of the independent legs hit), k = 0..n."""
    dist = [1.0]
    for p in probs:
        nxt = [0.0] * (len(dist) + 1)
        for k, q in enumerate(dist):
            nxt[k] += q * (1 - p)
            nxt[k + 1] += q * p
        dist = nxt
    return dist


def slip_ev(probs: Sequence[float], payouts: dict[int, float]) -> float:
    """EV of a pick'em slip. payouts maps hits -> total return multiple
    (entry included); hit counts not listed pay 0."""
    dist = hit_distribution(probs)
    return sum(dist[k] * m for k, m in payouts.items() if k < len(dist)) - 1


def breakeven_leg_prob(n_legs: int, payouts: dict[int, float]) -> float:
    """Per-leg win probability p (same for every leg) at which the slip is EV 0."""
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if slip_ev([mid] * n_legs, payouts) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
