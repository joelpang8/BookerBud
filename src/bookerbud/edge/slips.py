"""Pick'em slip optimizer: best 2-6 leg entries per app.

Legs are treated as independent EXCEPT same-game legs, which are either
excluded (default) or have each leg's probability shaded by
`same_game_penalty`. That's a deliberately blunt, conservative haircut, not a
correlation model; same-game legs can be positively or negatively correlated.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from bookerbud.edge.config import EdgeConfig, SlipTable
from bookerbud.edge.engine import Edge
from bookerbud.edge.ev import hit_distribution, slip_ev
from bookerbud.edge.kelly import kelly_multi, stake


@dataclass
class Slip:
    app: str
    kind: str  # power / flex / standard ...
    legs: list[Edge]
    probs: list[float]  # after any same-game penalty
    payouts: dict[int, float]
    ev_pct: float
    win_prob: float  # P(any payout >= 1x)
    kelly_full: float
    stake: float
    same_game_pairs: int
    verified_table: bool

    @property
    def size(self) -> int:
        return len(self.legs)


def _best_side_per_market(legs: list[Edge]) -> list[Edge]:
    best: dict[tuple, Edge] = {}
    for leg in legs:
        k = (leg.event_id, leg.player, leg.stat_type, leg.period)
        if k not in best or leg.fair_prob > best[k].fair_prob:
            best[k] = leg
    return list(best.values())


def build_slips(
    legs: list[Edge],
    payouts: dict[str, SlipTable],
    cfg: EdgeConfig,
    *,
    sizes: range | None = None,
    top_per_size: int = 3,
) -> list[Slip]:
    sc = cfg.slips
    sizes = sizes or range(sc.min_legs, sc.max_legs + 1)
    out: list[Slip] = []
    for app, table in payouts.items():
        app_legs = [l for l in legs if l.bookmaker == app and l.market_kind == "player_prop"]
        if not app_legs or not table.slips:
            continue
        pool = sorted(_best_side_per_market(app_legs), key=lambda l: l.fair_prob, reverse=True)[: sc.candidate_legs]
        for n in sizes:
            candidates: list[Slip] = []
            for combo in combinations(pool, n):
                players = {l.player for l in combo}
                if len(players) < n:
                    continue
                if len({l.team for l in combo}) < min(sc.min_teams, n):
                    continue
                events = [l.event_id for l in combo]
                sg_pairs = sum(events.count(e) - 1 for e in set(events))
                if sg_pairs and sc.same_game == "exclude":
                    continue
                probs = [
                    l.fair_prob * (sc.same_game_penalty if events.count(l.event_id) > 1 else 1.0) for l in combo
                ]
                for kind, by_size in table.slips.items():
                    pay = by_size.get(n)
                    if not pay:
                        continue
                    ev = slip_ev(probs, pay)
                    dist = hit_distribution(probs)
                    outcomes = [(dist[k], pay.get(k, 0.0)) for k in range(n + 1)]
                    kf = kelly_multi(outcomes)
                    b = cfg.bankroll
                    candidates.append(Slip(
                        app=app, kind=kind, legs=list(combo), probs=probs, payouts=pay, ev_pct=ev * 100,
                        win_prob=sum(p for p, m in outcomes if m >= 1), kelly_full=kf,
                        stake=stake(kf, b.amount, b.kelly_fraction, b.max_bet_pct),
                        same_game_pairs=sg_pairs, verified_table=table.verified,
                    ))
            candidates = [c for c in candidates if c.ev_pct > 0]
            candidates.sort(key=lambda s: s.ev_pct, reverse=True)
            out.extend(candidates[:top_per_size])
    return out
