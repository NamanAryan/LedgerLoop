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

from ledgerloop.db.enums import MatchLayer, ReconStatus, WebhookDeliveryStatus, WebhookProvider

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
# Webhook sources                                                               #
# --------------------------------------------------------------------------- #


class WebhookSourceOut(BaseModel):
    """One configured provider endpoint.

    Note what is absent: ``signing_secret``. It is the one value in the schema that
    cannot be hashed -- HMAC verification needs the bytes themselves -- so the whole
    of its protection is that it never leaves the database. Adding it to this model
    would be enough to undo that, which is why it is called out here rather than
    silently omitted.

    ``source_token`` *is* returned. It appears in the URL the provider posts to, so it
    is not a secret, and an operator needs it to configure the endpoint at the far end.
    """

    id: int
    provider: WebhookProvider
    source_token: str
    label: str
    #: When something last arrived, and what happened to it. Both nullable: a source
    #: that has never received a delivery is a different state from one whose last
    #: delivery failed, and a dashboard has to be able to say which.
    last_event_at: datetime | None
    last_delivery_status: WebhookDeliveryStatus | None
    created_at: datetime
    revoked_at: datetime | None

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None and self.last_delivery_status is WebhookDeliveryStatus.OK


class WebhookSourceList(BaseModel):
    """Not paginated. An account configures a handful of endpoints, not thousands, so
    a cursor here would be ceremony with no page behind it."""

    items: list[WebhookSourceOut]


class WebhookSourceCreate(_Strict):
    """Create an endpoint for the calling account.

    No ``signing_secret`` field, deliberately. Accepting one here would say the caller
    chooses the secret, and for Stripe and Razorpay they cannot -- the provider issues
    it, and verification uses the provider's bytes or fails. So the server generates
    one, and a source that must verify a real Stripe delivery has its secret set from
    the CLI instead (``scripts/create_webhook_source.py``).
    """

    provider: WebhookProvider
    label: Annotated[str, Field(min_length=1, max_length=255)] = "Dashboard endpoint"


class WebhookTestResult(BaseModel):
    """Outcome of a self-test delivery.

    ``duplicate`` is surfaced rather than hidden because it is the *correct* answer to
    a second test with the same event id, and a dashboard that reported it as a failure
    would be teaching the operator to distrust the idempotency they depend on.
    """

    delivered: bool
    http_status: int
    duplicate: bool
    txn_id: str | None = None
    detail: str | None = None


class WebhookAccepted(BaseModel):
    """Answer to a provider delivery. Mirrors the unauthenticated endpoint's contract:
    202 with ``duplicate`` on a redelivery, never a 409."""

    accepted: Literal[True] = True
    provider: WebhookProvider
    event_type: str
    result: IngestAck


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

    # Both occurrence instants, for the same reason as the amounts above. Layer 2
    # exists because clocks disagree, so "matched via time drift" is only meaningful
    # to a reader who can see *by how much* -- and that skew is the difference between
    # these two fields. Without them the dashboard can name the layer but not show the
    # evidence for it. Same join, no extra query.
    gateway_occurred_at: datetime | None = None
    ledger_occurred_at: datetime | None = None

    @property
    def skew_ms(self) -> int | None:
        """Ledger minus gateway, in milliseconds. None unless both sides exist."""
        if self.gateway_occurred_at is None or self.ledger_occurred_at is None:
            return None
        return int((self.ledger_occurred_at - self.gateway_occurred_at).total_seconds() * 1000)

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
    gateway_occurred_at: datetime | None = None
    ledger_occurred_at: datetime | None = None


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
    throughput_tx_per_sec: float
    #: The server's unmatched window, in seconds. Reported because a client cannot
    #: otherwise tell "not matched yet" from "declared a break": a row with no
    #: counterparty stays pending until the sweeper has waited this long, so any
    #: unmatched count read sooner than this after ingestion is still incomplete by
    #: design. Without it a dashboard has to either guess the window or call a
    #: deliberate wait a discrepancy.
    unmatched_after_s: int = Field(description="Results resolved per second over the window.")
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
