"""Layer 1 -- exact match, and the things that disqualify a pair entirely."""

from __future__ import annotations

import pytest

from ledgerloop.db.enums import MatchLayer, ReconStatus
from ledgerloop.matching.core import classify_pair, decide
from tests.factories import DEFAULT_CONFIG, gateway, ledger


def test_identical_pair_matches_exactly():
    decision = decide(gateway(), [ledger()], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.status is ReconStatus.MATCHED
    assert decision.layer is MatchLayer.EXACT
    assert decision.gateway_row_id == 1
    assert decision.ledger_row_id == 2
    assert decision.opens_exception is False


def test_exact_match_is_symmetric_from_the_ledger_side():
    """Whichever side arrives second does the matching, so both directions must agree."""
    from_gateway = decide(gateway(), [ledger()], DEFAULT_CONFIG)
    from_ledger = decide(ledger(), [gateway()], DEFAULT_CONFIG)
    assert from_gateway == from_ledger


@pytest.mark.parametrize("offset", [0.0, 0.5, 1.0, 1.999, 2.0, -2.0, -1.5])
def test_within_two_seconds_inclusive_is_exact(offset):
    decision = decide(gateway(), [ledger(offset_s=offset)], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.layer is MatchLayer.EXACT


@pytest.mark.parametrize("offset", [2.001, -2.001, 3.0])
def test_just_past_two_seconds_is_no_longer_exact(offset):
    decision = decide(gateway(), [ledger(offset_s=offset)], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.layer is MatchLayer.TIME_DRIFT


def test_different_txn_id_never_pairs():
    assert decide(gateway(), [ledger(txn_id="TXN-OTHER")], DEFAULT_CONFIG) is None


def test_different_currency_never_pairs_even_when_everything_else_agrees():
    """Pairing 1000 USD with 1000 INR would report a reconciled payment that never
    happened. A break is the correct, honest outcome."""
    assert decide(gateway(currency="INR"), [ledger(currency="USD")], DEFAULT_CONFIG) is None


def test_same_side_rows_never_pair():
    """Two gateway rows are not a reconciliation, however identical they look."""
    assert classify_pair(gateway(row_id=1), gateway(row_id=9), DEFAULT_CONFIG) is None


def test_empty_counterparty_list_defers():
    assert decide(gateway(), [], DEFAULT_CONFIG) is None


def test_amount_difference_of_one_paisa_is_not_an_exact_match():
    decision = decide(gateway(amount="1000.00"), [ledger(amount="1000.01")], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.layer is MatchLayer.AMOUNT_DRIFT


def test_negative_amounts_match_exactly():
    """Refunds and reversals reconcile like anything else."""
    decision = decide(gateway(amount="-2500.00"), [ledger(amount="-2500.00")], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.layer is MatchLayer.EXACT


def test_exact_match_carries_no_notes():
    decision = decide(gateway(), [ledger()], DEFAULT_CONFIG)
    assert decision is not None
    assert decision.notes is None


def test_decide_does_not_mutate_its_inputs():
    candidate = gateway()
    counterparties = [ledger()]
    snapshot = (candidate, list(counterparties))
    decide(candidate, counterparties, DEFAULT_CONFIG)
    assert (candidate, counterparties) == snapshot
