"""Two workers, one stream, 1000 messages, no double processing.

This is the test the whole design is arranged around. Two things have to hold at once:

* Redis must hand each entry to exactly one consumer in the group (work distribution).
* Even if it did not -- and after a crash it deliberately does not, because pending
  entries get claimed by a sibling -- the database must refuse the second write.

The assertions below check the *outcome*, not the mechanism: exactly one
reconciliation_result per transaction, no matter how the two workers interleaved.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from ledgerloop.db.enums import ReconStatus
from ledgerloop.db.models import ReconciliationResult
from ledgerloop.worker.loop import MatcherWorker
from tests.helpers import gateway_payload, ledger_payload, now, post_gateway, post_ledger
from tests.pipeline import consume_all, publish_all

PAIRS = 500  # 500 gateway + 500 ledger = 1000 stream messages


async def _ingest_pairs(api, count: int) -> None:  # type: ignore[no-untyped-def]
    occurred = now()
    for index in range(count):
        await post_gateway(api, gateway_payload(f"TXN-CC-{index}", occurred_at=occurred))
    await post_ledger(
        api,
        *[
            ledger_payload(
                f"TXN-CC-{index}", occurred_at=occurred, idempotency_key=f"ld-cc-{index}"
            )
            for index in range(count)
        ],
    )


async def test_two_workers_process_a_thousand_messages_exactly_once(
    api, sessions, stream, settings, session
):
    await _ingest_pairs(api, PAIRS)
    published = await publish_all(sessions, stream, settings)
    assert published == PAIRS * 2
    assert await stream.depth() == PAIRS * 2

    worker_a = MatcherWorker(sessions, stream, settings, "worker-a")
    worker_b = MatcherWorker(sessions, stream, settings, "worker-b")
    outcomes_a, outcomes_b = await asyncio.gather(
        consume_all(worker_a, stream), consume_all(worker_b, stream)
    )

    # Every message was delivered to exactly one of them.
    assert len(outcomes_a) + len(outcomes_b) == PAIRS * 2
    # Both actually did work, or this test would silently degrade into a single-worker
    # test the day the distribution breaks.
    assert outcomes_a and outcomes_b

    total_results = (
        await session.execute(select(func.count()).select_from(ReconciliationResult))
    ).scalar_one()
    assert total_results == PAIRS

    written = sum(1 for outcome in outcomes_a + outcomes_b if outcome.written)
    assert written == PAIRS

    assert await stream.pending_count() == 0


async def test_no_gateway_row_has_two_active_results(api, sessions, stream, settings, session):
    """The partial unique index stated as an assertion: one non-duplicate outcome per
    raw row, whatever the interleaving was."""
    await _ingest_pairs(api, 100)
    await publish_all(sessions, stream, settings)

    workers = [MatcherWorker(sessions, stream, settings, f"worker-{index}") for index in range(3)]
    await asyncio.gather(*(consume_all(worker, stream) for worker in workers))

    duplicated = (
        await session.execute(
            select(ReconciliationResult.gateway_txn_id, func.count())
            .where(ReconciliationResult.status != ReconStatus.DUPLICATE)
            .group_by(ReconciliationResult.gateway_txn_id)
            .having(func.count() > 1)
        )
    ).all()
    assert duplicated == []


async def test_concurrent_workers_on_the_same_transaction_write_once(
    api, sessions, stream, settings, session
):
    """Both sides of one transaction, deliberately handed to two workers at the same
    moment. Exactly one of them may win."""
    occurred = now()
    await post_gateway(api, gateway_payload("TXN-RACE", occurred_at=occurred))
    await post_ledger(api, ledger_payload("TXN-RACE", occurred_at=occurred))
    await publish_all(sessions, stream, settings)

    entries = await stream.read("splitter", count=10, block_ms=100)
    assert len(entries) == 2

    worker_a = MatcherWorker(sessions, stream, settings, "race-a")
    worker_b = MatcherWorker(sessions, stream, settings, "race-b")
    outcomes = await asyncio.gather(
        worker_a.process(entries[0][1], entries[0][0]),
        worker_b.process(entries[1][1], entries[1][0]),
    )

    total = (
        await session.execute(select(func.count()).select_from(ReconciliationResult))
    ).scalar_one()
    assert total == 1
    assert sum(1 for outcome in outcomes if outcome.written) == 1
