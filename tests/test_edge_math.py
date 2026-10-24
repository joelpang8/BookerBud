"""Hand-checked numbers. Each expected value has its arithmetic in a comment."""

import math

import pytest

from bookerbud.edge.devig import devig, devig_multiplicative, devig_power
from bookerbud.edge.ev import breakeven_leg_prob, hit_distribution, slip_ev, sportsbook_ev
from bookerbud.edge.kelly import kelly_binary, kelly_multi, stake
from bookerbud.edge.lines import Outcome, StatModel, adjust
from bookerbud.edge.odds import american_to_decimal, decimal_to_american, implied_prob, prob_to_american

approx = pytest.approx


# ---------------------------------------------------------------- odds
@pytest.mark.parametrize("american, decimal", [(-110, 1 + 100 / 110), (+150, 2.5), (-200, 1.5), (+100, 2.0), (-100, 2.0)])
def test_american_to_decimal(american, decimal):
    assert american_to_decimal(american) == approx(decimal)


def test_implied_and_back():
    assert implied_prob(-110) == approx(110 / 210)  # 0.523810
    assert implied_prob(+150) == approx(0.4)  # 100 / 250
    assert decimal_to_american(2.5) == 150 and decimal_to_american(1.5) == -200
    assert prob_to_american(0.6) == -150  # 1/0.6 = 1.6667 -> -100/0.6667
    with pytest.raises(ValueError):
        american_to_decimal(50)


# ---------------------------------------------------------------- devig
def test_multiplicative_symmetric():
    assert devig([-110, -110], "multiplicative") == approx([0.5, 0.5])


def test_multiplicative_hand():
    # q = 0.6 (-150), 0.434783 (+130); sum 1.034783 -> 0.579832, 0.420168
    assert devig([-150, +130], "multiplicative") == approx([0.579832, 0.420168], abs=1e-6)


def test_power_defining_property_and_values():
    q = [implied_prob(-150), implied_prob(+130)]
    p = devig_power(q)
    assert sum(p) == approx(1)
    k = math.log(p[0]) / math.log(q[0])  # p_i = q_i^k with one shared k
    assert math.log(p[1]) / math.log(q[1]) == approx(k, rel=1e-9)
    assert k > 1  # overround -> k > 1
    assert p == approx([0.583983, 0.416017], abs=1e-6)
    # power shades the longshot more than multiplicative does
    assert p[1] < devig_multiplicative(q)[1]


def test_power_symmetric_and_three_way():
    assert devig([-110, -110], "power") == approx([0.5, 0.5])
    p = devig([+200, +200, +200], "power")  # q = 1/3 each, no vig -> unchanged
    assert p == approx([1 / 3] * 3)


# ---------------------------------------------------------------- line adjustment
def test_same_half_line_is_identity():
    o = adjust(StatModel("normal", 2, cv=0.3), 24.5, 0.55, 24.5)
    assert (o.over, o.push, o.under) == approx((0.55, 0, 0.45))


def test_poisson_half_lines():
    # P(over 0.5) = 1 - e^-l = 0.6 -> l = -ln 0.4 = 0.916291
    # P(over 1.5) = 1 - e^-l (1 + l) = 1 - 0.4 * 1.916291 = 0.233484
    o = adjust(StatModel("poisson", 2), 0.5, 0.6, 1.5)
    assert o.over == approx(0.233484, abs=1e-6) and o.push == 0


def test_poisson_whole_line_push():
    # lambda = 2 exactly gives P(X >= 2) = 1 - 3e^-2 = 0.593994
    # at line 2: push = P(X=2) = 2e^-2 = 0.270671, under = P(X<=1) = 3e^-2 = 0.406006
    o = adjust(StatModel("poisson", 2), 1.5, 1 - 3 * math.exp(-2), 2)
    assert o.push == approx(2 * math.exp(-2), abs=1e-6)
    assert o.under == approx(3 * math.exp(-2), abs=1e-6)
    assert o.over == approx(1 - 5 * math.exp(-2), abs=1e-6)  # 0.323324


def test_normal_shift():
    # sigma = 0.30 * 24.5 = 7.35; mu = 24.5 + 7.35 * z(0.55) = 24.5 + 7.35*0.125661 = 25.423611
    # P(over 25.5) = 1 - Phi((25.5 - 25.423611) / 7.35) = 1 - Phi(0.010393) = 0.495854
    o = adjust(StatModel("normal", 2, cv=0.3), 24.5, 0.55, 25.5)
    assert o.over == approx(0.495854, abs=1e-5)


def test_normal_whole_target_has_push_and_conditional_fit():
    m = StatModel("normal", 2, sd=12.0)
    o = adjust(m, 4.5, 0.5, 4.0)  # margin threshold 4.5 -> 4.0 (home -4.5 vs -4)
    # mu = 4.5 (50/50 at half line); push = P(3.5 < X < 4.5) = Phi(0) - Phi(-1/12) = 0.033207
    assert o.push == approx(0.033207, abs=1e-5)
    assert o.over == approx(0.5, abs=1e-9)  # P(X > 4.5) unchanged
    # going back: fit at a whole line uses the no-push conditional probability
    back = adjust(m, 4.0, o.over_given_no_push, 4.5)
    assert back.over == approx(0.5, abs=1e-6)


def test_gap_too_large_skips():
    assert adjust(StatModel("normal", 1.0, cv=0.3), 24.5, 0.55, 26.5) is None
    assert adjust(StatModel("poisson", 0.5), 0.5, 0.6, 1.5) is None


def test_outcome_monotone():
    m = StatModel("normal", 5, cv=0.3)
    overs = [adjust(m, 24.5, 0.55, x).over for x in (22.5, 23.5, 24.5, 25.5, 26.5)]
    assert overs == sorted(overs, reverse=True)


def test_model_validation():
    with pytest.raises(ValueError):
        StatModel("gamma", 1)
    with pytest.raises(ValueError):
        StatModel("normal", 1)


# ---------------------------------------------------------------- EV
def test_sportsbook_ev():
    # 55% at -110: 0.55 * 0.909091 - 0.45 = 0.05
    assert sportsbook_ev(0.55, 0, american_to_decimal(-110)) == approx(0.05)
    # 50% at +100 with 10% push: 0.5*1 - 0.4 = 0.1
    assert sportsbook_ev(0.5, 0.1, 2.0) == approx(0.1)
    assert sportsbook_ev(0.5, 0, american_to_decimal(-110)) < 0


def test_hit_distribution():
    assert hit_distribution([0.5, 0.5]) == approx([0.25, 0.5, 0.25])
    # 0.6 & 0.7: none = 0.12, one = 0.6*0.3 + 0.4*0.7 = 0.46, both = 0.42
    assert hit_distribution([0.6, 0.7]) == approx([0.12, 0.46, 0.42])


def test_slip_ev_power_and_flex():
    # PrizePicks 2-pick power 3x: 0.36 * 3 - 1 = 0.08
    assert slip_ev([0.6, 0.6], {2: 3.0}) == approx(0.08)
    # 3-pick flex 3x / 1x: P3 = 0.216, P2 = 3 * 0.36 * 0.4 = 0.432 -> 0.648 + 0.432 - 1 = 0.08
    assert slip_ev([0.6] * 3, {3: 3.0, 2: 1.0}) == approx(0.08)


def test_breakeven_leg_prob():
    assert breakeven_leg_prob(2, {2: 3.0}) == approx(1 / math.sqrt(3), abs=1e-9)  # 0.577350
    assert breakeven_leg_prob(3, {3: 6.0}) == approx(6 ** (-1 / 3), abs=1e-9)  # 0.550321


# ---------------------------------------------------------------- Kelly
def test_kelly_binary():
    # b = 0.909091, p = 0.55, q = 0.45: (0.5 - 0.45) / 0.909091 = 0.055
    assert kelly_binary(0.55, american_to_decimal(-110)) == approx(0.055)
    # even money 60%: (0.6 - 0.4) / 1 = 0.2
    assert kelly_binary(0.6, 2.0) == approx(0.2)
    assert kelly_binary(0.45, 2.0) == 0.0
    # with push 0.1, p=0.5, q=0.4, b=1: (0.5-0.4)/(0.9) = 0.111111
    assert kelly_binary(0.5, 2.0, p_push=0.1) == approx(1 / 9)


def test_kelly_multi_matches_binary_and_slips():
    assert kelly_multi([(0.6, 2.0), (0.4, 0.0)]) == approx(0.2, abs=1e-9)
    # 2-pick power 3x at 0.36: binary b = 2: (2*0.36 - 0.64)/2 = 0.04
    assert kelly_multi([(0.36, 3.0), (0.64, 0.0)]) == approx(0.04, abs=1e-9)
    assert kelly_multi([(0.3, 3.0), (0.7, 0.0)]) == 0.0
    with pytest.raises(ValueError):
        kelly_multi([(0.5, 2.0)])


def test_stake_fraction_and_cap():
    # quarter Kelly of 0.055 on $1,000 = $13.75; cap 2% = $20 not binding
    assert stake(0.055, 1000, 0.25, 2.0) == 13.75
    # quarter of 0.2 = 0.05 -> capped at 2% -> $20
    assert stake(0.2, 1000, 0.25, 2.0) == 20.0
    assert stake(-0.1, 1000) == 0.0 and stake(0.1, 0) == 0.0


def test_outcome_conditional():
    assert Outcome(0.4, 0.2, 0.4).over_given_no_push == approx(0.5)
