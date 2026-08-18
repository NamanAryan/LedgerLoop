"""The matcher worker against real Postgres and real Redis.

The theme of this file is at-least-once delivery: messages get redelivered, workers
race, and none of it may produce a second reconciliation_result.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from ledgerloop.db.enums import MatchLayer, ReconStatus
from ledgerloop.db.models import Exception_, GatewayTransaction, LedgerEntry, ReconciliationResult
from ledgerloop.worker.loop import MatcherWorker
from tests.helpers import gateway_payload, ingest_pair, ledger_payload, post_gateway, post_ledger
from tests.pipeline import consume_all, publish_all, run_pipeline


async def _results(session) -> list[ReconciliationResult]:  # type: ignore[no-untyped-def]
    return list(
        (await session.execute(select(ReconciliationResult).order_by(ReconciliationResult.id)))
        .scalars()
        .all()
    )


async def _count(session, model) -> int:  # type: ignore[no-untyped-def]
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# --- happy path ------------------------------------------------------------
async def test_matching_pair_produces_one_matched_result(api, sessions, stream, settings, session):
    await ingest_pair(api, "TXN-M1")
    await run_pipeline(sessions, stream, settings)

    results = await _results(session)
    assert len(results) == 1
    assert results[0].status is ReconStatus.MATCHED
    assert results[0].match_layer is MatchLayer.EXACT
    assert results[0].gateway_txn_id is not None
    assert results[0].ledger_entry_id is not None


async def test_match_marks_both_sides_reconciled(api, sessions, stream, settings, session):
    await ingest_pair(api, "TXN-M2")
    await run_pipeline(sessions, stream, settings)

    gateway_row = (await session.execute(select(GatewayTransaction))).scalar_one()
    ledger_row = (await session.execute(select(LedgerEntry))).scalar_one()
    assert gateway_row.reconciled_at is not None
    assert ledger_row.reconciled_at is not None


async def test_match_records_a_latency(api, sessions, stream, settings, session):
    await ingest_pair(api, "TXN-M3")
    await run_pipeline(sessions, stream, settings)

    result = (await session.execute(select(ReconciliationResult))).scalar_one()
    assert result.match_latency_ms is not None
    assert result.match_latency_ms >= 0


async def test_result_records_the_stream_message_id(api, sessions, stream, settings, session):
    await ingest_pair(api, "TXN-M4")
    await run_pipeline(sessions, stream, settings)

    result = (await session.execute(select(ReconciliationResult))).scalar_one()
    assert result.source_message_id is not None


async def test_gateway_alone_defers_rather_than_declaring_a_break(
    api, sessions, stream, settings, session
):
    """The ledger may simply be late. Only the sweeper is allowed to call it a break."""
    await post_gateway(api, gateway_payload("TXN-ALONE"))
    outcomes = await run_pipeline(sessions, stream, settings)

    assert [outcome.decision for outcome in outcomes] == [None]
    assert await _count(session, ReconciliationResult) == 0


async def test_second_side_arriving_later_completes_the_match(
    api, sessions, stream, settings, session
):
    occurred = await ingest_pair(api, "TXN-LATE-A")  # both sides
    await run_pipeline(sessions, stream, settings)

    # A second transaction whose ledger side arrives only after the gateway was
    # already processed and deferred.
    await post_gateway(api, gateway_payload("TXN-LATE-B", occurred_at=occurred))
    await run_pipeline(sessions, stream, settings)
    await post_ledger(api, ledger_payload("TXN-LATE-B", occurred_at=occurred))
    await run_pipeline(sessions, stream, settings)

    statuses = [result.status for result in await _results(session)]
    assert statuses == [ReconStatus.MATCHED, ReconStatus.MATCHED]


async def test_time_drift_is_matched_and_tagged(api, sessions, stream, settings, session):
    await ingest_pair(api, "TXN-TD", skew=timedelta(seconds=30))
    await run_pipeline(sessions, stream, settings)

    result = (await session.execute(select(ReconciliationResult))).scalar_one()
    assert result.status is ReconStatus.MATCHED
    assert result.match_layer is MatchLayer.TIME_DRIFT
    assert result.notes is not None and "drift" in result.notes


async def test_amount_drift_opens_an_exception(api, sessions, stream, settings, session):
    await ingest_pair(api, "TXN-AD", gateway_amount="1000.00", ledger_amount="1005.00")
    await run_pipeline(sessions, stream, settings)

    result = (await session.execute(select(ReconciliationResult))).scalar_one()
    assert result.status is ReconStatus.AMOUNT_DRIFT
    assert result.match_layer is MatchLayer.AMOUNT_DRIFT

    exception = (await session.execute(select(Exception_))).scalar_one()
    assert exception.reconciliation_result_id == result.id
    assert exception.closed_at is None


async def test_amount_beyond_tolerance_does_not_match(api, sessions, stream, settings, session):
    await ingest_pair(api, "TXN-FAR", gateway_amount="1000.00", ledger_amount="1500.00")
    await run_pipeline(sessions, stream, settings)
    assert await _count(session, ReconciliationResult) == 0


# --- duplicates ------------------------------------------------------------
async def test_duplicate_submission_produces_a_duplicate_result(
    api, sessions, stream, settings, session
):
    payload = gateway_payload("TXN-D1")
    await post_gateway(api, payload)
    await post_gateway(api, payload)
    await run_pipeline(sessions, stream, settings)

    results = await _results(session)
    assert [result.status for result in results] == [ReconStatus.DUPLICATE]
    assert results[0].match_layer is MatchLayer.DUPLICATE


async def test_duplicate_does_not_strand_the_original_row(api, sessions, stream, settings, session):
    """The duplicate marker points at the original row, which is still waiting for its
    counterparty. Marking it reconciled would lose a real transaction."""
    payload = gateway_payload("TXN-D2")
    await post_gateway(api, payload)
    await post_gateway(api, payload)
    await run_pipeline(sessions, stream, settings)

    row = (await session.execute(select(GatewayTransaction))).scalar_one()
    assert row.reconciled_at is None

    await post_ledger(api, ledger_payload("TXN-D2", occurred_at=row.occurred_at))
    await run_pipeline(sessions, stream, settings)

    statuses = {result.status for result in await _results(session)}
    assert statuses == {ReconStatus.DUPLICATE, ReconStatus.MATCHED}


async def test_many_retries_produce_one_duplicate_marker(api, sessions, stream, settings, session):
    payload = gateway_payload("TXN-D3")
    for _ in range(5):
        await post_gateway(api, payload)
    await run_pipeline(sessions, stream, settings)

    duplicates = [
        result for result in await _results(session) if result.status is ReconStatus.DUPLICATE
    ]
    assert len(duplicates) == 1
    row = (await session.execute(select(GatewayTransaction))).scalar_one()
    assert row.duplicate_count == 4


async def test_duplicate_never_opens_an_exception(api, sessions, stream, settings, session):
    payload = gateway_payload("TXN-D4")
    await post_gateway(api, payload)
    await post_gateway(api, payload)
    await run_pipeline(sessions, stream, settings)
    assert await _count(session, Exception_) == 0


# --- at-least-once delivery ------------------------------------------------
async def test_processing_the_same_message_twice_writes_one_result(
    api, sessions, stream, settings, session
):
    """The headline guarantee. A redelivered message must collapse to a no-op."""
    await ingest_pair(api, "TXN-REPLAY")
    await publish_all(sessions, stream, settings)

    worker = MatcherWorker(sessions, stream, settings, "replay-worker")
    entries = await stream.read("replay-worker", count=10, block_ms=100)

    for entry_id, message in entries:
        await worker.process(message, entry_id)
    for entry_id, message in entries:  # deliberate redelivery of the identical batch
        await worker.process(message, entry_id)

    assert await _count(session, ReconciliationResult) == 1


async def test_redelivery_reports_not_written(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-REPLAY2")
    await publish_all(sessions, stream, settings)

    worker = MatcherWorker(sessions, stream, settings, "replay-worker-2")
    entries = await stream.read("replay-worker-2", count=10, block_ms=100)

    first = [await worker.process(message, entry_id) for entry_id, message in entries]
    second = [await worker.process(message, entry_id) for entry_id, message in entries]

    assert any(outcome.written for outcome in first)
    assert not any(outcome.written for outcome in second)


async def test_unacked_message_is_redelivered_and_stays_idempotent(
    api, sessions, stream, settings, session
):
    """Simulates a crash between processing and XACK: the entry stays pending, another
    consumer claims it, and the second attempt must not duplicate the result."""
    await ingest_pair(api, "TXN-CRASH")
    await publish_all(sessions, stream, settings)

    crasher = MatcherWorker(sessions, stream, settings, "crasher")
    entries = await stream.read("crasher", count=10, block_ms=100)
    for entry_id, message in entries:
        await crasher.process(message, entry_id)
    # No ack: the crash happens here.

    survivor = MatcherWorker(sessions, stream, settings, "survivor")
    claimed = await stream.claim_stale("survivor", min_idle_ms=0, count=10)
    assert claimed, "the pending entries should be claimable"
    for entry_id, message in claimed:
        await survivor.process(message, entry_id)
        await stream.ack(entry_id)

    assert await _count(session, ReconciliationResult) == 1


async def test_message_for_a_missing_row_is_tolerated(sessions, stream, settings, session):
    """A phantom message must not wedge the consumer in a retry loop."""
    from ledgerloop.db.enums import IngestSource
    from ledgerloop.queue.messages import IngestMessage

    worker = MatcherWorker(sessions, stream, settings, "phantom-worker")
    outcome = await worker.process(IngestMessage(IngestSource.GATEWAY, 999_999, "TXN-GHOST"), "0-1")

    assert outcome.decision is None
    assert outcome.written is False
    assert await _count(session, ReconciliationResult) == 0


async def test_matched_rows_are_not_rematched_by_a_later_message(
    api, sessions, stream, settings, session
):
    """reconciled_at excludes settled rows from the counterparty query, so a third
    transaction sharing the txn_id cannot steal a partner out of an existing match."""
    occurred = await ingest_pair(api, "TXN-STEAL")
    await run_pipeline(sessions, stream, settings)

    await post_ledger(
        api, ledger_payload("TXN-STEAL", occurred_at=occurred, idempotency_key="ld-steal-2")
    )
    await run_pipeline(sessions, stream, settings)

    results = await _results(session)
    assert len(results) == 1
    assert results[0].status is ReconStatus.MATCHED


async def test_worker_consumes_a_large_batch(api, sessions, stream, settings, session):
    for index in range(120):
        await ingest_pair(api, f"TXN-BULK-{index}")
    await run_pipeline(sessions, stream, settings)

    results = await _results(session)
    assert len(results) == 120
    assert all(result.status is ReconStatus.MATCHED for result in results)


async def test_consume_all_acks_everything(api, sessions, stream, settings):
    await ingest_pair(api, "TXN-ACK")
    await publish_all(sessions, stream, settings)
    worker = MatcherWorker(sessions, stream, settings, "ack-worker")
    await consume_all(worker, stream)

    assert await stream.pending_count() == 0
