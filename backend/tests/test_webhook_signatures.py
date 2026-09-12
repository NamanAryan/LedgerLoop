"""Signature verification, per provider. No database, no HTTP -- these are pure.

The replay tests are the ones worth reading. A signature scheme that verifies correctly
but has no time bound is not secure against anyone who has seen one valid request, and
that failure is invisible in a test that only checks "correct signature passes".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ledgerloop.db.enums import WebhookProvider
from ledgerloop.webhooks.signatures import (
    DEFAULT_TOLERANCE,
    sign,
    verify,
    verify_custom,
    verify_razorpay,
    verify_stripe,
)

SECRET = "whsec_test_secret_value"
NOW = datetime(2026, 3, 14, 9, 30, 0, tzinfo=UTC)
BODY = b'{"id":"evt_1","type":"payment_intent.succeeded","data":{"object":{"amount":105000}}}'


# --- stripe ----------------------------------------------------------------
def test_stripe_accepts_its_own_signature() -> None:
    header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    assert verify_stripe(BODY, header, SECRET, now=NOW)


def test_stripe_rejects_a_wrong_secret() -> None:
    header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    verdict = verify_stripe(BODY, header, "whsec_a_different_secret", now=NOW)
    assert not verdict
    assert verdict.reason == "signature mismatch"


def test_stripe_rejects_a_tampered_body() -> None:
    """The amount is changed by one paisa after signing. This is the attack the HMAC
    exists to stop, and the one a "parse then verify" implementation lets through."""
    header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    tampered = BODY.replace(b"105000", b"105001")
    assert not verify_stripe(tampered, header, SECRET, now=NOW)


def test_stripe_rejects_reserialised_body() -> None:
    """Semantically identical JSON, different bytes -- which is exactly what binding a
    Pydantic model and dumping it back produces. It must fail, so that anyone who
    refactors the endpoint that way finds out here rather than in production."""
    header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    respaced = BODY.replace(b'","', b'" , "')
    assert respaced != BODY
    assert not verify_stripe(respaced, header, SECRET, now=NOW)


@pytest.mark.parametrize("header", ["", None, "v1=abc", "t=123", "garbage", "t=notanint,v1=abc"])
def test_stripe_rejects_malformed_headers(header: str | None) -> None:
    assert not verify_stripe(BODY, header, SECRET, now=NOW)


def test_stripe_accepts_any_of_several_v1_signatures() -> None:
    """Stripe sends one v1 per active secret during a rotation. Reading only the first
    would break every rotation, so any match is a pass."""
    valid = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    timestamp = valid.split(",")[0]
    digest = valid.split("v1=")[1]
    rotated = f"{timestamp},v1=00deadbeef,v1={digest}"
    assert verify_stripe(BODY, rotated, SECRET, now=NOW)


def test_stripe_rejects_a_replay_past_the_tolerance() -> None:
    """A captured request replayed six minutes later. The signature is still perfectly
    valid -- only the window refuses it."""
    header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    late = NOW + DEFAULT_TOLERANCE + timedelta(seconds=1)
    verdict = verify_stripe(BODY, header, SECRET, now=late)
    assert not verdict
    assert verdict.reason == "timestamp outside tolerance"


def test_stripe_accepts_inside_the_tolerance() -> None:
    header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    assert verify_stripe(BODY, header, SECRET, now=NOW + timedelta(minutes=4, seconds=59))


def test_stripe_tolerance_boundary_is_inclusive() -> None:
    """Exactly 300.000s. Stated as a test because "<=" and "<" are one character apart
    and the difference is otherwise invisible."""
    header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    assert verify_stripe(BODY, header, SECRET, now=NOW + DEFAULT_TOLERANCE)


def test_stripe_rejects_a_timestamp_from_the_future() -> None:
    """Symmetric window. A timestamp far ahead is either a badly wrong clock or a
    forgery, and accepting it would let one be used to widen the replay window."""
    future = NOW + timedelta(hours=1)
    header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=future)
    assert not verify_stripe(BODY, header, SECRET, now=NOW)


# --- razorpay ---------------------------------------------------------------
def test_razorpay_accepts_its_own_signature() -> None:
    header = sign(WebhookProvider.RAZORPAY, BODY, SECRET, now=NOW)
    assert verify_razorpay(BODY, header, SECRET)


def test_razorpay_rejects_wrong_secret_and_tampered_body() -> None:
    header = sign(WebhookProvider.RAZORPAY, BODY, SECRET, now=NOW)
    assert not verify_razorpay(BODY, header, "other-secret")
    assert not verify_razorpay(BODY.replace(b"105000", b"1"), header, SECRET)


@pytest.mark.parametrize("header", ["", None, "not-hex"])
def test_razorpay_rejects_missing_or_junk_headers(header: str | None) -> None:
    assert not verify_razorpay(BODY, header, SECRET)


def test_razorpay_tolerates_surrounding_whitespace() -> None:
    """Proxies trim and pad header values; a leading space is not a forgery."""
    header = sign(WebhookProvider.RAZORPAY, BODY, SECRET, now=NOW)
    assert verify_razorpay(BODY, f"  {header}  ", SECRET)


# --- custom -----------------------------------------------------------------
def test_custom_round_trips() -> None:
    header = sign(WebhookProvider.CUSTOM, BODY, SECRET, now=NOW)
    assert verify_custom(BODY, header, SECRET)
    assert not verify_custom(BODY, header, "nope")


# --- dispatch ---------------------------------------------------------------
@pytest.mark.parametrize("provider", list(WebhookProvider))
def test_verify_dispatches_to_the_right_scheme(provider: WebhookProvider) -> None:
    header = sign(provider, BODY, SECRET, now=NOW)
    assert verify(provider, BODY, header, SECRET, now=NOW)
    assert not verify(provider, BODY, header, "wrong-secret", now=NOW)


def test_a_stripe_signature_does_not_verify_as_razorpay() -> None:
    """The provider on the source selects the scheme, so a misconfigured provider fails
    closed rather than accepting a differently-shaped signature."""
    stripe_header = sign(WebhookProvider.STRIPE, BODY, SECRET, now=NOW)
    assert not verify(WebhookProvider.RAZORPAY, BODY, stripe_header, SECRET, now=NOW)
