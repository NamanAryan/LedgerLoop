"""The transactional outbox relay."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from ledgerloop.db.models import OutboxEvent
from ledgerloop.queue.relay import OutboxRelay
from tests.helpers import gateway_payload, ledger_payload, post_gateway, post_ledger


async def _unpublished(session) -> int:  # type: ignore[no-untyped-def]
    return (
        await session.execute(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))
        )
    ).scalar_one()


async def test_relay_publishes_outbox_rows_to_the_stream(api, sessions, stream, settings, session):
    await post_gateway(api, gateway_payload("TXN-R1"))
    relay = OutboxRelay(sessions, stream, settings)

    assert await relay.drain_once() == 1
    assert await stream.depth() == 1
    assert await _unpublished(session) == 0


async def test_relay_marks_rows_published(api, sessions, stream, settings, session):
    await post_gateway(api, gateway_payload("TXN-R2"))
    await OutboxRelay(sessions, stream, settings).drain_once()

    event = (await session.execute(select(OutboxEvent))).scalar_one()
    assert event.published_at is not None
    assert event.attempts == 1


async def test_relay_is_a_noop_when_the_outbox_is_empty(sessions, stream, settings):
    assert await OutboxRelay(sessions, stream, settings).drain_once() == 0


async def test_published_rows_are_not_republished(api, sessions, stream, settings):
    await post_gateway(api, gateway_payload("TXN-R3"))
    relay = OutboxRelay(sessions, stream, settings)

    assert await relay.drain_once() == 1
    assert await relay.drain_once() == 0
    assert await stream.depth() == 1


async def test_relay_publishes_a_whole_ledger_batch(api, sessions, stream, settings):
    await post_ledger(
        api, *[ledger_payload(f"TXN-B{i}", idempotency_key=f"ld-b{i}") for i in range(25)]
    )
    assert await OutboxRelay(sessions, stream, settings).drain_once() == 25
    assert await stream.depth() == 25


async def test_concurrent_relays_do_not_double_publish(api, sessions, stream, settings):
    """SKIP LOCKED under test: two relays draining at once must partition the work
    rather than both claiming the same rows."""
    await post_ledger(
        api, *[ledger_payload(f"TXN-C{i}", idempotency_key=f"ld-c{i}") for i in range(200)]
    )
    relay_a = OutboxRelay(sessions, stream, settings)
    relay_b = OutboxRelay(sessions, stream, settings)

    counts = await asyncio.gather(relay_a.drain_once(), relay_b.drain_once())

    assert sum(counts) == 200
    assert await stream.depth() == 200


async def test_relay_preserves_the_duplicate_flag(api, sessions, stream, settings):
    payload = gateway_payload("TXN-DUPFLAG")
    await post_gateway(api, payload)
    await post_gateway(api, payload)
    await OutboxRelay(sessions, stream, settings).drain_once()

    entries = await stream.read("relay-test", count=10, block_ms=50)
    assert [message.is_duplicate for _, message in entries] == [False, True]


async def test_relay_message_points_at_the_committed_row(api, sessions, stream, settings, session):
    response = await post_gateway(api, gateway_payload("TXN-PTR"))
    row_id = response.json()["result"]["row_id"]
    await OutboxRelay(sessions, stream, settings).drain_once()

    entries = await stream.read("relay-test", count=10, block_ms=50)
    assert entries[0][1].row_id == row_id
    assert entries[0][1].txn_id == "TXN-PTR"
