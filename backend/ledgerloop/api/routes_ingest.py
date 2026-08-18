"""Write path: the two ingestion endpoints.

Both return 202, never 201 and never 409. 202 is the accurate code: the record is
durably stored and queued, but it has not been reconciled yet, and the client should
not read that as a completed reconciliation. A duplicate submission also returns 202
with ``duplicate: true`` -- see the note on IngestAck for why answering a retry with
an error is the wrong move.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, status

from ledgerloop.api.deps import IdempotencyKeyDep, SessionDep
from ledgerloop.api.schemas import (
    GatewayWebhookAccepted,
    GatewayWebhookIn,
    IngestAck,
    LedgerSyncAccepted,
    LedgerSyncIn,
)
from ledgerloop.observability.logging import get_logger
from ledgerloop.observability.metrics import INGEST_REQUEST_SECONDS, INGEST_TOTAL
from ledgerloop.services.ingest import IngestOutcome, ingest_gateway, ingest_ledger_batch

router = APIRouter(prefix="/v1", tags=["ingestion"])
log = get_logger("ledgerloop.api.ingest")


def _ack(outcome: IngestOutcome) -> IngestAck:
    return IngestAck(
        row_id=outcome.row_id,
        txn_id=outcome.txn_id,
        duplicate=outcome.duplicate,
        submissions=outcome.submissions,
    )


@router.post(
    "/gateway/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=GatewayWebhookAccepted,
    summary="Accept one gateway transaction",
)
async def gateway_webhook(
    payload: GatewayWebhookIn,
    request: Request,
    session: SessionDep,
    idempotency_key: IdempotencyKeyDep,
) -> GatewayWebhookAccepted:
    started = time.perf_counter()
    # Starlette caches the request body, so this re-read is free and gives us the
    # bytes the gateway actually sent rather than our reserialisation of them.
    raw_body = await request.json()

    # Isolation: READ COMMITTED (PostgreSQL default). The raw row and its outbox event
    # commit together or not at all; idempotency comes from the unique index inside
    # this transaction, so nothing here needs a stronger snapshot.
    async with session.begin():
        outcome = await ingest_gateway(session, payload, idempotency_key, raw_body)

    INGEST_TOTAL.labels(
        endpoint="gateway_webhook", outcome="duplicate" if outcome.duplicate else "accepted"
    ).inc()
    INGEST_REQUEST_SECONDS.labels(endpoint="gateway_webhook").observe(time.perf_counter() - started)
    log.info(
        "gateway.ingested",
        txn_id=outcome.txn_id,
        row_id=outcome.row_id,
        duplicate=outcome.duplicate,
        submissions=outcome.submissions,
    )
    return GatewayWebhookAccepted(result=_ack(outcome))


@router.post(
    "/ledger/sync",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LedgerSyncAccepted,
    summary="Accept a batch of up to 1000 ledger entries",
)
async def ledger_sync(payload: LedgerSyncIn, session: SessionDep) -> LedgerSyncAccepted:
    started = time.perf_counter()

    # Isolation: READ COMMITTED (PostgreSQL default). The whole batch is one
    # transaction: a partial batch would leave the client unable to tell which entries
    # to resend, and resending all of them is exactly what idempotency makes safe.
    async with session.begin():
        outcomes = await ingest_ledger_batch(session, payload.entries)

    duplicates = sum(1 for outcome in outcomes if outcome.duplicate)
    accepted = len(outcomes) - duplicates
    INGEST_TOTAL.labels(endpoint="ledger_sync", outcome="accepted").inc(accepted)
    if duplicates:
        INGEST_TOTAL.labels(endpoint="ledger_sync", outcome="duplicate").inc(duplicates)
    INGEST_REQUEST_SECONDS.labels(endpoint="ledger_sync").observe(time.perf_counter() - started)
    log.info("ledger.ingested", accepted=accepted, duplicates=duplicates, batch=len(outcomes))

    return LedgerSyncAccepted(
        accepted=accepted,
        duplicates=duplicates,
        results=[_ack(outcome) for outcome in outcomes],
    )
