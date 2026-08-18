"""Layer 3 -- amount drift.

Tolerance is ``max(1% of the gateway amount, 10.00)``, so the flat floor governs small
tickets and the percentage governs large ones. The crossover is at 1000.00, and both
sides of it are tested here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ledgerloop.db.enums import MatchLayer, ReconStatus
from ledgerloop.matching.core import amount_tolerance, decide
from tests.factories import DEFAULT_CONFIG, gateway, ledger


# --- the tolerance function itself ----------------------------------------
@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("100.00", "10.00"),  # 1% = 1.00, floor wins
        ("500.00", "10.00"),  # 1% = 5.00, floor wins
        ("1000.00", "10.00"),  # crossover: both arms equal
        ("2000.00", "20.00"),  # 1% = 20.00, percentage wins
        ("50000.00", "500.00"),
    ],
)
def test_tolerance_is_the_larger_of_one_percent_and_ten(reference, expected):
    assert amount_tolerance(Decimal(reference), DEFAULT_CONFIG) == Decimal(expected)


def test_tolerance_uses_magnitude_so_refunds_get_the_same_band():
    assert amount_tolerance(Decimal("-2000.00"), DEFAULT_CONFIG) == Decimal("20.00")


def test_tolerance_is_exact_decimal_not_float():
    """0.01 as a binary float is 0.010000000000000000208..., so a float tolerance would
    make a difference of exactly 20.00 fall outside 1% of 2000.00. With Decimal the
    boundary is exact, and a payment on the line is classified the same way every time.
    (Scale may be 20.0000 rather than 20.00; Decimal equality compares value, not scale.)
    """
    assert amount_tolerance(Decimal("1000.00"), DEFAULT_CONFIG) == Decimal("10.00")
    assert amount_tolerance(Decimal("2000.00"), DEFAULT_CONFIG) == Decimal("20.00")
    assert abs(Decimal("2000.00") - Decimal("1980.00")) <= amount_tolerance(
        Decimal("2000.00"), DEFAULT_CONFIG
    )


# --- classification --------------------------------------------------------
@pytest.mark.parametrize("ledger_amount", ["990.00", "1009.99", "1010.00", "1000.01", "999.99"])
def test_within_tolerance_is_amount_drift(ledger_amount):
    decision = decide(gateway(amount="1000.00"), [ledger(amount=ledger_amount)], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.status is ReconStatus.AMOUNT_DRIFT
    assert decision.layer is MatchLayer.AMOUNT_DRIFT


@pytest.mark.parametrize("ledger_amount", ["1010.01", "989.99", "1500.00", "0.01"])
def test_beyond_tolerance_is_not_a_match_at_all(ledger_amount):
    """Too far apart to call it drift. Deferring sends both sides to the sweeper, which
    reports two honest breaks rather than one invented pairing."""
    assert decide(gateway(amount="1000.00"), [ledger(amount=ledger_amount)], DEFAULT_CONFIG) is None


def test_small_ticket_uses_the_flat_floor():
    """1% of 50.00 is 0.50, but a 9.00 difference is still inside the 10.00 floor."""
    decision = decide(gateway(amount="50.00"), [ledger(amount="59.00")], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.status is ReconStatus.AMOUNT_DRIFT


def test_small_ticket_beyond_the_floor_defers():
    assert decide(gateway(amount="50.00"), [ledger(amount="61.00")], DEFAULT_CONFIG) is None


def test_large_ticket_uses_the_percentage():
    decision = decide(gateway(amount="50000.00"), [ledger(amount="50400.00")], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.status is ReconStatus.AMOUNT_DRIFT


def test_large_ticket_beyond_the_percentage_defers():
    assert decide(gateway(amount="50000.00"), [ledger(amount="50600.00")], DEFAULT_CONFIG) is None


def test_amount_drift_requires_the_time_window_too():
    """Right amount band, wrong clock: not drift, because a transaction two hours away
    is a different transaction that happens to share an id."""
    assert (
        decide(gateway(amount="1000.00"), [ledger(amount="1005.00", offset_s=120)], DEFAULT_CONFIG)
        is None
    )


@pytest.mark.parametrize("offset", [0.0, 2.0, 59.9, 60.0])
def test_amount_drift_spans_the_whole_sixty_second_window(offset):
    decision = decide(
        gateway(amount="1000.00"), [ledger(amount="1005.00", offset_s=offset)], DEFAULT_CONFIG
    )
    assert decision is not None
    assert decision.status is ReconStatus.AMOUNT_DRIFT


def test_amount_drift_opens_an_exception():
    decision = decide(gateway(amount="1000.00"), [ledger(amount="1005.00")], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.opens_exception is True
    assert decision.is_match is False


def test_amount_drift_notes_name_both_amounts_and_the_tolerance():
    decision = decide(gateway(amount="1000.00"), [ledger(amount="1005.00")], DEFAULT_CONFIG)
    assert decision is not None and decision.notes is not None
    assert "1000.00" in decision.notes
    assert "1005.00" in decision.notes
    assert "10.00" in decision.notes


def test_tolerance_is_measured_against_the_gateway_side():
    """The gateway is what the customer was actually charged, so the allowance must not
    stretch just because the ledger is the side that is wrong. Ledger 1200 vs gateway
    1000 is outside 1% of 1000 even though it is inside 1% of... nothing relevant."""
    assert decide(gateway(amount="1000.00"), [ledger(amount="1200.00")], DEFAULT_CONFIG) is None


def test_drift_direction_does_not_change_the_classification():
    short = decide(gateway(amount="1000.00"), [ledger(amount="995.00")], DEFAULT_CONFIG)
    long = decide(gateway(amount="1000.00"), [ledger(amount="1005.00")], DEFAULT_CONFIG)
    assert short is not None and long is not None
    assert short.status is long.status is ReconStatus.AMOUNT_DRIFT
