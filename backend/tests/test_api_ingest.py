"""Ingestion endpoints: contracts, idempotency, and the outbox write."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from ledgerloop.db.models import GatewayTransaction, LedgerEntry, OutboxEvent
from tests.helpers import gateway_payload, ledger_payload, now, post_gateway, post_ledger


async def _count(session, model) -> int:  # type: ignore[no-untyped-def]
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# --- gateway webhook -------------------------------------------------------
async def test_gateway_webhook_returns_202(api):
    response = await post_gateway(api, gateway_payload())
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["result"]["duplicate"] is False
    assert body["result"]["submissions"] == 1


async def test_gateway_webhook_persists_the_row(api, session):
    await post_gateway(api, gateway_payload("TXN-A", "1234.56"))
    row = (await session.execute(select(GatewayTransaction))).scalar_one()
    assert row.txn_id == "TXN-A"
    assert str(row.amount) == "1234.56"
    assert row.occurred_at.tzinfo is not None
    assert row.reconciled_at is None
    assert row.duplicate_count == 0


async def test_gateway_webhook_stores_the_verbatim_payload(api, session):
    payload = gateway_payload("TXN-RAW")
    await post_gateway(api, payload)
    row = (await session.execute(select(GatewayTransaction))).scalar_one()
    assert row.raw_payload == payload


async def test_gateway_webhook_writes_exactly_one_outbox_event(api, session):
    await post_gateway(api, gateway_payload())
    event = (await session.execute(select(OutboxEvent))).scalar_one()
    assert event.published_at is None
    assert event.payload["source"] == "gateway"
    assert event.payload["is_duplicate"] is False


async def test_missing_idempotency_key_is_rejected(api):
    response = await api.post("/v1/gateway/webhook", json=gateway_payload())
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["detail"]


async def test_blank_idempotency_key_is_rejected(api):
    response = await api.post(
        "/v1/gateway/webhook", json=gateway_payload(), headers={"Idempotency-Key": "   "}
    )
    assert response.status_code == 400


async def test_invalid_body_is_422_not_500(api):
    response = await post_gateway(api, {**gateway_payload(), "amount": "not-a-number"})
    assert response.status_code == 422


async def test_naive_timestamp_is_rejected_at_the_edge(api):
    payload = {**gateway_payload(), "occurred_at": "2026-03-14T09:30:00"}
    assert (await post_gateway(api, payload)).status_code == 422


# --- idempotency: the core guarantee --------------------------------------
async def test_same_request_twice_creates_exactly_one_row(api, session):
    payload = gateway_payload("TXN-DUP")
    first = await post_gateway(api, payload)
    second = await post_gateway(api, payload)

    assert first.status_code == second.status_code == 202
    assert first.json()["result"]["duplicate"] is False
    assert second.json()["result"]["duplicate"] is True
    assert await _count(session, GatewayTransaction) == 1


async def test_duplicate_returns_202_not_409(api):
    """Clients retry on 5xx and on network errors. Answering a successful retry with an
    error makes them retry the retry; 202 lets the retry succeed silently."""
    payload = gateway_payload("TXN-RETRY")
    await post_gateway(api, payload)
    assert (await post_gateway(api, payload)).status_code == 202


async def test_duplicate_returns_the_original_row_id(api):
    payload = gateway_payload("TXN-SAME-ID")
    first = await post_gateway(api, payload)
    second = await post_gateway(api, payload)
    assert first.json()["result"]["row_id"] == second.json()["result"]["row_id"]


async def test_duplicate_count_tracks_submissions(api, session):
    payload = gateway_payload("TXN-COUNT")
    for _ in range(4):
        await post_gateway(api, payload)
    row = (await session.execute(select(GatewayTransaction))).scalar_one()
    assert row.duplicate_count == 3  # four submissions, three of them retries


async def test_submissions_is_reported_back_to_the_client(api):
    payload = gateway_payload("TXN-REPORT")
    await post_gateway(api, payload)
    third = await post_gateway(api, payload)
    assert third.json()["result"]["submissions"] == 2


async def test_retry_does_not_overwrite_the_original_row(api, session):
    """First receipt is the record of what arrived. A retry with a mutated body must
    not rewrite history -- otherwise a buggy client could restate a settled amount."""
    await post_gateway(api, gateway_payload("TXN-IMM", "1000.00"), key="fixed-key")
    await post_gateway(api, gateway_payload("TXN-IMM", "9999.99"), key="fixed-key")
    row = (await session.execute(select(GatewayTransaction))).scalar_one()
    assert str(row.amount) == "1000.00"


async def test_different_keys_create_different_rows(api, session):
    await post_gateway(api, gateway_payload("TXN-X"), key="key-1")
    await post_gateway(api, gateway_payload("TXN-X"), key="key-2")
    assert await _count(session, GatewayTransaction) == 2


async def test_each_submission_enqueues_its_own_event(api, session):
    """The retry must reach the matcher too -- that is what produces the `duplicate`
    reconciliation result rather than a silently swallowed request."""
    payload = gateway_payload("TXN-EVENTS")
    await post_gateway(api, payload)
    await post_gateway(api, payload)
    assert await _count(session, OutboxEvent) == 2
    events = (await session.execute(select(OutboxEvent).order_by(OutboxEvent.id))).scalars().all()
    assert [event.payload["is_duplicate"] for event in events] == [False, True]


# --- ledger sync -----------------------------------------------------------
async def test_ledger_sync_accepts_a_batch(api, session):
    response = await post_ledger(
        api,
        ledger_payload("TXN-L1", idempotency_key="ld-1"),
        ledger_payload("TXN-L2", idempotency_key="ld-2"),
    )
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] == 2
    assert body["duplicates"] == 0
    assert len(body["results"]) == 2
    assert await _count(session, LedgerEntry) == 2


async def test_ledger_sync_is_idempotent_across_calls(api, session):
    entry = ledger_payload("TXN-LDUP", idempotency_key="ld-dup")
    await post_ledger(api, entry)
    second = await post_ledger(api, entry)
    assert second.json()["duplicates"] == 1
    assert second.json()["accepted"] == 0
    assert await _count(session, LedgerEntry) == 1


async def test_ledger_sync_handles_a_repeated_key_inside_one_batch(api, session):
    """A client merging two pages of its own retry queue can legitimately send the same
    key twice in one request. One row, and the repeat counted as a duplicate."""
    entry = ledger_payload("TXN-INNER", idempotency_key="ld-inner")
    response = await post_ledger(api, entry, entry)
    assert await _count(session, LedgerEntry) == 1
    assert response.json()["duplicates"] >= 1
    row = (await session.execute(select(LedgerEntry))).scalar_one()
    assert row.duplicate_count == 1


async def test_ledger_sync_results_follow_request_order(api):
    response = await post_ledger(
        api,
        ledger_payload("TXN-O1", idempotency_key="ld-o1"),
        ledger_payload("TXN-O2", idempotency_key="ld-o2"),
        ledger_payload("TXN-O3", idempotency_key="ld-o3"),
    )
    assert [item["txn_id"] for item in response.json()["results"]] == [
        "TXN-O1",
        "TXN-O2",
        "TXN-O3",
    ]


async def test_ledger_sync_writes_one_outbox_event_per_entry(api, session):
    await post_ledger(
        api,
        ledger_payload("TXN-E1", idempotency_key="ld-e1"),
        ledger_payload("TXN-E2", idempotency_key="ld-e2"),
    )
    assert await _count(session, OutboxEvent) == 2


async def test_ledger_batch_of_one_thousand_is_accepted(api, session):
    entries = [
        ledger_payload(f"TXN-B{i}", idempotency_key=f"ld-b{i}", entry_id=f"E{i}")
        for i in range(1000)
    ]
    response = await api.post("/v1/ledger/sync", json={"entries": entries})
    assert response.status_code == 202
    assert await _count(session, LedgerEntry) == 1000


async def test_ledger_batch_over_the_cap_is_rejected(api, session):
    entries = [
        ledger_payload(f"TXN-C{i}", idempotency_key=f"ld-c{i}", entry_id=f"E{i}")
        for i in range(1001)
    ]
    response = await api.post("/v1/ledger/sync", json={"entries": entries})
    assert response.status_code == 422
    # Rejected wholesale: not one row of an oversized batch may land.
    assert await _count(session, LedgerEntry) == 0


async def test_empty_batch_is_rejected(api):
    assert (await api.post("/v1/ledger/sync", json={"entries": []})).status_code == 422


@pytest.mark.parametrize("bad_currency", ["inr", "RUPEE", "IN"])
async def test_ledger_entry_currency_is_validated(api, bad_currency):
    response = await post_ledger(
        api, ledger_payload("TXN-BAD", currency=bad_currency, idempotency_key="ld-bad")
    )
    assert response.status_code == 422


async def test_ledger_timestamps_keep_their_offset(api, session):
    occurred = now() - timedelta(minutes=5)
    await post_ledger(api, ledger_payload("TXN-TZ", occurred_at=occurred, idempotency_key="ld-tz"))
    row = (await session.execute(select(LedgerEntry))).scalar_one()
    assert row.occurred_at.tzinfo is not None
    assert abs((row.occurred_at - occurred).total_seconds()) < 0.001
