"""Price conversions. American odds are what US books quote; decimal odds
include the stake (decimal 1.909 = win 0.909 per 1 staked)."""

from __future__ import annotations


def american_to_decimal(american: int | float) -> float:
    if american == 0 or -100 < american < 100:
        raise ValueError(f"invalid American odds: {american}")
    return 1 + american / 100 if american > 0 else 1 + 100 / -american


def decimal_to_american(decimal: float) -> int:
    if decimal <= 1:
        raise ValueError(f"invalid decimal odds: {decimal}")
    return round((decimal - 1) * 100) if decimal >= 2 else round(-100 / (decimal - 1))


def implied_prob(american: int | float) -> float:
    """Break-even probability of a price, vig included (-110 -> 0.5238)."""
    return 1 / american_to_decimal(american)


def prob_to_american(p: float) -> int:
    if not 0 < p < 1:
        raise ValueError(f"probability out of range: {p}")
    return decimal_to_american(1 / p)
