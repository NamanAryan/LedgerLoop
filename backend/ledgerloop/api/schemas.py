"""Request and response models.

Not one ``dict[str, Any]`` crosses an endpoint boundary. Two things follow from that:
the OpenAPI schema is generated rather than written, and every invariant the database
enforces is *also* enforced at the edge, so a bad payload gets a 422 with a field path
instead of a 500 from a CHECK constraint.

``AwareDatetime`` is doing real work here: it rejects a naive timestamp at the door.
A naive ``occurred_at`` is the single most likely source of a silent off-by-hours
matching failure, and it would look like a legitimate 'unmatched' break.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from ledgerloop.db.enums import MatchLayer, ReconStatus

#: numeric(18,2), mirrored from the schema. More than two decimal places is a 422,
#: not a silent round -- rounding someone's money without telling them is not okay.
MoneyField = Annotated[Decimal, Field(max_digits=18, decimal_places=2)]
CurrencyField = Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="ISO 4217, uppercase")]
IdemKeyField = Annotated[str, Field(min_length=1, max_length=255)]
ShortIdField = Annotated[str, Field(min_length=1, max_length=128)]


class _Strict(BaseModel):
    # Unknown fields are rejected rather than ignored: a client sending `ammount`
    # should be told, not silently reconciled against a default.
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Ingestion                                                                     #
# --------------------------------------------------------------------------- #


class GatewayWebhookIn(_Strict):
    """One transaction as posted by the payment gateway."""

    txn_id: ShortIdField
    amount: MoneyField
    currency: CurrencyField
    occurred_at: AwareDatetime
    gateway_ref: ShortIdField

    @field_validator("amount")
    @classmethod
    def _non_zero(cls, value: Decimal) -> Decimal:
        # Sign is allowed (refunds and reversals are negative); zero is not, because a
        # zero-value transaction has nothing to reconcile.
        if value == 0:
            raise ValueError("amount must be non-zero")
        return value


class LedgerEntryIn(_Strict):
    """One line from the merchant's internal ledger."""

    entry_id: ShortIdField
    txn_id: ShortIdField
    amount: MoneyField
    currency: CurrencyField
    occurred_at: AwareDatetime
    idempotency_key: IdemKeyField

    @field_validator("amount")
    @classmethod
    def _non_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("amount must be non-zero")
        return value


class LedgerSyncIn(_Strict):
    """A batch of ledger entries. The cap is enforced by the model, so an oversized
    batch is rejected before a single row is parsed into the session."""

    entries: Annotated[list[LedgerEntryIn], Field(min_length=1, max_length=1000)]


class IngestAck(BaseModel):
    """Per-record ingestion outcome.

    A retry reports ``duplicate=true`` alongside a 202, never a 409. Clients retry on
    5xx and on network failures; answering a successful retry with an error would make
    them retry the retry. The whole point of an idempotency key is that the second
    attempt is a no-op that still looks like success.
    """

    row_id: int
    txn_id: str
    duplicate: bool
    submissions: int = Field(description="Total times this idempotency key has been submitted.")


class GatewayWebhookAccepted(BaseModel):
    accepted: Literal[True] = True
    result: IngestAck


class LedgerSyncAccepted(BaseModel):
    accepted: int = Field(description="Entries stored for the first time.")
    duplicates: int = Field(description="Entries whose idempotency key was already present.")
    results: list[IngestAck]


# --------------------------------------------------------------------------- #
# Read path                                                                     #
# --------------------------------------------------------------------------- #


class ReconciliationResultOut(BaseModel):
    id: int
    gateway_txn_id: int | None
    ledger_entry_id: int | None
    status: ReconStatus
    match_layer: MatchLayer
    resolved_at: datetime
    match_latency_ms: int | None
    notes: str | None

    # Denormalised onto the response, not the table. The dashboard has to render the
    # business identifier and both amounts side by side to show a drift break, and
    # without these it would need one extra request per row to be readable at all.
    # Served by a join on the page of rows already being fetched -- see
    # fetch_results_page() -- so it costs one query, not N.
    txn_id: str | None = None
    currency: str | None = None
    gateway_amount: MoneyField | None = None
    ledger_amount: MoneyField | None = None

    @property
    def amount_delta(self) -> Decimal | None:
        """Ledger minus gateway; None unless both sides exist."""
        if self.gateway_amount is None or self.ledger_amount is None:
            return None
        return self.ledger_amount - self.gateway_amount


class TransactionPage(BaseModel):
    """Keyset page. ``next_cursor`` is None exactly when the feed is exhausted."""

    items: list[ReconciliationResultOut]
    next_cursor: str | None = Field(
        default=None,
        description="Opaque. Pass back as ?cursor= for the next page. Never an offset.",
    )


class ExceptionOut(BaseModel):
    id: int
    reconciliation_result_id: int
    opened_at: datetime
    closed_at: datetime | None
    resolution_notes: str | None
    status: ReconStatus
    match_layer: MatchLayer
    gateway_txn_id: int | None
    ledger_entry_id: int | None
    notes: str | None

    # Same enrichment, and for the same reason, as ReconciliationResultOut: an
    # operator resolving a break needs the transaction and the two amounts, not a
    # pair of foreign keys. Joined onto the page already being fetched.
    txn_id: str | None = None
    currency: str | None = None
    gateway_amount: MoneyField | None = None
    ledger_amount: MoneyField | None = None


class ExceptionPage(BaseModel):
    items: list[ExceptionOut]
    next_cursor: str | None = None


class ExceptionResolveIn(_Strict):
    resolution_notes: Annotated[str, Field(min_length=1, max_length=4000)]


class LatencyPercentiles(BaseModel):
    """Milliseconds, from the later side's arrival to the result being written."""

    p50: float | None
    p95: float | None
    p99: float | None


class StatsOut(BaseModel):
    window: Literal["1h", "24h", "7d"]
    window_seconds: int
    matched: int
    #: Of ``matched``, how many needed the fuzzy-time layer rather than an exact hit.
    #: Not a separate bucket -- a subset, so it must never be added to ``matched``.
    matched_via_time_drift: int
    unmatched: int
    #: ``unmatched`` split by which side was missing. The two sum to ``unmatched``.
    unmatched_gateway_only: int
    unmatched_ledger_only: int
    duplicates: int
    drift: int
    total: int = Field(description="Active results in the window; duplicates excluded.")
    match_rate: float = Field(ge=0.0, le=1.0, description="matched / total; 0.0 when total is 0.")
    latency_ms: LatencyPercentiles
    throughput_tx_per_sec: float = Field(description="Results resolved per second over the window.")
    open_exceptions: int


# --------------------------------------------------------------------------- #
# Ops                                                                           #
# --------------------------------------------------------------------------- #


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    service: str


class DependencyStatus(BaseModel):
    ok: bool
    detail: str | None = None


class ReadyOut(BaseModel):
    ready: bool
    database: DependencyStatus
    redis: DependencyStatus
