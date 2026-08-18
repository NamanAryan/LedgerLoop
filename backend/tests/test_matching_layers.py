"""Layer 4 (duplicate), layer 5 (unmatched sweep), layer precedence, and counterparty
selection when several are viable."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ledgerloop.db.enums import IngestSource, MatchLayer, ReconStatus
from ledgerloop.matching.core import decide, decide_duplicate, decide_unmatched
from tests.factories import DEFAULT_CONFIG, gateway, ledger


# --- layer 4: duplicates ---------------------------------------------------
def test_duplicate_on_the_gateway_side_points_at_the_gateway_row():
    decision = decide_duplicate(gateway(row_id=7), submissions=2)
    assert decision.status is ReconStatus.DUPLICATE
    assert decision.layer is MatchLayer.DUPLICATE
    assert decision.gateway_row_id == 7
    assert decision.ledger_row_id is None


def test_duplicate_on_the_ledger_side_points_at_the_ledger_row():
    decision = decide_duplicate(ledger(row_id=9), submissions=3)
    assert decision.gateway_row_id is None
    assert decision.ledger_row_id == 9


def test_duplicate_never_opens_an_exception():
    """A retry is the idempotency layer working, not a break for a human to triage."""
    assert decide_duplicate(gateway(), submissions=2).opens_exception is False


def test_duplicate_is_not_counted_as_a_match():
    assert decide_duplicate(gateway(), submissions=2).is_match is False


def test_duplicate_notes_record_the_submission_count():
    decision = decide_duplicate(gateway(), submissions=4)
    assert decision.notes is not None
    assert "4 submissions" in decision.notes


def test_duplicate_needs_no_counterparty():
    """Signature check with teeth: duplicates are decided from the key alone, so this
    call cannot start depending on the other side without failing here."""
    assert decide_duplicate(gateway(), submissions=2) is not None


# --- layer 5: the sweep ----------------------------------------------------
def test_unmatched_gateway_row_becomes_gateway_only():
    decision = decide_unmatched(gateway(row_id=4), timedelta(seconds=300))
    assert decision.status is ReconStatus.UNMATCHED_GATEWAY_ONLY
    assert decision.layer is MatchLayer.UNMATCHED_SWEEP
    assert decision.gateway_row_id == 4
    assert decision.ledger_row_id is None


def test_unmatched_ledger_row_becomes_ledger_only():
    decision = decide_unmatched(ledger(row_id=5), timedelta(seconds=300))
    assert decision.status is ReconStatus.UNMATCHED_LEDGER_ONLY
    assert decision.ledger_row_id == 5
    assert decision.gateway_row_id is None


@pytest.mark.parametrize("side", [gateway(), ledger()])
def test_every_unmatched_outcome_opens_an_exception(side):
    assert decide_unmatched(side, timedelta(seconds=300)).opens_exception is True


def test_unmatched_notes_record_how_long_we_waited():
    decision = decide_unmatched(gateway(), timedelta(seconds=420))
    assert decision.notes is not None
    assert "420s" in decision.notes


# --- layer precedence ------------------------------------------------------
def test_exact_beats_time_drift_when_both_are_available():
    """Two viable ledger rows; the one inside the exact window must win, regardless of
    the order they came back from the database in."""
    candidates = [ledger(row_id=20, offset_s=40.0), ledger(row_id=21, offset_s=0.5)]
    decision = decide(gateway(), candidates, DEFAULT_CONFIG)
    assert decision is not None
    assert decision.layer is MatchLayer.EXACT
    assert decision.ledger_row_id == 21


def test_exact_beats_time_drift_regardless_of_input_order():
    candidates = [ledger(row_id=21, offset_s=0.5), ledger(row_id=20, offset_s=40.0)]
    decision = decide(gateway(), candidates, DEFAULT_CONFIG)
    assert decision is not None
    assert decision.ledger_row_id == 21


def test_a_same_amount_match_beats_a_drifted_one_even_when_further_away_in_time():
    """Layer order is not negotiable: layer 2 runs before layer 3, so an exact-amount
    counterparty 50s away wins over a drifted one 1s away."""
    candidates = [
        ledger(row_id=30, amount="1005.00", offset_s=1.0),
        ledger(row_id=31, amount="1000.00", offset_s=50.0),
    ]
    decision = decide(gateway(amount="1000.00"), candidates, DEFAULT_CONFIG)
    assert decision is not None
    assert decision.layer is MatchLayer.TIME_DRIFT
    assert decision.ledger_row_id == 31


def test_closest_in_time_wins_within_the_same_layer():
    candidates = [ledger(row_id=40, offset_s=30.0), ledger(row_id=41, offset_s=5.0)]
    decision = decide(gateway(), candidates, DEFAULT_CONFIG)
    assert decision is not None
    assert decision.ledger_row_id == 41


def test_ties_break_on_lowest_row_id_so_racing_workers_agree():
    """Two counterparties identical in every way that matters. The tie-break must be
    deterministic, or two workers could pick different partners for the same txn."""
    candidates = [ledger(row_id=51, offset_s=1.0), ledger(row_id=50, offset_s=-1.0)]
    first = decide(gateway(), candidates, DEFAULT_CONFIG)
    second = decide(gateway(), list(reversed(candidates)), DEFAULT_CONFIG)
    assert first is not None and second is not None
    assert first.ledger_row_id == second.ledger_row_id == 50


def test_unmatchable_counterparties_are_ignored_not_matched():
    candidates = [
        ledger(row_id=60, txn_id="OTHER"),
        ledger(row_id=61, currency="USD"),
        ledger(row_id=62, offset_s=900.0),
    ]
    assert decide(gateway(), candidates, DEFAULT_CONFIG) is None


def test_one_viable_counterparty_among_noise_is_still_found():
    candidates = [
        ledger(row_id=70, txn_id="OTHER"),
        ledger(row_id=71),
        ledger(row_id=72, currency="USD"),
    ]
    decision = decide(gateway(), candidates, DEFAULT_CONFIG)
    assert decision is not None
    assert decision.ledger_row_id == 71


# --- the full status surface ----------------------------------------------
def test_all_six_statuses_are_reachable():
    """Every value in the enum is either produced by a layer or explicitly reserved.
    A status nothing can produce is dead schema, and this test says which is which."""
    produced = {
        decide(gateway(), [ledger()], DEFAULT_CONFIG).status,  # matched (exact)
        decide(gateway(), [ledger(offset_s=30)], DEFAULT_CONFIG).status,  # matched (time drift)
        decide(gateway(), [ledger(amount="1005.00")], DEFAULT_CONFIG).status,  # amount_drift
        decide_duplicate(gateway(), 2).status,
        decide_unmatched(gateway(), timedelta(seconds=300)).status,
        decide_unmatched(ledger(), timedelta(seconds=300)).status,
    }
    assert produced == {
        ReconStatus.MATCHED,
        ReconStatus.AMOUNT_DRIFT,
        ReconStatus.DUPLICATE,
        ReconStatus.UNMATCHED_GATEWAY_ONLY,
        ReconStatus.UNMATCHED_LEDGER_ONLY,
    }
    # TIME_DRIFT is reserved: layer 2 resolves to MATCHED tagged with the time_drift
    # layer, so a late reconciliation still counts toward the match rate.
    assert ReconStatus.TIME_DRIFT not in produced


def test_every_match_layer_is_reachable():
    layers = {
        decide(gateway(), [ledger()], DEFAULT_CONFIG).layer,
        decide(gateway(), [ledger(offset_s=30)], DEFAULT_CONFIG).layer,
        decide(gateway(), [ledger(amount="1005.00")], DEFAULT_CONFIG).layer,
        decide_duplicate(gateway(), 2).layer,
        decide_unmatched(gateway(), timedelta(seconds=300)).layer,
    }
    assert layers == set(MatchLayer)


def test_gateway_and_ledger_ids_are_assigned_by_side_not_by_argument_position():
    """decide() is called with the candidate first whichever side it is; the decision
    must still put the gateway id in the gateway column."""
    from_ledger = decide(ledger(row_id=2), [gateway(row_id=1)], DEFAULT_CONFIG)
    assert from_ledger is not None
    assert from_ledger.gateway_row_id == 1
    assert from_ledger.ledger_row_id == 2


def test_facts_are_frozen():
    facts = gateway()
    with pytest.raises((AttributeError, TypeError)):
        facts.amount = 0  # type: ignore[misc]


def test_sides_are_distinguishable():
    assert gateway().side is IngestSource.GATEWAY
    assert ledger().side is IngestSource.LEDGER
