import math

import pytest

from iron_butterfly import (
    ChainSnapshot, LegQuote, IronButterflyError,
    select_short_strike, select_wing, build_iron_butterfly,
)


def _leg(strike, delta, bid, ask, is_call):
    sym = f"SPY261026{'C' if is_call else 'P'}{int(strike*1000):08d}"
    return LegQuote(strike=strike, symbol=sym, delta=delta, bid=bid, ask=ask)


def _base_chain() -> ChainSnapshot:
    """SPY-style $1-wide strikes, underlying at 760.00 (exact ATM, no
    tie). Call deltas descend 0.50->~0 going up from 760; put deltas
    (signed negative) shrink toward 0 going DOWN from 760. Deliberately
    UNEQUAL wing widths: the closest-to-0.10 put is 8 points away (752),
    the closest-to-0.10 call is 6 points away (766) -- exercises the
    "wings chosen by delta, not fixed width" rule directly."""
    call_deltas = {
        750: 0.90, 751: 0.85, 752: 0.80, 753: 0.75, 754: 0.70,
        755: 0.65, 756: 0.60, 757: 0.55, 758: 0.55, 759: 0.55,
        760: 0.50,
        761: 0.45, 762: 0.40, 763: 0.35, 764: 0.30, 765: 0.22,
        766: 0.10, 767: 0.06, 768: 0.04, 769: 0.02, 770: 0.01,
    }
    put_deltas = {
        750: -0.05, 751: -0.07, 752: -0.10, 753: -0.15, 754: -0.20,
        755: -0.25, 756: -0.30, 757: -0.35, 758: -0.40, 759: -0.45,
        760: -0.50,
        761: -0.55, 762: -0.60, 763: -0.65, 764: -0.70, 765: -0.75,
        766: -0.80, 767: -0.85, 768: -0.88, 769: -0.90, 770: -0.92,
    }
    calls = [_leg(float(k), v, bid=round(v * 6, 2), ask=round(v * 6 + 0.05, 2), is_call=True)
             for k, v in call_deltas.items()]
    puts = [_leg(float(k), v, bid=round(abs(v) * 6, 2), ask=round(abs(v) * 6 + 0.05, 2), is_call=False)
            for k, v in put_deltas.items()]

    # Override the four legs the happy-path test actually prices, with
    # hand-picked bid/ask so expected $ figures are exact and legible
    # (the generated bid/ask above is just plausible filler elsewhere).
    for leg in calls:
        if leg.strike == 760.0:
            leg.bid, leg.ask = 3.00, 3.10
        elif leg.strike == 766.0:
            leg.bid, leg.ask = 0.20, 0.25
    for leg in puts:
        if leg.strike == 760.0:
            leg.bid, leg.ask = 2.90, 3.00
        elif leg.strike == 752.0:
            leg.bid, leg.ask = 0.15, 0.20

    return ChainSnapshot(underlying_price=760.0, calls=calls, puts=puts)


class TestSelectShortStrike:
    def test_picks_strike_nearest_underlying(self):
        chain = _base_chain()
        strike, call, put = select_short_strike(chain)
        assert strike == 760.0
        assert call.strike == 760.0 and put.strike == 760.0

    def test_tie_break_prefers_delta_closest_to_half(self):
        """Remove strike 760 so 759 and 761 are the two nearest strikes
        to underlying_price=760.0, tied at distance 1.0 each. At
        baseline both combine to the same distance from 0.50 (759:
        call .55/put -.45 -> .05+.05=.10; 761: call .45/put -.55 ->
        .05+.05=.10) -- a genuine tie. Nudge 759's call delta to 0.58
        (combined .13) so 761 (combined .10) wins outright."""
        chain = _base_chain()
        chain.calls = [c for c in chain.calls if c.strike != 760.0]
        chain.puts = [p for p in chain.puts if p.strike != 760.0]
        for c in chain.calls:
            if c.strike == 759.0:
                c.delta = 0.58
        strike, call, put = select_short_strike(chain)
        assert strike == 761.0

    def test_raises_if_no_strike_has_both_sides(self):
        chain = ChainSnapshot(underlying_price=760.0, calls=[], puts=[])
        with pytest.raises(IronButterflyError):
            select_short_strike(chain)


class TestSelectWing:
    def test_put_wing_below_short_strike_closest_to_target(self):
        chain = _base_chain()
        wing = select_wing(chain.puts, 760.0, "put", target_delta=0.10)
        assert wing.strike == 752.0  # exact -0.10 match

    def test_call_wing_above_short_strike_closest_to_target(self):
        chain = _base_chain()
        wing = select_wing(chain.calls, 760.0, "call", target_delta=0.10)
        assert wing.strike == 766.0  # exact 0.10 match

    def test_unequal_wing_widths_are_not_forced_equal(self):
        chain = _base_chain()
        put_wing = select_wing(chain.puts, 760.0, "put", target_delta=0.10)
        call_wing = select_wing(chain.calls, 760.0, "call", target_delta=0.10)
        put_width = 760.0 - put_wing.strike
        call_width = call_wing.strike - 760.0
        assert put_width == 8.0
        assert call_width == 6.0
        assert put_width != call_width

    def test_raises_if_no_candidates_on_the_correct_side(self):
        chain = _base_chain()
        # Only strikes ABOVE 760 for "put" side -> none below -> error.
        above_only = [l for l in chain.puts if l.strike > 760.0]
        with pytest.raises(IronButterflyError):
            select_wing(above_only, 760.0, "put", target_delta=0.10)


class TestBuildIronButterflyEconomics:
    def test_happy_path_matches_hand_computed_figures(self):
        chain = _base_chain()
        r = build_iron_butterfly(chain, contracts=1, multiplier=100, target_pct=0.25)

        assert r.short_strike == 760.0
        assert r.long_put.strike == 752.0
        assert r.long_call.strike == 766.0

        # net_credit = (3.05 + 2.95) - (0.225 + 0.175) = 6.00 - 0.40 = 5.60
        assert math.isclose(r.net_credit, 5.60, abs_tol=1e-9)
        assert math.isclose(r.max_profit, 5.60, abs_tol=1e-9)

        assert r.call_wing_width == 6.0
        assert r.put_wing_width == 8.0
        # call_side_max_loss = 6 - 5.60 = 0.40
        assert math.isclose(r.call_side_max_loss, 0.40, abs_tol=1e-9)
        # put_side_max_loss = 8 - 5.60 = 2.40
        assert math.isclose(r.put_side_max_loss, 2.40, abs_tol=1e-9)
        # max_loss binds on the put side here
        assert math.isclose(r.max_loss, 2.40, abs_tol=1e-9)

        assert math.isclose(r.breakeven_lower, 754.40, abs_tol=1e-9)
        assert math.isclose(r.breakeven_upper, 765.60, abs_tol=1e-9)

        assert math.isclose(r.reward_to_risk, 5.60 / 2.40, abs_tol=1e-9)
        assert math.isclose(r.credit_to_width_call, 5.60 / 6.0, abs_tol=1e-9)
        assert math.isclose(r.credit_to_width_put, 5.60 / 8.0, abs_tol=1e-9)

        # gross_profit_target_debit = 5.60 * (1 - 0.25) = 4.20 (fees ignored)
        assert math.isclose(r.gross_profit_target_debit, 4.20, abs_tol=1e-9)

        # capital_at_risk = max_loss * multiplier * contracts = 2.40*100*1
        assert math.isclose(r.capital_at_risk, 240.0, abs_tol=1e-9)
        assert r.warnings == []

        # Net-of-fees figures (fees.py, contracts=1): entry=4.482, exit=0.482
        assert math.isclose(r.entry_fees, 4.482, abs_tol=1e-9)
        assert math.isclose(r.exit_fees, 0.482, abs_tol=1e-9)
        assert math.isclose(r.round_trip_fees, 4.964, abs_tol=1e-9)
        # net_max_profit = 560 - 4.964 = 555.036; net_max_loss = 240 + 4.964 = 244.964
        assert math.isclose(r.net_max_profit, 555.036, abs_tol=1e-9)
        assert math.isclose(r.net_max_loss, 244.964, abs_tol=1e-9)
        assert math.isclose(r.net_reward_to_risk, 555.036 / 244.964, abs_tol=1e-9)
        # profit_target_debit (NET): net_credit - (0.25*555.036 + 4.964)/100
        assert math.isclose(r.profit_target_debit, 5.60 - (0.25 * 555.036 + 4.964) / 100, abs_tol=1e-9)
        # The net target is more conservative than the gross one -- it takes
        # a LOWER debit (more of the credit given back) to actually realize
        # the same target_pct of profit once real fees are subtracted.
        assert r.profit_target_debit < r.gross_profit_target_debit

    def test_contracts_and_multiplier_scale_capital_at_risk_only(self):
        chain = _base_chain()
        r1 = build_iron_butterfly(chain, contracts=1, multiplier=100)
        r3 = build_iron_butterfly(chain, contracts=3, multiplier=100)
        assert math.isclose(r3.capital_at_risk, r1.capital_at_risk * 3, abs_tol=1e-9)
        # Per-share figures are unaffected by contracts/multiplier.
        assert r1.net_credit == r3.net_credit
        assert r1.max_loss == r3.max_loss

    def test_target_pct_changes_only_profit_target_debit(self):
        chain = _base_chain()
        r10 = build_iron_butterfly(chain, target_pct=0.10)
        r50 = build_iron_butterfly(chain, target_pct=0.50)
        assert math.isclose(r10.gross_profit_target_debit, r10.net_credit * 0.90, abs_tol=1e-9)
        assert math.isclose(r50.gross_profit_target_debit, r50.net_credit * 0.50, abs_tol=1e-9)
        assert r10.net_credit == r50.net_credit
        # A smaller target_pct means a higher (less aggressive) debit --
        # true for the net-of-fees figure too, same direction as gross.
        assert r10.profit_target_debit > r50.profit_target_debit

    def test_more_contracts_means_lower_round_trip_fee_per_share_impact(self):
        """The $10/leg commission cap means fees grow sub-linearly past
        10 contracts -- the net profit target should converge toward the
        gross one as size increases (fees matter less per share)."""
        chain = _base_chain()
        r_small = build_iron_butterfly(chain, contracts=1)
        r_big = build_iron_butterfly(chain, contracts=50)
        gap_small = r_small.gross_profit_target_debit - r_small.profit_target_debit
        gap_big = r_big.gross_profit_target_debit - r_big.profit_target_debit
        assert gap_big < gap_small


class TestSanityChecks:
    def test_aborts_on_non_positive_net_credit(self):
        chain = _base_chain()
        # Make the call wing (selected by delta, price doesn't move the
        # selection) absurdly expensive so credit goes negative:
        # (3.05+2.95) - (6.05+0.175) = -0.225
        for c in chain.calls:
            if c.strike == 766.0:
                c.bid, c.ask = 6.00, 6.10
        with pytest.raises(IronButterflyError, match="Net credit"):
            build_iron_butterfly(chain)

    def test_aborts_when_net_credit_exceeds_a_wing_width(self):
        chain = _base_chain()
        # Blow out the short premiums so credit (still computed from
        # mids) exceeds the 6-point call wing width.
        for c in chain.calls:
            if c.strike == 760.0:
                c.bid, c.ask = 10.00, 10.10
        with pytest.raises(IronButterflyError, match="wing width"):
            build_iron_butterfly(chain)

    def test_aborts_on_missing_bid_ask(self):
        chain = _base_chain()
        for c in chain.calls:
            if c.strike == 766.0:
                c.bid, c.ask = None, None
        with pytest.raises(IronButterflyError, match="no bid/ask"):
            build_iron_butterfly(chain)

    def test_aborts_on_zero_bid_long_wing(self):
        chain = _base_chain()
        for p in chain.puts:
            if p.strike == 752.0:
                p.bid = 0.0
        with pytest.raises(IronButterflyError, match="zero bid"):
            build_iron_butterfly(chain)

    def test_zero_bid_on_a_SHORT_leg_is_not_specifically_checked(self):
        """Spec calls out zero bid specifically for long wings; a short
        leg with a (quoted, non-missing) zero bid is not aborted by that
        rule -- only the general missing-bid/ask and credit checks apply."""
        chain = _base_chain()
        for c in chain.calls:
            if c.strike == 760.0:
                c.bid = 0.0  # ask stays 3.10 -> mid 1.55, still net positive credit
        r = build_iron_butterfly(chain)
        assert r.short_call.bid == 0.0

    def test_aborts_on_far_wing_delta_by_default(self):
        chain = _base_chain()
        for c in chain.calls:
            if c.strike == 766.0:
                c.delta = 0.20  # 0.10 away from target 0.10 -- over the 0.03 tolerance
        with pytest.raises(IronButterflyError, match="allow-far-wings"):
            build_iron_butterfly(chain)

    def test_allow_far_wings_warns_instead_of_aborting(self):
        chain = _base_chain()
        for c in chain.calls:
            if c.strike == 766.0:
                c.delta = 0.20
        r = build_iron_butterfly(chain, allow_far_wings=True)
        assert any("WARNING" in w for w in r.warnings)
        # Whichever call ends up closest-to-target with 766 perturbed
        # away, the point of this test is that it's still > tolerance
        # from the target and got warned, not aborted.
        assert abs(abs(r.long_call.delta) - 0.10) > 0.03

    def test_wing_delta_within_tolerance_does_not_warn(self):
        chain = _base_chain()
        for c in chain.calls:
            if c.strike == 766.0:
                c.delta = 0.12  # within 0.03 of 0.10
        r = build_iron_butterfly(chain)
        assert r.warnings == []
