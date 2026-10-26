"""
fees.py -- Tastytrade's commission/fee schedule for equity options,
as it applies to a 4-leg iron butterfly. Verified 2026-10-26 two ways:
the official published schedule (tastytrade.com/pricing) and real
dry-run fee-calculation data from the sandbox at 1/8/10/12/20
contracts, which pinned the per-leg cap exactly at 10 contracts.

THIS SCHEDULE CAN CHANGE. These are the rates observed/documented as
of 2026-10-26 -- re-verify against a real dry-run (same technique
used to build this) before trusting it for anything that matters.

Rates (per the official page + confirmed against real dry-run data):
  - Commission: $1.00 per contract per leg to OPEN, $0 to CLOSE,
    capped at $10.00 per leg. For 4 legs: entry commission =
    4 x min(contracts, 10) dollars; exit commission = $0.
  - Clearing fee: confirmed exactly linear in the real data --
    $0.10 per contract per leg (4 legs => $0.40/contract/order), no
    cap observed up to 20 contracts. Applies on BOTH entry and exit
    (the official page: "applicable ... clearing ... fees still apply
    to all opening and closing trades").
  - Regulatory fees (ORF + TAF combined): approximately $0.082 per
    contract per order -- NOT perfectly linear in the real data
    (observed 0.0806-0.0822/contract across 1/8/10/12/20 contracts),
    because the real ORF/TAF components round at their own small
    increments. Close enough to model as linear; don't expect it to
    reconcile to the cent against a real statement.

These are intentionally kept as a separate, named module (not buried
as magic numbers in iron_butterfly.py) so the one place this schedule
lives is obvious, and so it can be re-verified/updated without
touching the economics logic that consumes it.
"""

ENTRY_COMMISSION_PER_CONTRACT_PER_LEG = 1.00
ENTRY_COMMISSION_CAP_PER_LEG = 10.00
EXIT_COMMISSION_PER_CONTRACT_PER_LEG = 0.00  # closing is free

CLEARING_FEE_PER_CONTRACT_PER_LEG = 0.10        # applies on entry AND exit
REGULATORY_FEE_PER_CONTRACT_PER_ORDER = 0.082   # approximate; applies on entry AND exit

LEGS_PER_IRON_BUTTERFLY = 4


def entry_fees(contracts: int, legs: int = LEGS_PER_IRON_BUTTERFLY) -> float:
    """Total real-dollar cost to OPEN the structure at this size."""
    commission_per_leg = min(contracts * ENTRY_COMMISSION_PER_CONTRACT_PER_LEG,
                              ENTRY_COMMISSION_CAP_PER_LEG)
    commission = commission_per_leg * legs
    clearing = CLEARING_FEE_PER_CONTRACT_PER_LEG * contracts * legs
    regulatory = REGULATORY_FEE_PER_CONTRACT_PER_ORDER * contracts
    return round(commission + clearing + regulatory, 4)


def exit_fees(contracts: int, legs: int = LEGS_PER_IRON_BUTTERFLY) -> float:
    """Total real-dollar cost to CLOSE the structure at this size --
    commission is $0, but clearing/regulatory fees still apply."""
    commission = EXIT_COMMISSION_PER_CONTRACT_PER_LEG * contracts * legs
    clearing = CLEARING_FEE_PER_CONTRACT_PER_LEG * contracts * legs
    regulatory = REGULATORY_FEE_PER_CONTRACT_PER_ORDER * contracts
    return round(commission + clearing + regulatory, 4)


def round_trip_fees(contracts: int, legs: int = LEGS_PER_IRON_BUTTERFLY) -> float:
    return round(entry_fees(contracts, legs) + exit_fees(contracts, legs), 4)
