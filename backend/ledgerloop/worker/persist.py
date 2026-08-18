"""The thin persistence layer wrapped around the pure matcher.

Everything that touches the database for matching lives here; everything that decides
lives in ``ledgerloop.matching.core``. The split is what makes the decision logic
testable without a container, and it keeps the I/O small enough to audit in one sitting.

The idempotency contract, in one sentence: ``INSERT ... ON CONFLICT DO NOTHING`` with
*no* conflict target, so any of the four partial unique indexes firing means "already
decided" and the write silently collapses to a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerloop.db.enums import IngestSource, ReconStatus
from ledgerloop.db.models import Exception_, GatewayTransaction, LedgerEntry, ReconciliationResult
from ledgerloop.matching.core import Decision, MatchConfig, TxnFacts

#: The two raw tables, keyed by the side they represent.
_MODEL = {IngestSource.GATEWAY: GatewayTransaction, IngestSource.LEDGER: LedgerEntry}


def _other(side: IngestSource) -> IngestSource:
    return IngestSource.LEDGER if side is IngestSource.GATEWAY else IngestSource.GATEWAY


def _facts(row: GatewayTransaction | LedgerEntry, side: IngestSource) -> TxnFacts:
    return TxnFacts(
        side=side,
        row_id=row.id,
        txn_id=row.txn_id,
        amount=row.amount,
        currency=row.currency,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
    )


@dataclass(frozen=True, slots=True)
class PersistResult:
    """``written=False`` means someone else already decided this row -- the normal,
    expected outcome of a redelivered message, not an error."""

    written: bool
    result_id: int | None
    exception_opened: bool


async def load_facts(session: AsyncSession, source: IngestSource, row_id: int) -> TxnFacts | None:
    model = _MODEL[source]
    row = (await session.execute(select(model).where(model.id == row_id))).scalar_one_or_none()
    return _facts(row, source) if row is not None else None


async def find_counterparties(
    session: AsyncSession, candidate: TxnFacts, config: MatchConfig
) -> list[TxnFacts]:
    """Fetch the other side's plausible partners for this transaction.

    Two predicates keep this cheap and correct:

    * ``txn_id = :t AND occurred_at BETWEEN :lo AND :hi`` rides the composite index and
      bounds the scan to the drift window, so the query cost does not grow with history.
    * ``reconciled_at IS NULL`` excludes rows that already reached a terminal state, so
      we can never steal a counterparty out of an existing match.
    """
    other_side = _other(candidate.side)
    model = _MODEL[other_side]
    low = candidate.occurred_at - config.drift_window
    high = candidate.occurred_at + config.drift_window

    rows = (
        (
            await session.execute(
                select(model).where(
                    model.txn_id == candidate.txn_id,
                    model.occurred_at >= low,
                    model.occurred_at <= high,
                    model.reconciled_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [_facts(row, other_side) for row in rows]


def compute_latency_ms(decision: Decision, sides: list[TxnFacts], now: datetime) -> int:
    """Latency measured from the arrival of the *later* side.

    Measuring from the earlier side would charge the engine for the counterparty's
    tardiness: a ledger that syncs 4 minutes after the gateway would show a 4-minute
    'matching latency' when the matcher in fact took 3ms. This number is meant to
    describe the engine, so the clock starts when the engine could first have acted.
    """
    if not sides:
        return 0
    started = max(fact.received_at for fact in sides)
    return max(int((now - started).total_seconds() * 1000), 0)


async def apply_decision(
    session: AsyncSession,
    decision: Decision,
    *,
    latency_ms: int | None,
    message_id: str | None,
) -> PersistResult:
    """Write a decision. Caller owns the transaction.

    Isolation: READ COMMITTED (PostgreSQL default). Two workers can reach this line for
    the same transaction concurrently; the partial unique indexes decide the winner at
    the index level, which is stronger than anything a snapshot could give us and does
    not produce serialisation failures to retry.
    """
    insert_stmt = (
        pg_insert(ReconciliationResult)
        .values(
            gateway_txn_id=decision.gateway_row_id,
            ledger_entry_id=decision.ledger_row_id,
            status=decision.status,
            match_layer=decision.layer,
            notes=decision.notes,
            match_latency_ms=latency_ms,
            source_message_id=message_id,
        )
        # No conflict target: any of the four partial unique indexes firing means this
        # row is already decided. Naming one index would let the other three raise
        # IntegrityError instead of collapsing to a no-op.
        .on_conflict_do_nothing()
        .returning(ReconciliationResult.id)
    )
    result_id: int | None = (await session.execute(insert_stmt)).scalar_one_or_none()
    if result_id is None:
        return PersistResult(written=False, result_id=None, exception_opened=False)

    # A duplicate marker points at the *original* row, which is still waiting for its
    # own counterparty. Marking it reconciled here would strand a real transaction.
    if decision.status is not ReconStatus.DUPLICATE:
        await _mark_reconciled(session, decision)

    exception_opened = False
    if decision.opens_exception:
        opened = (
            await session.execute(
                pg_insert(Exception_)
                .values(reconciliation_result_id=result_id)
                .on_conflict_do_nothing(index_elements=["reconciliation_result_id"])
                .returning(Exception_.id)
            )
        ).scalar_one_or_none()
        exception_opened = opened is not None

    return PersistResult(written=True, result_id=result_id, exception_opened=exception_opened)


async def _mark_reconciled(session: AsyncSession, decision: Decision) -> None:
    """Flip reconciled_at on whichever sides this decision consumed.

    ``WHERE reconciled_at IS NULL`` keeps this idempotent and keeps a replay from
    moving a timestamp that already reflects an earlier, authoritative decision.
    """
    for model, row_id in (
        (GatewayTransaction, decision.gateway_row_id),
        (LedgerEntry, decision.ledger_row_id),
    ):
        if row_id is None:
            continue
        await session.execute(
            update(model)
            .where(model.id == row_id, model.reconciled_at.is_(None))
            .values(reconciled_at=datetime.now(UTC))
        )


async def find_stale_pending(
    session: AsyncSession, source: IngestSource, older_than: timedelta, limit: int
) -> list[TxnFacts]:
    """Rows still awaiting a counterparty past the unmatched window.

    Rides the partial index ``WHERE reconciled_at IS NULL`` ordered by received_at, so
    the sweeper reads the backlog and nothing else -- it never scans reconciled history.
    """
    model = _MODEL[source]
    cutoff = datetime.now(UTC) - older_than
    rows = (
        (
            await session.execute(
                select(model)
                .where(model.reconciled_at.is_(None), model.received_at < cutoff)
                .order_by(model.received_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_facts(row, source) for row in rows]
