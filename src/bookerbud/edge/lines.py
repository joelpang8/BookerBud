"""Moving a fair probability from one line to another.

Books often hang different numbers on the same market (Pinnacle 24.5,
PrizePicks 25.5). We fit a distribution for the stat to the fair price at the
source line, then read the probability off at the target line.

* poisson: count stats with small means (threes, steals, blocks, home runs,
  strikeouts, receptions, TDs). Fit lambda so P(X > L1) matches; exact integer
  arithmetic, so whole-number lines get a real push probability.
* normal: high-volume stats (points, yards, PRA) and game margins/totals.
  sigma is fixed up front: either `sd` (game lines) or `cv * source line`
  (player stats). Outcomes are integers, so we use a continuity correction:
  over L wins when X >= floor(L) + 1, i.e. X > floor(L) + 0.5.

The fair probability from a two-way devig is conditional on no push, so for
whole-number source lines we fit P(over) / (1 - P(push)).

Fits are 1-parameter, so they're only trustworthy for small moves.
`StatModel.max_gap` caps how far we'll move a line; beyond that we skip the
play instead of guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist


@dataclass(frozen=True)
class StatModel:
    kind: str  # "poisson" | "normal"
    max_gap: float
    cv: float | None = None  # normal: sigma = cv * source line
    sd: float | None = None  # normal: fixed sigma (overrides cv)

    def __post_init__(self):
        if self.kind not in ("poisson", "normal"):
            raise ValueError(f"unknown model kind {self.kind!r}")
        if self.kind == "normal" and self.sd is None and self.cv is None:
            raise ValueError("normal model needs sd or cv")


@dataclass(frozen=True)
class Outcome:
    over: float
    push: float
    under: float

    @property
    def over_given_no_push(self) -> float:
        return self.over / (self.over + self.under)


def _is_whole(line: float) -> bool:
    return abs(line - round(line)) < 1e-9


def _poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k)."""
    if k < 0:
        return 0.0
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(total, 1.0)


def _poisson_pmf(k: int, lam: float) -> float:
    if k < 0:
        return 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1)) if lam > 0 else float(k == 0)


def outcome(model: StatModel, param: float, line: float, sigma: float | None = None) -> Outcome:
    """Over/push/under at `line` given the fitted location param (lambda or mu)."""
    if model.kind == "poisson":
        if _is_whole(line):
            k = round(line)
            push = _poisson_pmf(k, param)
            under = _poisson_cdf(k - 1, param)
        else:
            push = 0.0
            under = _poisson_cdf(math.floor(line), param)
        return Outcome(over=max(1 - under - push, 0.0), push=push, under=under)
    nd = NormalDist(param, sigma)
    if _is_whole(line):
        under = nd.cdf(line - 0.5)
        over = 1 - nd.cdf(line + 0.5)
        return Outcome(over=over, push=1 - over - under, under=under)
    # half (or other fractional) line: no push; continuity-correct to the integer grid
    cut = math.floor(line) + 0.5
    under = nd.cdf(cut)
    return Outcome(over=1 - under, push=0.0, under=under)


def sigma_for(model: StatModel, source_line: float) -> float | None:
    if model.kind != "normal":
        return None
    return model.sd if model.sd is not None else model.cv * max(abs(source_line), 1.0)


def fit(model: StatModel, line: float, p_over_no_push: float, sigma: float | None = None) -> float:
    """Location parameter that reproduces the fair over probability at `line`."""
    if not 0 < p_over_no_push < 1:
        raise ValueError("probability must be in (0, 1)")
    if model.kind == "poisson":
        lo, hi = 1e-9, max(4 * (line + 1), 10.0)
    else:
        lo, hi = line - 50 * sigma, line + 50 * sigma
    for _ in range(200):
        mid = (lo + hi) / 2
        if outcome(model, mid, line, sigma).over_given_no_push < p_over_no_push:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def adjust(model: StatModel, from_line: float, p_over_no_push: float, to_line: float) -> Outcome | None:
    """Fair outcome at `to_line` given the fair (no-push) over prob at `from_line`.
    Returns None when the gap exceeds model.max_gap."""
    if abs(to_line - from_line) > model.max_gap + 1e-9:
        return None
    sigma = sigma_for(model, from_line)
    if abs(to_line - from_line) < 1e-9 and not _is_whole(from_line):
        return Outcome(over=p_over_no_push, push=0.0, under=1 - p_over_no_push)
    param = fit(model, from_line, p_over_no_push, sigma)
    return outcome(model, param, to_line, sigma)
