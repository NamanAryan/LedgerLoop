"""Write path: the ingestion endpoints.

Both return 202, never 201 and never 409. 202 is the accurate code: the record is
durably stored and queued, but it has not been reconciled yet, and the client should
not read that as a completed reconciliation. A duplicate submission also returns 202
with ``duplicate: true`` -- see the note on IngestAck for why answering a retry with
an error is the wrong move. The provider-signed endpoint below keeps that contract
exactly: gateways redeliver aggressively, and a 409 would make them redeliver harder.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, status

from ledgerloop.api.deps import IdempotencyKeyDep, SessionDep, TenantDep, WriteLimitDep
from ledgerloop.api.schemas import (
    GatewayWebhookAccepted,
    GatewayWebhookIn,
    IngestAck,
    LedgerSyncAccepted,
    LedgerSyncIn,
    WebhookAccepted,
    WebhookSourceCreate,
    WebhookSourceList,
    WebhookSourceOut,
    WebhookTestResult,
)
from ledgerloop.db.enums import WebhookDeliveryStatus, WebhookProvider
from ledgerloop.observability.logging import get_logger
from ledgerloop.observability.metrics import INGEST_REQUEST_SECONDS, INGEST_TOTAL
from ledgerloop.services.ingest import IngestOutcome, ingest_gateway, ingest_ledger_batch
from ledgerloop.services.sources import (
    create_source,
    list_sources,
    record_delivery,
    resolve_source,
    resolve_source_for_tenant,
)
from ledgerloop.webhooks import signatures
from ledgerloop.webhooks.adapters import AdapterError, adapt

router = APIRouter(prefix="/v1", tags=["ingestion"])
log = get_logger("ledgerloop.api.ingest")


def _ack(outcome: IngestOutcome) -> IngestAck:
    return IngestAck(
        row_id=outcome.row_id,
        txn_id=outcome.txn_id,
        duplicate=outcome.duplicate,
        submissions=outcome.submissions,
    )


@router.post(
    "/gateway/webhook",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=GatewayWebhookAccepted,
    summary="Accept one gateway transaction",
    # The limit is a route dependency rather than something the handler calls, so it
    # runs before the body is parsed and cannot be skipped by an early return added
    # later. This endpoint accepts writes with no credential at all -- see
    # api/ratelimit.py for what that means and why the limiter fails open.
    dependencies=[WriteLimitDep],
)
async def gateway_webhook(
    payload: GatewayWebhookIn,
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    idempotency_key: IdempotencyKeyDep,
) -> GatewayWebhookAccepted:
    started = time.perf_counter()
    # Starlette caches the request body, so this re-read is free and gives us the
    # bytes the gateway actually sent rather than our reserialisation of them.
    raw_body = await request.json()

    # Isolation: READ COMMITTED (PostgreSQL default). The raw row and its outbox event
    # commit together or not at all; idempotency comes from the unique index inside
    # this transaction, so nothing here needs a stronger snapshot.
    async with session.begin():
        outcome = await ingest_gateway(session, tenant, payload, idempotency_key, raw_body)

    INGEST_TOTAL.labels(
        endpoint="gateway_webhook", outcome="duplicate" if outcome.duplicate else "accepted"
    ).inc()
    INGEST_REQUEST_SECONDS.labels(endpoint="gateway_webhook").observe(time.perf_counter() - started)
    log.info(
        "gateway.ingested",
        txn_id=outcome.txn_id,
        row_id=outcome.row_id,
        duplicate=outcome.duplicate,
        submissions=outcome.submissions,
        tenant=tenant.name,
    )
    return GatewayWebhookAccepted(result=_ack(outcome))


@router.post(
    "/ledger/sync",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=LedgerSyncAccepted,
    summary="Accept a batch of up to 1000 ledger entries",
    dependencies=[WriteLimitDep],
)
async def ledger_sync(
    payload: LedgerSyncIn, session: SessionDep, tenant: TenantDep
) -> LedgerSyncAccepted:
    started = time.perf_counter()

    # Isolation: READ COMMITTED (PostgreSQL default). The whole batch is one
    # transaction: a partial batch would leave the client unable to tell which entries
    # to resend, and resending all of them is exactly what idempotency makes safe.
    async with session.begin():
        outcomes = await ingest_ledger_batch(session, tenant, payload.entries)

    duplicates = sum(1 for outcome in outcomes if outcome.duplicate)
    accepted = len(outcomes) - duplicates
    INGEST_TOTAL.labels(endpoint="ledger_sync", outcome="accepted").inc(accepted)
    if duplicates:
        INGEST_TOTAL.labels(endpoint="ledger_sync", outcome="duplicate").inc(duplicates)
    INGEST_REQUEST_SECONDS.labels(endpoint="ledger_sync").observe(time.perf_counter() - started)
    log.info(
        "ledger.ingested",
        accepted=accepted,
        duplicates=duplicates,
        batch=len(outcomes),
        tenant=tenant.name,
    )

    return LedgerSyncAccepted(
        accepted=accepted,
        duplicates=duplicates,
        results=[_ack(outcome) for outcome in outcomes],
    )


@router.post(
    "/gateway/webhook/{source_token}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAccepted,
    summary="Accept a signed delivery from a configured payment provider",
    # No WriteLimitDep and no TenantDep here, and both omissions are deliberate.
    #
    # TenantDep resolves a caller from headers; this endpoint's caller is Stripe, which
    # sends no Authorization and no demo session. The tenant comes from the source the
    # path token resolves to, so running TenantDep first would resolve the *shared demo
    # tenant* and then throw the answer away -- creating an account row on every
    # delivery and hiding a real bug behind a plausible default.
    #
    # The rate limiter is skipped because the credential here is a signature, not an
    # absent one: an unsigned request is already rejected, and throttling a gateway's
    # legitimate retry storm with a 429 makes it retry harder and eventually disables
    # the endpoint.
)
async def provider_webhook(
    request: Request,
    session: SessionDep,
    source_token: Annotated[str, Path(min_length=8, max_length=64)],
) -> WebhookAccepted:
    started = time.perf_counter()

    # The raw bytes, before anything parses them. This is the single most important
    # line in the endpoint: the HMAC covers exactly what the provider sent, and binding
    # a Pydantic model here and re-serialising it would change key order and whitespace
    # and make every signature fail with a "wrong secret" that is nothing of the kind.
    # It is also why the body is not a typed parameter -- FastAPI would parse it for us,
    # which is precisely what must not happen first.
    raw_body = await request.body()

    resolved = await resolve_source(session, source_token)
    if resolved is None:
        # Unknown and revoked give the same answer. Distinguishing them would confirm
        # that a guessed token was once real, which is the one bit enumeration wants.
        log.warning("webhook.unknown_source")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown source")

    # The lookup above opened an implicit read transaction on this session; close it
    # before the ingest block opens its own. (The read-path routes get this for free --
    # resolve_tenant commits -- but this endpoint resolves its caller from the path
    # token instead, so it has to end its own read.) Nothing is lost by releasing the
    # snapshot here: the source has been read into a value object, and holding a
    # transaction open across signature verification would pin it for the whole request.
    await session.commit()

    sessions = request.app.state.sessionmaker
    header = request.headers.get(signatures.SIGNATURE_HEADER[resolved.provider])
    verdict = signatures.verify(
        resolved.provider,
        raw_body,
        header,
        resolved.signing_secret,
        now=datetime.now(UTC),
    )
    if not verdict:
        # Recorded before raising, in its own transaction, so an endpoint that rejects
        # everything is visibly different from one receiving nothing. verdict.reason is
        # written by signatures.py and never contains the secret or a computed digest.
        await record_delivery(sessions, resolved.source_id, WebhookDeliveryStatus.INVALID_SIGNATURE)
        log.warning(
            "webhook.signature_rejected",
            provider=resolved.provider.value,
            source_id=resolved.source_id,
            reason=verdict.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature"
        )

    try:
        body = json.loads(raw_body)
        if not isinstance(body, dict):
            raise AdapterError("payload is not a JSON object")
        adapted = adapt(resolved.provider, body)
    except (AdapterError, json.JSONDecodeError) as exc:
        await record_delivery(sessions, resolved.source_id, WebhookDeliveryStatus.INVALID_PAYLOAD)
        log.warning(
            "webhook.payload_rejected",
            provider=resolved.provider.value,
            source_id=resolved.source_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    # Isolation: READ COMMITTED, and the same ingest_gateway the unauthenticated
    # endpoint calls. The provider path adds authentication and a payload mapping and
    # nothing else -- there is one place that writes a gateway row, so idempotency and
    # the outbox cannot drift between the two entry points.
    async with session.begin():
        outcome = await ingest_gateway(
            session,
            resolved.tenant,
            adapted.payload,
            adapted.idempotency_key,
            # The provider's own body, stored verbatim. A disputed reconciliation is
            # then arguable from what Stripe actually sent rather than from our reading
            # of it -- which is the same reason the unauthenticated path keeps its
            # raw JSON, and matters more here because we did the mapping.
            body,
        )

    await record_delivery(sessions, resolved.source_id, WebhookDeliveryStatus.OK)

    INGEST_TOTAL.labels(
        endpoint="provider_webhook", outcome="duplicate" if outcome.duplicate else "accepted"
    ).inc()
    INGEST_REQUEST_SECONDS.labels(endpoint="provider_webhook").observe(
        time.perf_counter() - started
    )
    log.info(
        "webhook.ingested",
        provider=resolved.provider.value,
        event_type=adapted.event_type,
        txn_id=outcome.txn_id,
        row_id=outcome.row_id,
        duplicate=outcome.duplicate,
        tenant=resolved.tenant.name,
    )
    return WebhookAccepted(
        provider=resolved.provider,
        event_type=adapted.event_type,
        result=_ack(outcome),
    )


@router.get(
    "/gateway/sources",
    response_model=WebhookSourceList,
    summary="The caller's configured webhook sources",
)
async def list_gateway_sources(session: SessionDep, tenant: TenantDep) -> WebhookSourceList:
    """Scoped like every other read. A demo tenant has no sources and gets an empty
    list rather than an error -- which is what lets the dashboard ask unconditionally
    and simply show nothing when there is nothing to show."""
    rows = await list_sources(session, tenant)
    return WebhookSourceList(items=[_source_out(row) for row in rows])


def _source_out(row) -> WebhookSourceOut:  # type: ignore[no-untyped-def]
    """Flatten a source onto the wire. ``signing_secret`` is absent, and its absence is
    the whole of its protection -- it cannot be hashed, because HMAC verification needs
    the bytes themselves."""
    return WebhookSourceOut(
        id=row.id,
        provider=row.provider,
        source_token=row.source_token,
        label=row.label,
        last_event_at=row.last_event_at,
        last_delivery_status=row.last_delivery_status,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


@router.post(
    "/gateway/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=WebhookSourceOut,
    summary="Register a webhook endpoint for the calling account",
)
async def create_gateway_source(
    session: SessionDep, tenant: TenantDep, payload: WebhookSourceCreate
) -> WebhookSourceOut:
    """201, not 202: unlike ingestion this creates a resource, and the caller needs its
    token back immediately to configure the far end.

    Demo tenants are refused. A demo account is deleted 24 hours after it goes idle,
    taking the endpoint with it -- so the URL would work, be pasted into a gateway's
    dashboard, and then start 404ing with nothing to explain why. Refusing up front is
    kinder than an endpoint with a hidden expiry.
    """
    if tenant.is_demo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a live endpoint requires an account key; demo tenants expire after 24h",
        )

    async with session.begin():
        source = await create_source(session, tenant, payload.provider, payload.label)
        created = _source_out(source)

    log.info(
        "webhook.source_created",
        provider=payload.provider.value,
        source_id=created.id,
        tenant=tenant.name,
    )
    return created


@router.post(
    "/gateway/sources/{source_id}/test",
    response_model=WebhookTestResult,
    summary="Deliver a synthetic signed event to this source",
)
async def test_gateway_source(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    source_id: Annotated[int, Path(ge=1)],
) -> WebhookTestResult:
    """Sign a synthetic payload with the source's own secret and run it through the real
    path: verify, adapt, ingest.

    Signed on the server because the secret never leaves it -- which is also why this is
    an endpoint at all rather than the dashboard posting to the webhook URL directly.
    The dashboard holds no secret and could only ever produce a 401.

    What this does *not* exercise is the hop from the provider: DNS, TLS, the proxy and
    a cold start all sit outside it. It answers "is this source wired up correctly", not
    "can Stripe reach me" -- a distinction worth remembering when it passes and real
    deliveries still do not arrive.
    """
    source = await resolve_source_for_tenant(session, tenant, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown source")
    # Close the implicit read transaction before the ingest block opens its own.
    await session.commit()

    signed_at = datetime.now(UTC)
    body = _synthetic_event(source.provider, signed_at)
    raw_body = json.dumps(body).encode()
    header = signatures.sign(source.provider, raw_body, source.signing_secret, now=signed_at)

    # The same verification the real endpoint runs, rather than a skip: a test delivery
    # that passes should prove the signing path works, not prove we can bypass it.
    verdict = signatures.verify(
        source.provider, raw_body, header, source.signing_secret, now=signed_at
    )
    sessions = request.app.state.sessionmaker
    if not verdict:
        await record_delivery(sessions, source.source_id, WebhookDeliveryStatus.INVALID_SIGNATURE)
        return WebhookTestResult(
            delivered=False,
            http_status=status.HTTP_401_UNAUTHORIZED,
            duplicate=False,
            detail=verdict.reason,
        )

    try:
        adapted = adapt(source.provider, body)
    except AdapterError as exc:
        await record_delivery(sessions, source.source_id, WebhookDeliveryStatus.INVALID_PAYLOAD)
        return WebhookTestResult(
            delivered=False,
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            duplicate=False,
            detail=str(exc),
        )

    async with session.begin():
        outcome = await ingest_gateway(
            session, source.tenant, adapted.payload, adapted.idempotency_key, body
        )
    await record_delivery(sessions, source.source_id, WebhookDeliveryStatus.OK)

    log.info(
        "webhook.test_delivered",
        source_id=source.source_id,
        provider=source.provider.value,
        duplicate=outcome.duplicate,
        tenant=tenant.name,
    )
    return WebhookTestResult(
        delivered=True,
        http_status=status.HTTP_202_ACCEPTED,
        duplicate=outcome.duplicate,
        txn_id=outcome.txn_id,
    )


def _synthetic_event(provider: WebhookProvider, at: datetime) -> dict[str, Any]:
    """A minimal, provider-shaped event.

    The event id carries a timestamp, so two tests a second apart are two transactions
    -- while a double-clicked button inside the same second collapses to
    ``duplicate: true``, which is the idempotency layer working and is reported as such
    rather than as a failure.

    105000 minor units is 1050.00 major. Chosen so a broken minor-unit conversion shows
    up on the dashboard as an obviously wrong number rather than a plausible one.
    """
    stamp = int(at.timestamp())
    txn_id = f"LEDGERLOOP-TEST-{stamp}"
    if provider is WebhookProvider.STRIPE:
        return {
            "id": f"evt_test_{stamp}",
            "type": "payment_intent.succeeded",
            "created": stamp,
            "data": {
                "object": {
                    "id": f"pi_test_{stamp}",
                    "amount": 105000,
                    "currency": "inr",
                    "created": stamp,
                    "metadata": {"txn_id": txn_id},
                }
            },
        }
    if provider is WebhookProvider.RAZORPAY:
        return {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_test_{stamp}",
                        "amount": 105000,
                        "currency": "INR",
                        "created_at": stamp,
                        "notes": {"txn_id": txn_id},
                    }
                }
            },
        }
    return {
        "txn_id": txn_id,
        "amount": "1050.00",
        "currency": "INR",
        "occurred_at": at.isoformat(),
        "gateway_ref": f"REF-TEST-{stamp}",
    }
