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
6. ``tenant_id`` is the leading column of every uniqueness constraint and every
   read-path index. Leading, not trailing: an index on ``(idempotency_key, tenant_id)``
   would still enforce global uniqueness of the key, so two tenants posting the same
   external transaction id would collide through ``ON CONFLICT DO NOTHING`` and the
   second tenant's payment would be silently swallowed as a "duplicate".

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
from ledgerloop.db.enums import (
    AccountKind,
    IngestSource,
    MatchLayer,
    ReconStatus,
    WebhookDeliveryStatus,
    WebhookProvider,
)

# Native PG enum types, shared by every column that references them. Creation is
# handled explicitly in the migration so Alembic never tries to CREATE TYPE twice.
_ingest_source = Enum(
    IngestSource, name="ingest_source", values_callable=lambda e: [m.value for m in e]
)
_recon_status = Enum(
    ReconStatus, name="recon_status", values_callable=lambda e: [m.value for m in e]
)
_match_layer = Enum(MatchLayer, name="match_layer", values_callable=lambda e: [m.value for m in e])
_account_kind = Enum(
    AccountKind, name="account_kind", values_callable=lambda e: [m.value for m in e]
)
_webhook_provider = Enum(
    WebhookProvider, name="webhook_provider", values_callable=lambda e: [m.value for m in e]
)
_webhook_delivery_status = Enum(
    WebhookDeliveryStatus,
    name="webhook_delivery_status",
    values_callable=lambda e: [m.value for m in e],
)

#: Every tenant-scoped column shares this definition. RESTRICT rather than CASCADE:
#: deleting an account must not silently take reconciliation evidence with it. The
#: demo sweeper deletes in explicit dependency order instead, and a missed table shows
#: up as a loud FK violation rather than as half-erased history.
def _tenant_column() -> Mapped[int]:
    return mapped_column(BigInteger, ForeignKey("accounts.id", ondelete="RESTRICT"))

# ISO 4217: exactly three uppercase letters. Cheap constraint, catches "inr" and "Rs"
# at the door instead of silently splitting one currency into two stats buckets.
_CURRENCY_CK = "currency ~ '^[A-Z]{3}$'"


class Account(Base):
    """A tenant. Either a key-holding customer or an ephemeral demo session.

    ``name`` carries the identity the resolver looked the account up by: the shared
    default demo tenant is literally named ``default``, and a per-visitor demo tenant
    is named by the UUID its browser sent in ``X-Demo-Session``. Keeping the lookup
    key in one unique column means resolution is a single index probe rather than a
    branch over two different columns.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[AccountKind] = mapped_column(_account_kind)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: Bumped lazily (see ``settings.last_seen_refresh_s``) on every request the
    #: account makes. Retention reads this and nothing else, so an actively used demo
    #: session is never swept out from under a visitor who is still looking at it.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # Tenant resolution, on every /v1 request: WHERE name = :n.
        # UNIQUE as well as indexed -- it is also the ON CONFLICT target that makes
        # two concurrent first requests from one demo session create one account.
        Index("uq_accounts_name", "name", unique=True),
        # Demo retention sweep: WHERE kind = 'demo' AND last_seen_at < :cutoff.
        # Partial, so the index is the size of the demo population and a growing
        # roster of real accounts never slows the sweep down.
        Index(
            "ix_accounts_demo_last_seen_at",
            "last_seen_at",
            postgresql_where=text("kind = 'demo'"),
        ),
    )


class ApiKey(Base):
    """A bearer credential for one real account.

    Only a SHA-256 of the key is stored. SHA-256 rather than bcrypt/argon2 on purpose:
    those exist to make *low-entropy human passwords* expensive to brute force, and
    these keys are 32 bytes from ``secrets.token_urlsafe``. There is no dictionary to
    run against 256 bits of entropy, so a slow KDF would buy nothing and would put a
    deliberate delay on the authentication path of every single request.

    ``prefix`` is the first few characters of the key, kept in the clear purely so a
    human can tell two keys apart in a list. It is not a secret and is never matched
    against on the authentication path -- lookup is by hash, one unique index probe.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="CASCADE")
    )

    key_hash: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: Set to revoke. Revoked keys are kept, not deleted: "which key was this request
    #: made with" has to stay answerable after the key is turned off.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    account: Mapped[Account] = relationship(lazy="raise")

    __table_args__ = (
        # Authentication: WHERE key_hash = :h. Unique because a hash collision here
        # would mean one credential authenticating two accounts.
        Index("uq_api_keys_key_hash", "key_hash", unique=True),
        # Listing an account's keys.
        Index("ix_api_keys_account_id", "account_id"),
    )


class WebhookSource(Base):
    """One configured provider endpoint for one account.

    The URL carries ``source_token``, which says *who is posting*. The
    ``signing_secret`` proves it, over the raw request bytes. Splitting the two is the
    point: the token appears in access logs, proxy traces and browser history, and on
    its own it authorises nothing.

    ``signing_secret`` is stored in the clear, unlike ``ApiKey.key_hash``, and that is
    forced rather than chosen: HMAC verification needs the secret itself, so there is
    no version of this that stores only a digest. What it means in practice is that
    this column is the most sensitive thing in the schema -- it is never logged, never
    returned by any endpoint, and never included in a source listing.
    """

    __tablename__ = "webhook_sources"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="CASCADE")
    )

    provider: Mapped[WebhookProvider] = mapped_column(_webhook_provider)
    #: URL-safe, unguessable, and in the path. Identifies the source; proves nothing.
    source_token: Mapped[str] = mapped_column(String(64))
    #: Shared secret for HMAC verification. Never leaves this table.
    signing_secret: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(255))

    #: When the last delivery arrived, and how it went. Both are for a human staring at
    #: a dashboard asking "is this endpoint actually receiving anything?" -- a question
    #: no counter answers, because zero deliveries and a thousand rejected ones both
    #: leave the transaction tables empty.
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_delivery_status: Mapped[WebhookDeliveryStatus | None] = mapped_column(
        _webhook_delivery_status, default=None
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    account: Mapped[Account] = relationship(lazy="raise")

    __table_args__ = (
        # Delivery: WHERE source_token = :t. One probe on every inbound webhook, so it
        # is the hot path for this table. Unique because a token resolving to two
        # sources would mean one gateway's events landing in two accounts.
        Index("uq_webhook_sources_source_token", "source_token", unique=True),
        # GET /v1/gateway/sources -> WHERE account_id = :a ORDER BY id.
        Index("ix_webhook_sources_account_id", "account_id"),
    )


class GatewayTransaction(Base):
    """A single transaction as reported by the payment gateway's webhook."""

    __tablename__ = "gateway_transactions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[int] = _tenant_column()

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
        # Ingestion: INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING.
        # This is the idempotency guarantee, not merely an index. tenant_id leads:
        # idempotency keys are client-chosen, so two tenants will pick the same one,
        # and a global unique index would answer the second tenant's genuine payment
        # with "duplicate" and never store it.
        Index(
            "uq_gateway_transactions_tenant_idempotency_key",
            "tenant_id",
            "idempotency_key",
            unique=True,
        ),
        # Matcher layers 1-3: WHERE tenant_id = :a AND txn_id = :t
        #                     AND occurred_at BETWEEN :lo AND :hi.
        # Composite so the range predicate is served by the index, not a heap filter.
        Index(
            "ix_gateway_transactions_tenant_txn_id_occurred_at",
            "tenant_id",
            "txn_id",
            "occurred_at",
        ),
        # Sweeper (every 30s): oldest still-pending rows past the unmatched window.
        # Partial -> the index is the size of the *backlog*, not of the table, so the
        # sweep stays O(backlog) as history grows unboundedly. Deliberately NOT
        # tenant-leading: the sweep is a global pass over every tenant's backlog in
        # arrival order, so a tenant-leading index would force it to walk tenants.
        Index(
            "ix_gateway_transactions_pending_received_at",
            "received_at",
            postgresql_where=text("reconciled_at IS NULL"),
        ),
        # Demo retention: DELETE ... WHERE tenant_id = :a.
        Index("ix_gateway_transactions_tenant_id", "tenant_id"),
    )


class LedgerEntry(Base):
    """A single entry from the merchant's internal ledger sync."""

    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[int] = _tenant_column()

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
        # Ingestion: batch INSERT ... ON CONFLICT (tenant_id, idempotency_key)
        # DO NOTHING. Same reasoning as the gateway side.
        Index(
            "uq_ledger_entries_tenant_idempotency_key",
            "tenant_id",
            "idempotency_key",
            unique=True,
        ),
        # Matcher layers 1-3, mirror of the gateway side.
        Index(
            "ix_ledger_entries_tenant_txn_id_occurred_at", "tenant_id", "txn_id", "occurred_at"
        ),
        # Sweeper, mirror of the gateway side. Global, not tenant-leading.
        Index(
            "ix_ledger_entries_pending_received_at",
            "received_at",
            postgresql_where=text("reconciled_at IS NULL"),
        ),
        # Demo retention: DELETE ... WHERE tenant_id = :a.
        Index("ix_ledger_entries_tenant_id", "tenant_id"),
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
    #: Copied from whichever raw row(s) this result covers. Denormalised rather than
    #: joined: every read-path query filters on it, and a join to the raw tables on
    #: each one would undo the point of the covering indexes below.
    tenant_id: Mapped[int] = _tenant_column()

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
            "uq_reconciliation_results_tenant_gateway_active",
            "tenant_id",
            "gateway_txn_id",
            unique=True,
            postgresql_where=text("gateway_txn_id IS NOT NULL AND status <> 'duplicate'"),
        ),
        Index(
            "uq_reconciliation_results_tenant_ledger_active",
            "tenant_id",
            "ledger_entry_id",
            unique=True,
            postgresql_where=text("ledger_entry_id IS NOT NULL AND status <> 'duplicate'"),
        ),
        # One duplicate marker per raw row, however many retries arrive; the retry count
        # itself lives in duplicate_count. Keeps "duplicates" a count of *keys*, not of
        # HTTP requests -- which is exactly what the generator's ground truth measures.
        Index(
            "uq_reconciliation_results_tenant_gateway_duplicate",
            "tenant_id",
            "gateway_txn_id",
            unique=True,
            postgresql_where=text("gateway_txn_id IS NOT NULL AND status = 'duplicate'"),
        ),
        Index(
            "uq_reconciliation_results_tenant_ledger_duplicate",
            "tenant_id",
            "ledger_entry_id",
            unique=True,
            postgresql_where=text("ledger_entry_id IS NOT NULL AND status = 'duplicate'"),
        ),
        # --- Read path -------------------------------------------------------
        # Every read is tenant-scoped, so tenant_id leads every read index. Without
        # it the planner would range-scan one tenant's slice out of every tenant's
        # rows and discard the rest -- the busiest tenant would set the cost of the
        # smallest one's dashboard.
        #
        # GET /v1/transactions?status=&cursor= -> WHERE tenant_id = :a AND status = :s
        # AND id < :cursor ORDER BY id DESC. Keyset, so the index serves the ordering.
        Index(
            "ix_reconciliation_results_tenant_status_id", "tenant_id", "status", text("id DESC")
        ),
        # Unfiltered GET /v1/transactions -> WHERE tenant_id = :a ORDER BY id DESC.
        # The primary key no longer covers this now that a tenant predicate is always
        # present, so the ordering needs its own index.
        Index("ix_reconciliation_results_tenant_id_id", "tenant_id", text("id DESC")),
        # GET /v1/stats?window= -> WHERE tenant_id = :a AND resolved_at >= :cutoff,
        # plus the percentile_cont aggregation over match_latency_ms in that range.
        Index("ix_reconciliation_results_tenant_resolved_at", "tenant_id", "resolved_at"),
    )


class Exception_(Base):
    """A reconciliation result a human has to look at, and its resolution."""

    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[int] = _tenant_column()
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
        # GET /v1/exceptions?status=open -> WHERE tenant_id = :a AND closed_at IS NULL
        # ORDER BY id DESC. Partial index sized to the open queue across all tenants,
        # tenant-leading so one tenant's queue is an index descent rather than a filter.
        Index(
            "ix_exceptions_tenant_open_id",
            "tenant_id",
            text("id DESC"),
            postgresql_where=text("closed_at IS NULL"),
        ),
        # GET /v1/exceptions?status=closed -> the audit trail, same keyset shape.
        Index(
            "ix_exceptions_tenant_closed_id",
            "tenant_id",
            text("id DESC"),
            postgresql_where=text("closed_at IS NOT NULL"),
        ),
        # Unfiltered GET /v1/exceptions, and the demo retention DELETE.
        Index("ix_exceptions_tenant_id_id", "tenant_id", text("id DESC")),
    )
