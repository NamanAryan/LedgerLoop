"""Provider payload -> LedgerLoop's internal transaction shape. Pure, like the matcher.

No I/O and no clock: a payload goes in, a ``GatewayWebhookIn`` plus an idempotency key
comes out, or an ``AdapterError`` explaining what the payload was missing. That is what
lets the minor-units test below assert on an exact ``Decimal`` with no database in sight.

Nothing here touches ``ledgerloop.matching``. The five layers are defined once, over
the internal shape, and stay ignorant of who sent the row -- which is the property that
keeps adding a sixth provider from being a change to reconciliation logic.

**Amounts are the whole reason this module is careful.** Stripe and Razorpay both send
money as an *integer count of minor units*: ``105000`` means ₹1,050.00, not ₹105,000.
The conversion is done with ``Decimal.scaleb``, never ``/ 100.0``, because a float
divide reintroduces exactly the binary rounding error this project exists to detect --
and it would do it silently, one paisa at a time, on a value that later gets compared
against a ledger to two decimal places.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ledgerloop.api.schemas import GatewayWebhookIn
from ledgerloop.db.enums import WebhookProvider


class AdapterError(ValueError):
    """The payload cannot be mapped: wrong event type, or a required field is absent.

    Distinct from a signature failure. This one means the delivery is authentic and we
    do not handle it -- which is a 202-and-ignore for an event type we do not care
    about, and a 422 for one we should have been able to read.
    """


#: Currencies whose minor unit is the major unit -- no cents, no paise. Sending 1050
#: JPY means ¥1,050, while 1050 INR means ₹10.50, and getting this backwards is a
#: 100x error in the amount that reconciliation would report as a break rather than as
#: a bug. Stripe's own zero-decimal list.
_ZERO_DECIMAL: frozenset[str] = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA",
        "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
    }
)

#: Three-decimal currencies. Refused rather than rounded: the schema is numeric(18,2),
#: so 1.234 KWD cannot be stored exactly, and quietly rounding somebody's money to make
#: it fit is the one thing a reconciliation engine must never do. A loud 422 is the
#: honest answer until the schema can hold them.
_THREE_DECIMAL: frozenset[str] = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


@dataclass(frozen=True, slots=True)
class AdaptedWebhook:
    """A provider delivery, normalised."""

    payload: GatewayWebhookIn
    #: Derived from the provider's own event id, so a redelivery -- which every gateway
    #: does, on any non-2xx and sometimes on a 2xx -- collapses to `duplicate: true`
    #: against the ingestion unique index rather than booking the payment twice.
    idempotency_key: str
    #: The provider's event type, logged so an unhandled one is diagnosable.
    event_type: str


def minor_to_major(minor_units: int, currency: str) -> Decimal:
    """Integer minor units -> a major-unit Decimal. Exact, always.

    ``Decimal(105000).scaleb(-2)`` is ``Decimal('1050.00')`` -- a decimal exponent
    shift, not a division, so there is no quotient to round and no float anywhere in
    the path. ``105000 / 100.0`` would give a float that happens to print as 1050.0
    today and stops being exact the moment an amount needs more than 15 significant
    digits, which is well inside numeric(18,2).
    """
    code = currency.upper()
    if code in _THREE_DECIMAL:
        raise AdapterError(
            f"currency {code} has three minor digits and cannot be stored in "
            "numeric(18,2) without rounding"
        )
    exponent = 0 if code in _ZERO_DECIMAL else 2
    return Decimal(int(minor_units)).scaleb(-exponent)


def _require(source: dict[str, Any], *path: str) -> Any:
    """Walk a nested payload, naming the exact key that was missing.

    Providers change payload shapes, and "KeyError: 'object'" three frames deep is not
    something anyone can act on at 3am. "stripe payload missing data.object.amount" is.
    """
    cursor: Any = source
    for index, key in enumerate(path):
        if not isinstance(cursor, dict) or key not in cursor:
            raise AdapterError(f"payload missing {'.'.join(path[: index + 1])}")
        cursor = cursor[key]
    return cursor


def _instant(epoch_seconds: Any, field: str) -> datetime:
    """Provider timestamps are unix seconds. Always made timezone-aware.

    The schema forbids a naive datetime, and for good reason -- a naive occurred_at is
    the single likeliest cause of a silent off-by-hours matching failure, which shows
    up as a legitimate-looking unmatched break rather than as an error.
    """
    try:
        return datetime.fromtimestamp(int(epoch_seconds), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise AdapterError(f"{field} is not a unix timestamp: {epoch_seconds!r}") from exc


def adapt_stripe(body: dict[str, Any]) -> AdaptedWebhook:
    """Stripe ``payment_intent.succeeded``.

    ``metadata.txn_id`` is the merchant's own transaction id and is preferred when
    present: reconciliation matches on the identifier *both* systems know, and the
    ledger has never heard of ``pi_3Ox...``. Falling back to the payment intent id
    keeps a source that has not set metadata working, and it will simply report
    unmatched -- which is the truth about that configuration.
    """
    event_type = str(body.get("type", ""))
    if event_type != "payment_intent.succeeded":
        raise AdapterError(f"unhandled stripe event type {event_type!r}")

    # Required fields are walked from the root, not from an already-extracted
    # ``intent``, so the error names ``data.object.amount`` rather than ``amount``.
    # Which key is missing is the entire content of that message; a provider changing
    # a payload shape is the common cause and "missing amount" does not locate it.
    intent = _require(body, "data", "object")
    amount = _require(body, "data", "object", "amount")
    currency = str(_require(body, "data", "object", "currency")).upper()
    intent_id = str(_require(body, "data", "object", "id"))
    metadata = intent.get("metadata") or {}
    txn_id = str(metadata.get("txn_id") or intent_id)
    event_id = str(body.get("id") or intent_id)
    occurred = _instant(intent.get("created", body.get("created")), "data.object.created")

    return AdaptedWebhook(
        payload=GatewayWebhookIn(
            txn_id=txn_id,
            amount=minor_to_major(amount, currency),
            currency=currency,
            occurred_at=occurred,
            gateway_ref=intent_id,
        ),
        idempotency_key=f"stripe:{event_id}",
        event_type=event_type,
    )


def adapt_razorpay(body: dict[str, Any]) -> AdaptedWebhook:
    """Razorpay ``payment.captured``.

    ``notes`` is Razorpay's equivalent of Stripe's ``metadata``, and carries the
    merchant's transaction id for the same reason.
    """
    event_type = str(body.get("event", ""))
    if event_type != "payment.captured":
        raise AdapterError(f"unhandled razorpay event type {event_type!r}")

    entity = _require(body, "payload", "payment", "entity")
    amount = _require(body, "payload", "payment", "entity", "amount")
    currency = str(_require(body, "payload", "payment", "entity", "currency")).upper()
    payment_id = str(_require(body, "payload", "payment", "entity", "id"))
    notes = entity.get("notes") or {}
    txn_id = str(notes.get("txn_id") or payment_id)
    occurred = _instant(entity.get("created_at"), "payload.payment.entity.created_at")

    return AdaptedWebhook(
        payload=GatewayWebhookIn(
            txn_id=txn_id,
            amount=minor_to_major(amount, currency),
            currency=currency,
            occurred_at=occurred,
            gateway_ref=payment_id,
        ),
        # Razorpay puts no event id in the body, so the payment id is the stable
        # identity a redelivery repeats.
        idempotency_key=f"razorpay:{payment_id}",
        event_type=event_type,
    )


def adapt_custom(body: dict[str, Any]) -> AdaptedWebhook:
    """LedgerLoop's own shape: the same fields the unauthenticated endpoint takes.

    Amounts here are major units already -- this is our schema, not a gateway's, and
    ``GatewayWebhookIn`` validates it as ``numeric(18,2)`` at the door. So there is no
    minor-unit conversion, deliberately: doing one would silently divide a correctly
    formatted amount by a hundred.
    """
    payload = GatewayWebhookIn.model_validate(body)
    return AdaptedWebhook(
        payload=payload,
        idempotency_key=f"custom:{payload.txn_id}:{payload.gateway_ref}",
        event_type="custom",
    )


_ADAPTERS = {
    WebhookProvider.STRIPE: adapt_stripe,
    WebhookProvider.RAZORPAY: adapt_razorpay,
    WebhookProvider.CUSTOM: adapt_custom,
}


def adapt(provider: WebhookProvider, body: dict[str, Any]) -> AdaptedWebhook:
    """Map a verified delivery. Raises AdapterError if the payload is not one we take."""
    return _ADAPTERS[provider](body)
