"""Provider signature verification. Pure: bytes and strings in, a verdict out.

Everything here is a function of its arguments -- no clock, no database, no request
object -- which is what lets a replay-window boundary at exactly 300.000s be a one-line
unit test instead of a fixture that sleeps.

Three rules hold for every scheme below, and each one is a way this gets silently
broken rather than a style preference.

**Verify the raw request bytes, never a re-serialisation.** The HMAC covers exactly
what the provider sent. Binding a Pydantic model and dumping it back to JSON changes
key order, whitespace, unicode escaping and float formatting -- all of which are
invisible in the parsed object and all of which change the digest. The failure looks
like "the secret is wrong", which sends you to rotate a secret that was always fine.

**Compare in constant time.** ``hmac.compare_digest`` everywhere. A ``==`` on a digest
leaks, through timing, how many leading bytes a guess got right, which turns forging a
signature into a byte-at-a-time search instead of a 2^256 one.

**A failure returns a reason, never the secret.** ``Verdict.reason`` is safe to log.
Nothing in this module puts the signing secret, or a computed digest, into a message.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

from ledgerloop.db.enums import WebhookProvider

#: How far a signed timestamp may sit from now before the delivery is refused. Stripe's
#: own default, and the reason the timestamp is inside the signed payload at all: without
#: a window, a captured request stays replayable for as long as the secret lives.
DEFAULT_TOLERANCE = timedelta(minutes=5)

#: Header each provider signs into.
SIGNATURE_HEADER: dict[WebhookProvider, str] = {
    WebhookProvider.STRIPE: "Stripe-Signature",
    WebhookProvider.RAZORPAY: "X-Razorpay-Signature",
    WebhookProvider.CUSTOM: "X-LedgerLoop-Signature",
}


@dataclass(frozen=True, slots=True)
class Verdict:
    """Why a delivery was accepted or refused. ``reason`` is always safe to log."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _hexdigest(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_stripe(
    raw_body: bytes,
    header: str | None,
    secret: str,
    *,
    now: datetime,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> Verdict:
    """Stripe's ``Stripe-Signature``: ``t=<unix>,v1=<hex>[,v1=<hex>...]``.

    The signed payload is ``{timestamp}.{raw_body}`` -- the timestamp is *inside* the
    HMAC, so it cannot be edited to slip an old capture past the window.

    Multiple ``v1`` entries are normal, not an attack: Stripe sends one per active
    secret while an endpoint's secret is being rotated, so a verifier that reads only
    the first one breaks every rotation. Any match is a pass.
    """
    if not header:
        return Verdict(False, "missing signature header")

    timestamp: str | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)

    if timestamp is None or not candidates:
        return Verdict(False, "malformed signature header")

    try:
        signed_at = datetime.fromtimestamp(int(timestamp), tz=now.tzinfo)
    except (ValueError, OverflowError, OSError):
        return Verdict(False, "malformed signature timestamp")

    # Absolute difference, so a timestamp in the future is refused too. A clock that
    # far ahead is either badly wrong or forged, and neither should be accepted.
    if abs(now - signed_at) > tolerance:
        return Verdict(False, "timestamp outside tolerance")

    expected = _hexdigest(secret, f"{timestamp}.".encode() + raw_body)
    # Every candidate is compared even after a match, so the work done does not depend
    # on which position matched. compare_digest already handles the per-comparison leak.
    if any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        return Verdict(True)
    return Verdict(False, "signature mismatch")


def verify_razorpay(raw_body: bytes, header: str | None, secret: str) -> Verdict:
    """Razorpay's ``X-Razorpay-Signature``: a bare HMAC-SHA256 hex of the raw body.

    No timestamp, so there is no replay window to enforce here -- Razorpay does not
    sign one, and inventing one from a field inside the body would be checking a value
    the attacker controls. Replay protection on this path comes from ingestion
    idempotency instead: a replayed delivery carries the same event id, so it collapses
    to ``duplicate: true`` and writes no second transaction.
    """
    if not header:
        return Verdict(False, "missing signature header")
    expected = _hexdigest(secret, raw_body)
    if hmac.compare_digest(expected, header.strip()):
        return Verdict(True)
    return Verdict(False, "signature mismatch")


def verify_custom(raw_body: bytes, header: str | None, secret: str) -> Verdict:
    """LedgerLoop's own scheme, for a merchant posting from their own systems.

    Deliberately identical in shape to Razorpay's -- HMAC-SHA256 hex over the raw body
    -- because it is the simplest thing a caller can implement correctly in any
    language in four lines, and a scheme people get wrong is worse than a plain one.
    """
    if not header:
        return Verdict(False, "missing signature header")
    expected = _hexdigest(secret, raw_body)
    if hmac.compare_digest(expected, header.strip()):
        return Verdict(True)
    return Verdict(False, "signature mismatch")


def sign(provider: WebhookProvider, raw_body: bytes, secret: str, *, now: datetime) -> str:
    """Produce a valid header for ``provider``. Used by tests and by the docs example.

    Kept beside the verifiers on purpose: a signer written separately drifts from the
    verifier, and then the tests prove the two agree with each other rather than that
    either matches the provider.
    """
    if provider is WebhookProvider.STRIPE:
        timestamp = int(now.timestamp())
        digest = _hexdigest(secret, f"{timestamp}.".encode() + raw_body)
        return f"t={timestamp},v1={digest}"
    return _hexdigest(secret, raw_body)


def verify(
    provider: WebhookProvider,
    raw_body: bytes,
    header: str | None,
    secret: str,
    *,
    now: datetime,
    tolerance: timedelta = DEFAULT_TOLERANCE,
) -> Verdict:
    """Dispatch to the scheme this source's provider uses."""
    if provider is WebhookProvider.STRIPE:
        return verify_stripe(raw_body, header, secret, now=now, tolerance=tolerance)
    if provider is WebhookProvider.RAZORPAY:
        return verify_razorpay(raw_body, header, secret)
    return verify_custom(raw_body, header, secret)
