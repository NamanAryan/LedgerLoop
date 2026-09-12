"""FastAPI dependencies.

Everything hangs off ``app.state`` rather than module-level singletons, so a test can
build an app against a throwaway database and Redis without monkeypatching imports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerloop.api import ratelimit
from ledgerloop.config import Settings
from ledgerloop.observability.logging import get_logger
from ledgerloop.queue.streams import StreamClient
from ledgerloop.services.tenancy import (
    DEFAULT_ACCOUNT_NAME,
    Tenant,
    demo_account_name,
    parse_bearer,
    resolve_api_key,
    resolve_demo_account,
    touch_account,
)

log = get_logger("ledgerloop.api.deps")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Closed on the way out, whatever happened."""
    async with request.app.state.sessionmaker() as session:
        yield session


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_stream(request: Request) -> StreamClient:
    return request.app.state.stream  # type: ignore[no-any-return]


async def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """Required on the gateway webhook.

    Rejecting the request outright is the honest choice: without a client-supplied key
    we cannot tell a retry from a genuine second payment of the same amount in the same
    second, and guessing wrong either drops real money or double-counts it.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    if len(idempotency_key) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be at most 255 characters",
        )
    return idempotency_key


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


async def resolve_tenant(
    session: SessionDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    x_demo_session: Annotated[str | None, Header()] = None,
) -> Tenant:
    """Whose data is this request about? The single answer, used by every /v1 route.

    Resolution order, and why each rung is where it is:

    1. A valid ``Authorization: Bearer`` -> that account.
    2. An ``Authorization`` header that does not resolve -> 401, never a fallback.
       Serving a broken integration the shared demo tenant instead would hand it a
       dashboard that looks like it is working, full of somebody else's synthetic
       rows. An error gets investigated; a plausible screen does not.
    3. ``X-Demo-Session: <uuid>`` -> that visitor's own demo tenant, created on
       first sight. Isolation between visitors, not authentication: the header is
       client-chosen and guessable, so nothing real belongs on this path.
    4. Neither header -> the shared default demo tenant. This rung exists so
       ``scripts/generate_load`` and ``scripts/benchmark`` keep working untouched:
       they send no headers, so they all land here and the ground-truth comparison
       against /v1/stats still sees exactly the rows they posted.

    ``/health``, ``/ready`` and ``/metrics`` sit outside this dependency: they are
    process-level questions and have no tenant.
    """
    if authorization is not None:
        raw_key = parse_bearer(authorization)
        tenant = await resolve_api_key(session, raw_key) if raw_key else None
        if tenant is None:
            # No detail about *why* -- malformed, unknown and revoked are one answer
            # to the caller. Distinguishing them turns this into an oracle for
            # probing which keys exist.
            log.warning("auth.rejected")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
    elif x_demo_session is not None:
        name = demo_account_name(x_demo_session)
        if name is None:
            # 400, not a silent fall-through to the shared tenant: a client sending a
            # malformed session id would otherwise have its data quietly pooled with
            # every other anonymous visitor's and never know.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Demo-Session must be a UUID",
            )
        tenant = await resolve_demo_account(session, name)
    else:
        tenant = await resolve_demo_account(session, DEFAULT_ACCOUNT_NAME)

    await touch_account(session, tenant, settings.last_seen_refresh_s)
    # Committed here rather than left to the route: a read-only endpoint never opens a
    # transaction of its own, so without this the account row (and its refreshed
    # last_seen_at) would be rolled back when the session closes, and an actively
    # polled demo session would still look idle to retention.
    await session.commit()
    return tenant


TenantDep = Annotated[Tenant, Depends(resolve_tenant)]


async def enforce_write_limit(
    request: Request,
    response: Response,
    tenant: TenantDep,
    settings: SettingsDep,
) -> None:
    """Rate-limit writes. Unauthenticated callers are limited per IP, keyed ones per
    account and far more loosely.

    Per IP rather than per demo session for the anonymous case, deliberately: the
    session id is chosen by the caller, so limiting on it would let anyone mint a new
    UUID and reset their own budget. The IP is the cheapest identifier the caller does
    not control outright.
    """
    if tenant.is_demo:
        bucket = f"ip:{_client_ip(request)}"
        limit = settings.rate_limit_anon_writes_per_min
    else:
        bucket = f"account:{tenant.account_id}"
        limit = settings.rate_limit_keyed_writes_per_min

    verdict = await ratelimit.check(request.app.state.stream.redis, bucket, limit)
    response.headers["RateLimit-Limit"] = str(verdict.limit)
    response.headers["RateLimit-Remaining"] = str(verdict.remaining)
    if not verdict.allowed:
        log.warning("ratelimit.exceeded", bucket=bucket, limit=limit)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="write rate limit exceeded",
            headers={"Retry-After": str(verdict.retry_after_s)},
        )


def _client_ip(request: Request) -> str:
    """The caller's address, honouring one hop of ``X-Forwarded-For``.

    Render, Fly and Vercel all terminate TLS in front of the app, so ``request.client``
    is the proxy for every request and limiting on it would put every visitor in one
    bucket. The *first* entry is taken because that is the one the trusted proxy
    appended; entries further left are supplied by the client and are not evidence.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


WriteLimitDep = Depends(enforce_write_limit)

StreamDep = Annotated[StreamClient, Depends(get_stream)]
IdempotencyKeyDep = Annotated[str, Depends(get_idempotency_key)]
