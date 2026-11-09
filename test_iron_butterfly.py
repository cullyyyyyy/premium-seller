import math

import pytest

from iron_butterfly import (
    ChainSnapshot, LegQuote, IronButterflyError,
    select_short_strike, select_wing, build_iron_butterfly, _floor_to_cent,
    select_untested_short, build_untested_side_roll,
)


class TestFloorToCent:
    def test_truncates_down_never_to_nearest(self):
        """User 2026-10-27: 'you should also round down or truncate
        (i.e. 1.1553 should distill to 1.15).' Plain round(1.1553, 2)
        would give 1.16 (nearer) -- this must not."""
        assert _floor_to_cent(1.1553) == 1.15

    def test_already_exact_cent_is_unchanged(self):
        assert _floor_to_cent(4.20) == 4.20

    def test_float_noise_just_below_a_cent_boundary_does_not_floor_an_extra_cent(self):
        assert _floor_to_cent(1.1499999999996) == 1.15


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

    def test_never_picks_under_target_when_an_at_or_above_candidate_exists(self):
        """2026-10-27, user: 'I don't want a delta less than 10 but I'll
        accept more than 10. Closest to 10 without going under.' A
        delta of 0.09 is numerically closer to 0.10 than 0.13 is (0.01
        vs 0.03 away), but 0.09 is UNDER target -- 0.13 must win."""
        legs = [
            LegQuote(strike=750.0, symbol="under", delta=-0.09, bid=0.10, ask=0.15),
            LegQuote(strike=749.0, symbol="over", delta=-0.13, bid=0.20, ask=0.25),
        ]
        wing = select_wing(legs, 760.0, "put", target_delta=0.10)
        assert wing.symbol == "over"

    def test_picks_closest_at_or_above_when_multiple_qualify(self):
        legs = [
            LegQuote(strike=750.0, symbol="far_over", delta=-0.15, bid=0.10, ask=0.15),
            LegQuote(strike=751.0, symbol="near_over", delta=-0.11, bid=0.20, ask=0.25),
        ]
        wing = select_wing(legs, 760.0, "put", target_delta=0.10)
        assert wing.symbol == "near_over"

    def test_aborts_when_every_candidate_is_under_target(self):
        legs = [
            LegQuote(strike=750.0, symbol="a", delta=-0.07, bid=0.10, ask=0.15),
            LegQuote(strike=749.0, symbol="b", delta=-0.03, bid=0.05, ask=0.10),
        ]
        with pytest.raises(IronButterflyError, match="under-protected"):
            select_wing(legs, 760.0, "put", target_delta=0.10)


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
        # = 5.60 - 1.43723 = 4.16277, FLOORED to the cent (never rounded
        # up/nearest -- user 2026-10-27: "round down or truncate") -> 4.16.
        raw_profit_target_debit = 5.60 - (0.25 * 555.036 + 4.964) / 100
        assert math.isclose(raw_profit_target_debit, 4.16277, abs_tol=1e-9)
        assert math.isclose(r.profit_target_debit, 4.16, abs_tol=1e-9)
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

    def test_does_not_abort_on_a_wing_far_from_target_as_long_as_it_qualifies(self):
        """2026-10-27, user: "I don't need a 3 point buffer. I don't want
        to get below 10 and I want the closest strike to 10." -- there
        used to be a second, separate distance-based abort in
        build_iron_butterfly on top of select_wing's own logic; it's
        gone now. A wing far from target_delta is accepted as long as
        it's the closest available AT OR ABOVE target -- select_wing's
        "nothing qualifies at all" check is the only abort left."""
        chain = _base_chain()
        for c in chain.calls:
            if c.strike == 766.0:
                c.delta = 0.20  # far from 0.10, but still a valid >= target pick
        r = build_iron_butterfly(chain)
        assert r.long_call.strike == 766.0
        assert r.warnings == []


class TestSelectUntestedShort:
    def test_call_side_picks_closest_to_target_at_or_above(self):
        chain = _base_chain()
        leg = select_untested_short(chain.calls, "call", current_price=760.0, target_delta=0.30)
        assert leg.strike == 764.0  # exact 0.30 match

    def test_put_side_picks_closest_to_target_at_or_above(self):
        chain = _base_chain()
        leg = select_untested_short(chain.puts, "put", current_price=760.0, target_delta=0.30)
        assert leg.strike == 756.0  # exact -0.30 match

    def test_never_picks_under_target_when_an_at_or_above_candidate_exists(self):
        legs = [
            LegQuote(strike=761.0, symbol="under", delta=0.09, bid=0.10, ask=0.15),
            LegQuote(strike=762.0, symbol="over", delta=0.13, bid=0.20, ask=0.25),
        ]
        leg = select_untested_short(legs, "call", current_price=760.0, target_delta=0.10)
        assert leg.symbol == "over"

    def test_raises_if_no_strikes_on_the_correct_side_of_current_price(self):
        chain = _base_chain()
        above_only = [l for l in chain.calls if l.strike > 760.0]
        with pytest.raises(IronButterflyError, match="usable delta"):
            select_untested_short(above_only, "put", current_price=760.0, target_delta=0.30)

    def test_raises_if_every_candidate_is_under_target(self):
        legs = [
            LegQuote(strike=769.0, symbol="a", delta=0.02, bid=0.05, ask=0.10),
            LegQuote(strike=770.0, symbol="b", delta=0.01, bid=0.02, ask=0.05),
        ]
        with pytest.raises(IronButterflyError, match="under-targeted"):
            select_untested_short(legs, "call", current_price=760.0, target_delta=0.30)

    def test_opposite_strike_excludes_a_candidate_between_it_and_current_price(self):
        """opposite_strike=765 sits just above the strike that's normally
        picked (764, delta exactly 0.30 -- see
        test_call_side_picks_closest_to_target_at_or_above). With the
        guard active, 764 is excluded even though it's OTM of current
        price (760), the same way it would be if it were ITM -- leaving
        only 766+ (deltas 0.10 and under), which fail the delta target.
        This is the Nov 5 2026 incident reproduced directly: a strike
        that's OTM of spot but UNDER the tested side's open short strike
        must not be selectable."""
        chain = _base_chain()
        with pytest.raises(IronButterflyError, match="under-targeted"):
            select_untested_short(chain.calls, "call", current_price=760.0, target_delta=0.30,
                                   opposite_strike=765.0)

    def test_opposite_strike_is_ignored_when_it_does_not_bind(self):
        """opposite_strike below current_price (call side) should change
        nothing -- current_price is still the tighter bound."""
        chain = _base_chain()
        leg = select_untested_short(chain.calls, "call", current_price=760.0, target_delta=0.30,
                                     opposite_strike=700.0)
        assert leg.strike == 764.0

    def test_opposite_strike_message_names_the_open_short_when_no_candidate_survives(self):
        chain = _base_chain()
        with pytest.raises(IronButterflyError, match=r"open short put \(\$771\.00\)"):
            select_untested_short(chain.calls, "call", current_price=760.0, target_delta=0.30,
                                   opposite_strike=771.0)

    def test_opposite_strike_binds_on_put_side_too(self):
        chain = _base_chain()
        with pytest.raises(IronButterflyError, match=r"open short call \(\$-1\.00\)"):
            select_untested_short(chain.puts, "put", current_price=760.0, target_delta=0.30,
                                   opposite_strike=-1.0)


class TestBuildUntestedSideRoll:
    def test_call_side_roll_matches_hand_computed_figures(self):
        chain = _base_chain()
        roll = build_untested_side_roll(chain, side="call", short_target_delta=0.30, wing_target_delta=0.10)
        assert roll.side == "call"
        assert roll.new_short_strike == 764.0
        assert roll.new_wing_strike == 766.0
        assert roll.new_wing_width == 2.0
        # new short mid=(1.80+1.85)/2=1.825, new wing mid=(0.20+0.25)/2=0.225 (overridden in _base_chain)
        assert round(roll.roll_credit, 3) == round(1.825 - 0.225, 3)

    def test_put_side_roll_matches_hand_computed_figures(self):
        chain = _base_chain()
        roll = build_untested_side_roll(chain, side="put", short_target_delta=0.30, wing_target_delta=0.10)
        assert roll.side == "put"
        assert roll.new_short_strike == 756.0
        assert roll.new_wing_strike == 752.0
        assert roll.new_wing_width == 4.0
        # new short mid=(1.80+1.85)/2=1.825, new wing mid=(0.15+0.20)/2=0.175 (overridden in _base_chain)
        assert round(roll.roll_credit, 3) == round(1.825 - 0.175, 3)

    def test_raises_if_roll_credit_would_leave_non_positive_max_loss(self):
        legs = [
            LegQuote(strike=761.0, symbol="short", delta=0.40, bid=2.00, ask=2.05),
            LegQuote(strike=762.0, symbol="wing", delta=0.10, bid=0.05, ask=0.10),
        ]
        chain = ChainSnapshot(underlying_price=760.0, calls=legs, puts=[])
        with pytest.raises(IronButterflyError, match="would leave zero or negative max loss"):
            build_untested_side_roll(chain, side="call", short_target_delta=0.30, wing_target_delta=0.10)

    def test_opposite_strike_forwarded_blocks_a_roll_that_would_cross_the_other_side(self):
        """End-to-end version of the Nov 5 2026 incident: without
        opposite_strike, this call-side roll would land on 764 (see
        test_call_side_roll_matches_hand_computed_figures). Passing the
        tested side's open short put (765) as opposite_strike must
        refuse the roll instead of building a crossed structure."""
        chain = _base_chain()
        with pytest.raises(IronButterflyError, match="under-targeted"):
            build_untested_side_roll(chain, side="call", short_target_delta=0.30, wing_target_delta=0.10,
                                      opposite_strike=765.0)

    def test_opposite_strike_defaults_to_none_and_does_not_change_existing_behavior(self):
        chain = _base_chain()
        roll = build_untested_side_roll(chain, side="call", short_target_delta=0.30, wing_target_delta=0.10)
        assert roll.new_short_strike == 764.0
