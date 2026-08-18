"""Edge validation. These invariants are enforced twice -- here and by the database --
and this suite is what proves the edge layer actually rejects rather than merely
documenting that it should."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ledgerloop.api.schemas import GatewayWebhookIn, LedgerEntryIn, LedgerSyncIn

VALID_GATEWAY = {
    "txn_id": "TXN-1",
    "amount": "1000.00",
    "currency": "INR",
    "occurred_at": "2026-03-14T09:30:00+00:00",
    "gateway_ref": "REF-1",
}


def _ledger(**overrides: object) -> dict[str, object]:
    base = {
        "entry_id": "LEDG-1",
        "txn_id": "TXN-1",
        "amount": "1000.00",
        "currency": "INR",
        "occurred_at": "2026-03-14T09:30:00+00:00",
        "idempotency_key": "ld-1",
    }
    base.update(overrides)
    return base


def test_valid_gateway_payload_parses():
    model = GatewayWebhookIn(**VALID_GATEWAY)
    assert model.amount == Decimal("1000.00")
    assert model.occurred_at == datetime(2026, 3, 14, 9, 30, tzinfo=UTC)


def test_amount_is_decimal_not_float():
    """If this ever comes back as a float, every downstream comparison inherits binary
    rounding error and money starts disagreeing with itself."""
    assert isinstance(GatewayWebhookIn(**VALID_GATEWAY).amount, Decimal)


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValidationError):
        GatewayWebhookIn(**{**VALID_GATEWAY, "occurred_at": "2026-03-14T09:30:00"})


def test_non_utc_offset_is_accepted_and_kept_aware():
    model = GatewayWebhookIn(**{**VALID_GATEWAY, "occurred_at": "2026-03-14T15:00:00+05:30"})
    assert model.occurred_at.utcoffset() is not None
    assert model.occurred_at == datetime(2026, 3, 14, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["inr", "Rs", "INRR", "IN", "1NR", ""])
def test_bad_currency_is_rejected(bad):
    with pytest.raises(ValidationError):
        GatewayWebhookIn(**{**VALID_GATEWAY, "currency": bad})


def test_three_decimal_places_is_rejected_not_silently_rounded():
    with pytest.raises(ValidationError):
        GatewayWebhookIn(**{**VALID_GATEWAY, "amount": "100.005"})


def test_zero_amount_is_rejected():
    with pytest.raises(ValidationError):
        GatewayWebhookIn(**{**VALID_GATEWAY, "amount": "0.00"})


def test_negative_amount_is_allowed_for_refunds():
    assert GatewayWebhookIn(**{**VALID_GATEWAY, "amount": "-500.00"}).amount == Decimal("-500.00")


def test_unknown_field_is_rejected():
    """A client sending `ammount` gets told, instead of having it silently ignored and
    a default reconciled in its place."""
    with pytest.raises(ValidationError):
        GatewayWebhookIn(**{**VALID_GATEWAY, "ammount": "10.00"})


def test_missing_required_field_is_rejected():
    payload = dict(VALID_GATEWAY)
    del payload["gateway_ref"]
    with pytest.raises(ValidationError):
        GatewayWebhookIn(**payload)


def test_amount_over_eighteen_digits_is_rejected():
    with pytest.raises(ValidationError):
        GatewayWebhookIn(**{**VALID_GATEWAY, "amount": "12345678901234567.00"})


def test_ledger_entry_requires_its_own_idempotency_key():
    payload = _ledger()
    del payload["idempotency_key"]
    with pytest.raises(ValidationError):
        LedgerEntryIn(**payload)


def test_ledger_batch_of_one_thousand_is_accepted():
    batch = LedgerSyncIn(entries=[_ledger(idempotency_key=f"ld-{i}") for i in range(1000)])
    assert len(batch.entries) == 1000


def test_ledger_batch_over_one_thousand_is_rejected():
    with pytest.raises(ValidationError):
        LedgerSyncIn(entries=[_ledger(idempotency_key=f"ld-{i}") for i in range(1001)])


def test_empty_ledger_batch_is_rejected():
    with pytest.raises(ValidationError):
        LedgerSyncIn(entries=[])
