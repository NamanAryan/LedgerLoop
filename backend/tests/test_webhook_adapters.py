"""Provider payload -> internal shape. Pure, so these are exact-value assertions.

The minor-units cases are the point of this file. Stripe sends ``105000`` meaning
₹1,050.00, and the only wrong answers that matter are the quiet ones: a float divide
that is right until it is not, and a missing divide that is off by 100x. Both would
reach the matcher as a perfectly well-formed amount and be reported as a break.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ledgerloop.db.enums import WebhookProvider
from ledgerloop.webhooks.adapters import AdapterError, adapt, minor_to_major

OCCURRED = int(datetime(2026, 3, 14, 9, 30, tzinfo=UTC).timestamp())


def stripe_event(
    amount: int = 105000, currency: str = "inr", txn_id: str | None = "TXN-1"
) -> dict:
    return {
        "id": "evt_1KxYz",
        "type": "payment_intent.succeeded",
        "created": OCCURRED,
        "data": {
            "object": {
                "id": "pi_3Ox9",
                "amount": amount,
                "currency": currency,
                "created": OCCURRED,
                "metadata": {} if txn_id is None else {"txn_id": txn_id},
            }
        },
    }


def razorpay_event(amount: int = 105000, txn_id: str | None = "TXN-1") -> dict:
    return {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_29QQoUBi66xm2f",
                    "amount": amount,
                    "currency": "INR",
                    "created_at": OCCURRED,
                    "notes": {} if txn_id is None else {"txn_id": txn_id},
                }
            }
        },
    }


# --- minor units ------------------------------------------------------------
def test_minor_units_convert_without_float_rounding() -> None:
    """The assertion is on the exact Decimal *and* its exponent. `105000 / 100.0`
    yields a float that compares equal to 1050 but is not a two-place Decimal, so
    comparing only the value would let the float implementation pass."""
    amount = minor_to_major(105000, "INR")
    assert amount == Decimal("1050.00")
    assert isinstance(amount, Decimal)
    assert amount.as_tuple().exponent == -2


@pytest.mark.parametrize(
    ("minor", "expected"),
    [
        (1, "0.01"),
        (99, "0.99"),
        (100, "1.00"),
        (105000, "1050.00"),
        # A value past float64's 15-16 significant digits. This is where `/ 100.0`
        # stops being merely inelegant and starts returning a different number.
        (999999999999999999, "9999999999999999.99"),
        (-2500, "-25.00"),  # refunds and reversals are negative, and must stay exact
    ],
)
def test_minor_units_are_exact_across_magnitudes(minor: int, expected: str) -> None:
    assert minor_to_major(minor, "INR") == Decimal(expected)


def test_minor_units_never_go_through_a_float() -> None:
    """Direct comparison against what the float path would have produced."""
    minor = 999999999999999999
    assert minor_to_major(minor, "INR") != Decimal(str(minor / 100.0))


@pytest.mark.parametrize("currency", ["JPY", "KRW", "VND", "XOF"])
def test_zero_decimal_currencies_are_not_divided(currency: str) -> None:
    """1050 JPY is ¥1,050, not ¥10.50. Dividing here would be a 100x understatement."""
    assert minor_to_major(1050, currency) == Decimal("1050")


def test_three_decimal_currencies_are_refused_not_rounded() -> None:
    """numeric(18,2) cannot hold 1.234 KWD. Refusing is the only honest option: silently
    rounding somebody's money to fit the column is the failure this project exists to
    catch."""
    with pytest.raises(AdapterError, match="three minor digits"):
        minor_to_major(1234, "KWD")


# --- stripe -----------------------------------------------------------------
def test_stripe_maps_a_payment_intent() -> None:
    adapted = adapt(WebhookProvider.STRIPE, stripe_event())
    assert adapted.payload.txn_id == "TXN-1"
    assert adapted.payload.amount == Decimal("1050.00")
    assert adapted.payload.currency == "INR"  # upper-cased; the schema demands it
    assert adapted.payload.gateway_ref == "pi_3Ox9"
    assert adapted.payload.occurred_at.tzinfo is not None
    assert adapted.idempotency_key == "stripe:evt_1KxYz"
    assert adapted.event_type == "payment_intent.succeeded"


def test_stripe_falls_back_to_the_intent_id_without_metadata() -> None:
    adapted = adapt(WebhookProvider.STRIPE, stripe_event(txn_id=None))
    assert adapted.payload.txn_id == "pi_3Ox9"


def test_stripe_idempotency_key_is_the_event_id() -> None:
    """A gateway redelivers the same event id, which is what makes a redelivery collapse
    to duplicate: true instead of booking the payment a second time."""
    first = adapt(WebhookProvider.STRIPE, stripe_event())
    again = adapt(WebhookProvider.STRIPE, stripe_event())
    assert first.idempotency_key == again.idempotency_key


def test_stripe_rejects_an_unhandled_event_type() -> None:
    event = stripe_event()
    event["type"] = "charge.refunded"
    with pytest.raises(AdapterError, match="unhandled stripe event type"):
        adapt(WebhookProvider.STRIPE, event)


def test_stripe_names_the_missing_field() -> None:
    event = stripe_event()
    del event["data"]["object"]["amount"]
    with pytest.raises(AdapterError, match=r"missing data\.object\.amount"):
        adapt(WebhookProvider.STRIPE, event)


def test_stripe_rejects_a_non_numeric_timestamp() -> None:
    event = stripe_event()
    event["data"]["object"]["created"] = "yesterday"
    with pytest.raises(AdapterError, match="not a unix timestamp"):
        adapt(WebhookProvider.STRIPE, event)


# --- razorpay ---------------------------------------------------------------
def test_razorpay_maps_a_captured_payment() -> None:
    adapted = adapt(WebhookProvider.RAZORPAY, razorpay_event())
    assert adapted.payload.txn_id == "TXN-1"
    assert adapted.payload.amount == Decimal("1050.00")
    assert adapted.payload.gateway_ref == "pay_29QQoUBi66xm2f"
    assert adapted.idempotency_key == "razorpay:pay_29QQoUBi66xm2f"


def test_razorpay_falls_back_to_the_payment_id_without_notes() -> None:
    assert adapt(WebhookProvider.RAZORPAY, razorpay_event(txn_id=None)).payload.txn_id == (
        "pay_29QQoUBi66xm2f"
    )


def test_razorpay_rejects_an_unhandled_event() -> None:
    event = razorpay_event()
    event["event"] = "payment.failed"
    with pytest.raises(AdapterError, match="unhandled razorpay event type"):
        adapt(WebhookProvider.RAZORPAY, event)


# --- custom -----------------------------------------------------------------
def test_custom_takes_major_units_unchanged() -> None:
    """Our own shape, already validated as numeric(18,2). Applying a minor-unit divide
    here would turn a correct ₹1,050.00 into ₹10.50."""
    adapted = adapt(
        WebhookProvider.CUSTOM,
        {
            "txn_id": "TXN-9",
            "amount": "1050.00",
            "currency": "INR",
            "occurred_at": "2026-03-14T09:30:00+00:00",
            "gateway_ref": "REF-9",
        },
    )
    assert adapted.payload.amount == Decimal("1050.00")
    assert adapted.idempotency_key == "custom:TXN-9:REF-9"


def test_custom_rejects_a_naive_timestamp() -> None:
    """AwareDatetime at the door. A naive occurred_at is the likeliest cause of a silent
    off-by-hours match failure, which reads as a legitimate break."""
    with pytest.raises(Exception):  # noqa: B017 -- pydantic ValidationError
        adapt(
            WebhookProvider.CUSTOM,
            {
                "txn_id": "TXN-9",
                "amount": "1050.00",
                "currency": "INR",
                "occurred_at": "2026-03-14T09:30:00",
                "gateway_ref": "REF-9",
            },
        )
