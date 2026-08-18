"""The stream wire format. Serialisation bugs here would be invisible in a unit test of
the matcher and catastrophic in production, so the round trips are asserted directly."""

from __future__ import annotations

from ledgerloop.db.enums import IngestSource
from ledgerloop.queue.messages import FIELD, IngestMessage


def test_fields_round_trip():
    message = IngestMessage(IngestSource.GATEWAY, 42, "TXN-9", is_duplicate=True, submissions=3)
    assert IngestMessage.from_fields(message.to_fields()) == message


def test_payload_round_trip():
    message = IngestMessage(IngestSource.LEDGER, 7, "TXN-3")
    assert IngestMessage.from_payload(message.to_payload()) == message


def test_fields_are_flat_strings_as_redis_requires():
    fields = IngestMessage(IngestSource.GATEWAY, 1, "TXN-1").to_fields()
    assert set(fields) == {FIELD}
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in fields.items())


def test_bytes_fields_decode():
    """redis-py returns bytes when decode_responses is off; the consumer must not care."""
    message = IngestMessage(IngestSource.GATEWAY, 5, "TXN-5")
    raw = {key.encode(): value.encode() for key, value in message.to_fields().items()}
    assert IngestMessage.from_fields(raw) == message


def test_defaults_are_not_duplicates():
    message = IngestMessage(IngestSource.LEDGER, 1, "TXN-1")
    assert message.is_duplicate is False
    assert message.submissions == 1


def test_message_carries_a_pointer_not_the_amounts():
    """The worker re-reads the row from Postgres, so a schema change never strands
    in-flight messages carrying stale copies of the money."""
    payload = IngestMessage(IngestSource.GATEWAY, 1, "TXN-1").to_payload()
    assert "amount" not in payload
    assert "occurred_at" not in payload
    assert payload["row_id"] == 1
