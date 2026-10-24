"""Remove the bookmaker margin from a two-way (or n-way) market.

multiplicative: p_i = q_i / sum(q). Spreads the vig in proportion to price.
power:          p_i = q_i ** k with k chosen so sum(p) = 1. Since k > 1 when the
                book is overround, longshots lose relatively more of their implied
                probability: this models favourite-longshot bias, which is usually
                a better fit for player props and lopsided lines.
Here q_i are the raw implied probabilities of each outcome's price.
"""

from __future__ import annotations

from enum import StrEnum

from bookerbud.edge.odds import implied_prob


class DevigMethod(StrEnum):
    MULTIPLICATIVE = "multiplicative"
    POWER = "power"


def devig_multiplicative(implied: list[float]) -> list[float]:
    total = sum(implied)
    return [q / total for q in implied]


def devig_power(implied: list[float], tol: float = 1e-12) -> list[float]:
    if any(not 0 < q < 1 for q in implied):
        raise ValueError(f"implied probabilities must be in (0, 1): {implied}")
    # f(k) = sum(q^k) - 1 is strictly decreasing in k; bracket then bisect.
    lo, hi = 1e-6, 1.0
    while sum(q**hi for q in implied) > 1:
        hi *= 2
    while sum(q**lo for q in implied) < 1 and lo > 1e-12:
        lo /= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if sum(q**mid for q in implied) > 1:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    k = (lo + hi) / 2
    probs = [q**k for q in implied]
    s = sum(probs)
    return [p / s for p in probs]  # remove residual bisection error


def devig(american_prices: list[int | float], method: DevigMethod | str = DevigMethod.POWER) -> list[float]:
    """Fair probabilities for each outcome, in the order given."""
    implied = [implied_prob(a) for a in american_prices]
    if DevigMethod(method) is DevigMethod.MULTIPLICATIVE:
        return devig_multiplicative(implied)
    return devig_power(implied)
