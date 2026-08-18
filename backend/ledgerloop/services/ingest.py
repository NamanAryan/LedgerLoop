"""Ingestion: write the raw row and its outbox event in one transaction.

This is the only place in the codebase that inserts into the raw tables, and it is
where the idempotency guarantee actually lives.

The shape of every write here is:

    INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id

If a row comes back, this is a first receipt. If nothing comes back, the key was
already present -- a retry -- and we bump ``duplicate_count`` instead. Both paths
then write an outbox row, so both paths produce exactly one stream message, and the
caller gets a 202 either way.

Why ON CONFLICT and not SELECT-then-INSERT: the check-then-act version has a race
between the SELECT and the INSERT that two concurrent retries will hit, and the loser
gets an IntegrityError that has to be caught anyway. ON CONFLICT does the check and
the act in one statement, at the index level, so there is no window to lose.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerloop.api.schemas import GatewayWebhookIn, LedgerEntryIn
from ledgerloop.db.enums import IngestSource
from ledgerloop.db.models import GatewayTransaction, LedgerEntry, OutboxEvent
from ledgerloop.queue.messages import IngestMessage


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    row_id: int
    txn_id: str
    duplicate: bool
    submissions: int


async def ingest_gateway(
    session: AsyncSession, payload: GatewayWebhookIn, idempotency_key: str, raw_body: dict[str, Any]
) -> IngestOutcome:
    """Store one gateway transaction. Caller owns the transaction boundary.

    Isolation: READ COMMITTED (PostgreSQL default). Correctness here rests on the
    unique index, which is enforced at the index level regardless of isolation, not on
    snapshot stability -- so a stricter level would buy nothing and cost serialisation
    failures under concurrent retries of the same key.
    """
    insert_stmt = (
        pg_insert(GatewayTransaction)
        .values(
            txn_id=payload.txn_id,
            amount=payload.amount,
            currency=payload.currency,
            occurred_at=payload.occurred_at,
            gateway_ref=payload.gateway_ref,
            raw_payload=raw_body,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(GatewayTransaction.id)
    )
    row_id: int | None = (await session.execute(insert_stmt)).scalar_one_or_none()

    if row_id is not None:
        outcome = IngestOutcome(
            row_id=row_id, txn_id=payload.txn_id, duplicate=False, submissions=1
        )
    else:
        # Retry of a key we already hold. Count it on the original row; the row itself
        # is never rewritten, because the first receipt is the record of what arrived.
        bump = (
            update(GatewayTransaction)
            .where(GatewayTransaction.idempotency_key == idempotency_key)
            .values(duplicate_count=GatewayTransaction.duplicate_count + 1)
            .returning(GatewayTransaction.id, GatewayTransaction.duplicate_count)
        )
        existing = (await session.execute(bump)).one()
        outcome = IngestOutcome(
            row_id=existing.id,
            txn_id=payload.txn_id,
            duplicate=True,
            submissions=existing.duplicate_count + 1,
        )

    await _enqueue(session, IngestSource.GATEWAY, [outcome])
    return outcome


async def ingest_ledger_batch(
    session: AsyncSession, entries: list[LedgerEntryIn]
) -> list[IngestOutcome]:
    """Store a batch of ledger entries. Two statements for the whole batch, not two
    per entry: at 1000 entries the per-row version is 2000 round trips, which is the
    difference between a 20ms request and a 2s one.

    Isolation: READ COMMITTED (PostgreSQL default), same reasoning as the gateway path.
    """
    if not entries:
        return []

    # A batch may legitimately contain the same key twice (a client merging two pages
    # of its own retry queue). Collapse to one row per key and remember the tally, so
    # duplicate_count reflects submissions rather than distinct rows.
    submissions_per_key = Counter(entry.idempotency_key for entry in entries)
    first_by_key: dict[str, LedgerEntryIn] = {}
    for entry in entries:
        first_by_key.setdefault(entry.idempotency_key, entry)

    insert_stmt = (
        pg_insert(LedgerEntry)
        .values(
            [
                {
                    "entry_id": entry.entry_id,
                    "txn_id": entry.txn_id,
                    "amount": entry.amount,
                    "currency": entry.currency,
                    "occurred_at": entry.occurred_at,
                    "raw_payload": entry.model_dump(mode="json"),
                    "idempotency_key": entry.idempotency_key,
                }
                for entry in first_by_key.values()
            ]
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(LedgerEntry.id, LedgerEntry.idempotency_key)
    )
    inserted: dict[str, int] = {
        row.idempotency_key: row.id for row in (await session.execute(insert_stmt)).all()
    }

    # Every key we did not insert, plus every key submitted more than once in this
    # batch, needs its counter moved. One executemany carries the whole set.
    increments = {
        key: count - (1 if key in inserted else 0)
        for key, count in submissions_per_key.items()
        if count - (1 if key in inserted else 0) > 0
    }
    counts_by_key: dict[str, int] = {}
    if increments:
        # Core UPDATE against the table, not the ORM entity: an executemany against a
        # mapped class is interpreted as "bulk update by primary key", and we are
        # keying on idempotency_key rather than on id.
        table = LedgerEntry.__table__
        bump = (
            update(table)
            .where(table.c.idempotency_key == bindparam("key"))
            .values(duplicate_count=table.c.duplicate_count + bindparam("delta"))
        )
        await session.execute(
            bump, [{"key": key, "delta": delta} for key, delta in increments.items()]
        )
        rows = (
            await session.execute(
                select(
                    LedgerEntry.id, LedgerEntry.idempotency_key, LedgerEntry.duplicate_count
                ).where(LedgerEntry.idempotency_key.in_(list(increments)))
            )
        ).all()
        for row in rows:
            inserted.setdefault(row.idempotency_key, row.id)
            counts_by_key[row.idempotency_key] = row.duplicate_count

    outcomes = [
        IngestOutcome(
            row_id=inserted[key],
            txn_id=first_by_key[key].txn_id,
            duplicate=key in increments,
            submissions=counts_by_key.get(key, 0) + 1,
        )
        for key in first_by_key
    ]
    await _enqueue(session, IngestSource.LEDGER, outcomes)

    # Response order follows request order, including repeated keys, so a client can
    # zip its own batch against the results.
    by_key = {outcome.row_id: outcome for outcome in outcomes}
    return [by_key[inserted[entry.idempotency_key]] for entry in entries]


async def _enqueue(
    session: AsyncSession, source: IngestSource, outcomes: list[IngestOutcome]
) -> None:
    """Append outbox rows in the *same* transaction as the raw inserts.

    This is the whole point of the outbox: the row and the intent to publish it commit
    together. There is no window in which a transaction exists in Postgres but its
    event was lost, which would show up later as a fabricated 'unmatched' exception.
    """
    if not outcomes:
        return
    await session.execute(
        pg_insert(OutboxEvent).values(
            [
                {
                    "source": source,
                    "row_id": outcome.row_id,
                    "payload": IngestMessage(
                        source=source,
                        row_id=outcome.row_id,
                        txn_id=outcome.txn_id,
                        is_duplicate=outcome.duplicate,
                        submissions=outcome.submissions,
                    ).to_payload(),
                }
                for outcome in outcomes
            ]
        )
    )
