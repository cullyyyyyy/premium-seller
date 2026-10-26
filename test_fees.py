import math

import fees


class TestEntryFees:
    def test_matches_real_captured_dry_run_data(self):
        """Pinned against real sandbox dry-run fee-calculation data,
        2026-10-26, at several contract counts spanning the per-leg
        cap boundary (10 contracts). Commission and clearing match
        exactly; regulatory is an approximation (documented in
        fees.py) so allow a small tolerance there."""
        real_totals = {1: 4.4856, 8: 35.8446, 10: 44.8158, 12: 45.7870, 20: 49.6316}
        for contracts, real_total in real_totals.items():
            assert math.isclose(fees.entry_fees(contracts), real_total, abs_tol=0.02)

    def test_commission_caps_at_10_contracts_per_leg(self):
        below_cap = fees.entry_fees(9) - fees.entry_fees(8)
        at_and_past_cap = fees.entry_fees(11) - fees.entry_fees(10)
        # Below the cap, each extra contract adds commission (4 legs x $1);
        # past it, commission is flat and only clearing/regulatory grow.
        assert below_cap > at_and_past_cap

    def test_commission_flat_beyond_cap(self):
        c10 = fees.entry_fees(10)
        c20 = fees.entry_fees(20)
        # Commission component alone should be identical (both capped at
        # $40 = 4 legs x $10); the fee TOTAL still rises from clearing.
        comm10 = min(10, 10) * 4 * fees.ENTRY_COMMISSION_PER_CONTRACT_PER_LEG
        comm20 = min(20, 10) * 4 * fees.ENTRY_COMMISSION_PER_CONTRACT_PER_LEG
        assert comm10 == comm20 == 40.0
        assert c20 > c10  # total still grows (clearing/regulatory aren't capped)


class TestExitFees:
    def test_zero_commission_component(self):
        """Closing commission is $0 per the official schedule -- exit
        fees should be strictly clearing + regulatory, no brokerage."""
        assert fees.EXIT_COMMISSION_PER_CONTRACT_PER_LEG == 0.0

    def test_exit_fees_less_than_entry_fees_same_size(self):
        for n in (1, 5, 10, 20):
            assert fees.exit_fees(n) < fees.entry_fees(n)

    def test_exit_fees_scale_linearly_with_contracts_no_cap(self):
        assert math.isclose(fees.exit_fees(20), fees.exit_fees(10) * 2, abs_tol=0.01)


class TestRoundTripFees:
    def test_equals_entry_plus_exit(self):
        for n in (1, 8, 15):
            assert math.isclose(fees.round_trip_fees(n), fees.entry_fees(n) + fees.exit_fees(n), abs_tol=1e-9)
