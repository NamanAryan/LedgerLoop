"""SQLAlchemy 2.0 models.

Schema invariants, stated once here so the rest of the codebase can rely on them:

1. Every monetary value is ``numeric(18,2)``. There is no float anywhere.
2. Every instant is ``timestamptz``. There is no naive timestamp anywhere.
3. ``idempotency_key`` is UNIQUE per side. This single constraint is the entire
   ingestion idempotency guarantee -- retries collide with it and become no-ops.
4. A raw row participates in at most one *non-duplicate* reconciliation_result.
   Enforced by a partial unique index, which is what makes at-least-once worker
   delivery safe: the second write hits ON CONFLICT DO NOTHING.
5. ``reconciled_at IS NULL`` means "still waiting for a counterparty". The sweeper
   scans exactly that set via a partial index, never a full anti-join.

Every index below carries a comment naming the query that needs it. An index with
no query is dead weight on the write path, and the write path is the hot one here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ledgerloop.db.base import Base, Money
from ledgerloop.db.enums import IngestSource, MatchLayer, ReconStatus

# Native PG enum types, shared by every column that references them. Creation is
# handled explicitly in the migration so Alembic never tries to CREATE TYPE twice.
_ingest_source = Enum(
    IngestSource, name="ingest_source", values_callable=lambda e: [m.value for m in e]
)
_recon_status = Enum(
    ReconStatus, name="recon_status", values_callable=lambda e: [m.value for m in e]
)
_match_layer = Enum(MatchLayer, name="match_layer", values_callable=lambda e: [m.value for m in e])

# ISO 4217: exactly three uppercase letters. Cheap constraint, catches "inr" and "Rs"
# at the door instead of silently splitting one currency into two stats buckets.
_CURRENCY_CK = "currency ~ '^[A-Z]{3}$'"


class GatewayTransaction(Base):
    """A single transaction as reported by the payment gateway's webhook."""

    __tablename__ = "gateway_transactions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    txn_id: Mapped[str] = mapped_column(String(128))
    amount: Mapped[Money]
    currency: Mapped[str] = mapped_column(String(3))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gateway_ref: Mapped[str] = mapped_column(String(128))

    #: Verbatim webhook body. Kept so a disputed reconciliation can be re-argued from
    #: the original bytes rather than from our parse of them.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    idempotency_key: Mapped[str] = mapped_column(String(255))
    #: Number of *additional* submissions of this same key. 0 on first receipt.
    duplicate_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    #: Set by the matcher when this row reaches a terminal state. NULL = pending.
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        CheckConstraint(_CURRENCY_CK, name="currency_iso4217"),
        CheckConstraint("duplicate_count >= 0", name="duplicate_count_non_negative"),
        # Ingestion: INSERT ... ON CONFLICT (idempotency_key) DO NOTHING.
        # This is the idempotency guarantee, not merely an index.
        Index("uq_gateway_transactions_idempotency_key", "idempotency_key", unique=True),
        # Matcher layers 1-3: WHERE txn_id = :t AND occurred_at BETWEEN :lo AND :hi.
        # Composite so the range predicate is served by the index, not a heap filter.
        Index("ix_gateway_transactions_txn_id_occurred_at", "txn_id", "occurred_at"),
        # Sweeper (every 30s): oldest still-pending rows past the unmatched window.
        # Partial -> the index is the size of the *backlog*, not of the table, so the
        # sweep stays O(backlog) as history grows unboundedly.
        Index(
            "ix_gateway_transactions_pending_received_at",
            "received_at",
            postgresql_where=text("reconciled_at IS NULL"),
        ),
    )


class LedgerEntry(Base):
    """A single entry from the merchant's internal ledger sync."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    entry_id: Mapped[str] = mapped_column(String(128))
    txn_id: Mapped[str] = mapped_column(String(128))
    amount: Mapped[Money]
    currency: Mapped[str] = mapped_column(String(3))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    idempotency_key: Mapped[str] = mapped_column(String(255))
    duplicate_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        CheckConstraint(_CURRENCY_CK, name="currency_iso4217"),
        CheckConstraint("duplicate_count >= 0", name="duplicate_count_non_negative"),
        # Ingestion: batch INSERT ... ON CONFLICT (idempotency_key) DO NOTHING.
        Index("uq_ledger_entries_idempotency_key", "idempotency_key", unique=True),
        # Matcher layers 1-3, mirror of the gateway side.
        Index("ix_ledger_entries_txn_id_occurred_at", "txn_id", "occurred_at"),
        # Sweeper, mirror of the gateway side.
        Index(
            "ix_ledger_entries_pending_received_at",
            "received_at",
            postgresql_where=text("reconciled_at IS NULL"),
        ),
        # NOTE: deliberately no index on entry_id. No query filters by it today; it is
        # carried for traceability back into the merchant's system only.
    )


class OutboxEvent(Base):
    """Transactional outbox: the bridge from "row committed" to "message published".

    The API writes the raw row and the outbox row in one transaction, so either both
    exist or neither does. A relay then moves outbox rows onto the Redis Stream. This
    removes the dual-write failure mode where a row commits but its event is lost --
    which would surface later as a *false* unmatched exception, the worst kind of bug
    in a reconciliation engine, because it fabricates work for a human.

    Publication is at-least-once: the relay can crash after XADD and before marking the
    row sent. That is fine -- the worker is idempotent by design (invariant 4).
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    source: Mapped[IngestSource] = mapped_column(_ingest_source)
    #: PK of the row in gateway_transactions or ledger_entries, per ``source``.
    #: Polymorphic, so no FK is possible; integrity comes from being written in the
    #: same transaction as the row it points at.
    row_id: Mapped[int] = mapped_column(BigInteger)
    #: The exact message body to publish. Denormalised on purpose: the relay never has
    #: to join back to the raw table, so publishing is one sequential scan of a
    #: partial index.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    __table_args__ = (
        # Relay poll: SELECT ... WHERE published_at IS NULL ORDER BY id
        #             LIMIT :n FOR UPDATE SKIP LOCKED.
        # Partial -> stays tiny (== unpublished backlog) after millions of events, and
        # SKIP LOCKED lets N relay instances drain it without blocking each other.
        Index(
            "ix_outbox_events_unpublished",
            "id",
            postgresql_where=text("published_at IS NULL"),
        ),
    )


class ReconciliationResult(Base):
    """The outcome of reconciling one transaction. The engine's output of record."""

    __tablename__ = "reconciliation_results"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    gateway_txn_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("gateway_transactions.id", ondelete="RESTRICT"), default=None
    )
    ledger_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ledger_entries.id", ondelete="RESTRICT"), default=None
    )

    status: Mapped[ReconStatus] = mapped_column(_recon_status)
    match_layer: Mapped[MatchLayer] = mapped_column(_match_layer)

    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    #: End-to-end reconciliation latency: resolved_at - max(received_at of both sides).
    #: Materialised because /v1/stats computes p50/p95/p99 over it; deriving it at read
    #: time would mean joining both raw tables on every stats call.
    match_latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    #: Redis Stream entry id that produced this result. Traceability only -- it shows up
    #: in the worker's structured logs. Never a correctness mechanism: a redelivered
    #: message carries the same id, but so would a legitimate reprocess after a reset.
    source_message_id: Mapped[str | None] = mapped_column(String(64), default=None)

    gateway_txn: Mapped[GatewayTransaction | None] = relationship(lazy="raise")
    ledger_entry: Mapped[LedgerEntry | None] = relationship(lazy="raise")

    __table_args__ = (
        # A result must reference at least one side. A row referencing neither is
        # meaningless and would silently pollute every count.
        CheckConstraint(
            "gateway_txn_id IS NOT NULL OR ledger_entry_id IS NOT NULL",
            name="at_least_one_side",
        ),
        CheckConstraint(
            "match_latency_ms IS NULL OR match_latency_ms >= 0", name="latency_non_negative"
        ),
        # --- Worker idempotency (the important part) -------------------------
        # At most one non-duplicate outcome per raw row. Two workers racing the same
        # txn, or one worker replaying a redelivered message, both land on ON CONFLICT
        # DO NOTHING instead of writing a second result. At-least-once delivery becomes
        # effectively-once without a distributed lock.
        Index(
            "uq_reconciliation_results_gateway_active",
            "gateway_txn_id",
            unique=True,
            postgresql_where=text("gateway_txn_id IS NOT NULL AND status <> 'duplicate'"),
        ),
        Index(
            "uq_reconciliation_results_ledger_active",
            "ledger_entry_id",
            unique=True,
            postgresql_where=text("ledger_entry_id IS NOT NULL AND status <> 'duplicate'"),
        ),
        # One duplicate marker per raw row, however many retries arrive; the retry count
        # itself lives in duplicate_count. Keeps "duplicates" a count of *keys*, not of
        # HTTP requests -- which is exactly what the generator's ground truth measures.
        Index(
            "uq_reconciliation_results_gateway_duplicate",
            "gateway_txn_id",
            unique=True,
            postgresql_where=text("gateway_txn_id IS NOT NULL AND status = 'duplicate'"),
        ),
        Index(
            "uq_reconciliation_results_ledger_duplicate",
            "ledger_entry_id",
            unique=True,
            postgresql_where=text("ledger_entry_id IS NOT NULL AND status = 'duplicate'"),
        ),
        # --- Read path -------------------------------------------------------
        # GET /v1/transactions?status=&cursor= -> WHERE status = :s AND id < :cursor
        # ORDER BY id DESC. Keyset pagination, so the index alone serves the ordering.
        Index("ix_reconciliation_results_status_id", "status", text("id DESC")),
        # GET /v1/stats?window= -> WHERE resolved_at >= now() - :window, plus the
        # percentile_cont aggregation over match_latency_ms inside that range.
        Index("ix_reconciliation_results_resolved_at", "resolved_at"),
        # Unfiltered GET /v1/transactions (ORDER BY id DESC) rides the primary key.
    )


class Exception_(Base):
    """A reconciliation result a human has to look at, and its resolution."""

    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    reconciliation_result_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reconciliation_results.id", ondelete="CASCADE")
    )

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resolution_notes: Mapped[str | None] = mapped_column(Text, default=None)

    result: Mapped[ReconciliationResult] = relationship(lazy="raise")

    __table_args__ = (
        CheckConstraint("closed_at IS NULL OR closed_at >= opened_at", name="closed_after_opened"),
        # One exception per result. Makes exception creation an ON CONFLICT DO NOTHING,
        # so a retried worker message cannot open the same case twice.
        Index("uq_exceptions_reconciliation_result_id", "reconciliation_result_id", unique=True),
        # GET /v1/exceptions?status=open -> WHERE closed_at IS NULL ORDER BY id DESC.
        # Partial index sized to the open queue, which is the list a human works from.
        Index(
            "ix_exceptions_open_id",
            text("id DESC"),
            postgresql_where=text("closed_at IS NULL"),
        ),
        # GET /v1/exceptions?status=closed -> the audit trail, same keyset shape.
        Index(
            "ix_exceptions_closed_id",
            text("id DESC"),
            postgresql_where=text("closed_at IS NOT NULL"),
        ),
    )
