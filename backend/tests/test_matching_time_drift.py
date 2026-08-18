"""Layer 2 -- time drift.

The rule under test: same amount, clocks up to 60s apart, still a match. The money
moved; only the clocks disagree. It is tagged so the match rate stays honest about
*how* it was reached, but it counts as matched, because a payment that reconciles 40
seconds late is not a break.
"""

from __future__ import annotations

import pytest

from ledgerloop.db.enums import MatchLayer, ReconStatus
from ledgerloop.matching.core import decide
from tests.factories import DEFAULT_CONFIG, gateway, ledger


@pytest.mark.parametrize("offset", [2.001, 5.0, 30.0, 59.999, 60.0])
def test_drift_inside_sixty_seconds_still_matches(offset):
    decision = decide(gateway(), [ledger(offset_s=offset)], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.status is ReconStatus.MATCHED
    assert decision.layer is MatchLayer.TIME_DRIFT


@pytest.mark.parametrize("offset", [-2.001, -30.0, -60.0])
def test_drift_is_symmetric_around_zero(offset):
    """The ledger clock may run early or late; +-60s means either direction."""
    decision = decide(gateway(), [ledger(offset_s=offset)], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.layer is MatchLayer.TIME_DRIFT


@pytest.mark.parametrize("offset", [60.001, 61.0, 3600.0, -60.001])
def test_beyond_sixty_seconds_defers_to_the_sweeper(offset):
    """Not a match and not a break -- the counterparty may simply be late. Deciding
    here would pre-empt the sweeper, which is the component that owns 'too late'."""
    assert decide(gateway(), [ledger(offset_s=offset)], DEFAULT_CONFIG) is None


def test_time_drift_does_not_open_an_exception():
    decision = decide(gateway(), [ledger(offset_s=45.0)], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.opens_exception is False


def test_time_drift_records_the_measured_drift_in_notes():
    decision = decide(gateway(), [ledger(offset_s=45.5)], DEFAULT_CONFIG)
    assert decision is not None and decision.notes is not None
    assert "45.500" in decision.notes


def test_time_drift_counts_as_a_match():
    decision = decide(gateway(), [ledger(offset_s=59.0)], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.is_match is True
