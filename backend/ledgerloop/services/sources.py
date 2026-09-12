"""Webhook source lookup and delivery bookkeeping.

Two responsibilities, kept apart from the route because they have different transaction
lifetimes: resolving a token happens inside the request's transaction, while recording
what happened to a delivery must survive the request *failing*.

That second point is the whole reason this module exists. A rejected delivery is
precisely the case an operator needs to see -- an endpoint receiving nothing and an
endpoint rejecting everything both leave the transaction tables empty, and the only
thing that tells them apart is ``last_delivery_status``. So the status write gets its
own session and its own commit, and happens before the 401 is raised rather than after.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.db.enums import WebhookDeliveryStatus, WebhookProvider
from ledgerloop.db.models import Account, WebhookSource
from ledgerloop.services.tenancy import Tenant

#: 32 URL-safe characters or so. In the path, so it must survive a URL untouched, and
#: unguessable so an endpoint cannot be found by enumeration even though the token
#: alone authorises nothing.
_TOKEN_BYTES = 24

#: The signing secret we generate for a `custom` source. Stripe and Razorpay issue
#: their own, which is supplied at creation instead.
_SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """A live source and the tenant it writes into.

    ``signing_secret`` is carried here because verification needs it and is deliberately
    the only field of this dataclass that must never be logged or serialised.
    """

    source_id: int
    provider: WebhookProvider
    signing_secret: str
    tenant: Tenant


def generate_source_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def generate_signing_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(_SECRET_BYTES)}"


async def resolve_source(session: AsyncSession, source_token: str) -> ResolvedSource | None:
    """Look a delivery's token up. None means unknown or revoked.

    Both answers are one answer on purpose: telling a caller that a token exists but is
    revoked confirms the token, which is the one bit an enumeration attack wants.
    """
    row = (
        await session.execute(
            select(WebhookSource, Account)
            .join(Account, WebhookSource.account_id == Account.id)
            .where(WebhookSource.source_token == source_token)
        )
    ).first()
    if row is None:
        return None

    source, account = row
    if source.revoked_at is not None:
        return None

    return ResolvedSource(
        source_id=source.id,
        provider=source.provider,
        signing_secret=source.signing_secret,
        tenant=Tenant(account_id=account.id, name=account.name, kind=account.kind),
    )


async def resolve_source_for_tenant(
    session: AsyncSession, tenant: Tenant, source_id: int
) -> ResolvedSource | None:
    """Look a source up *by id, within one account*.

    Scoped, so another account's source id is a 404 rather than a 403 -- the same
    choice the exception queue makes, and for the same reason: a 403 confirms the id
    exists, which is a fact about somebody else's configuration.
    """
    row = (
        await session.execute(
            select(WebhookSource, Account)
            .join(Account, WebhookSource.account_id == Account.id)
            .where(
                WebhookSource.id == source_id,
                WebhookSource.account_id == tenant.account_id,
            )
        )
    ).first()
    if row is None:
        return None

    source, account = row
    if source.revoked_at is not None:
        return None

    return ResolvedSource(
        source_id=source.id,
        provider=source.provider,
        signing_secret=source.signing_secret,
        tenant=Tenant(account_id=account.id, name=account.name, kind=account.kind),
    )


async def record_delivery(
    sessions: async_sessionmaker[AsyncSession],
    source_id: int,
    status: WebhookDeliveryStatus,
) -> None:
    """Stamp the outcome of one delivery, in its own transaction.

    Its own, not the request's, because the rejection paths raise an HTTPException and
    any work sharing that transaction would roll back with it -- which would leave the
    status column showing only successes, exactly inverting what it is for.

    ``last_event_at`` moves on every delivery including the rejected ones: the question
    it answers is "is anything arriving at all", and a stream of rejections is very much
    something arriving.
    """
    async with sessions() as session, session.begin():
        await session.execute(
            update(WebhookSource)
            .where(WebhookSource.id == source_id)
            .values(last_event_at=datetime.now(UTC), last_delivery_status=status)
        )


async def list_sources(session: AsyncSession, tenant: Tenant) -> list[WebhookSource]:
    """This account's sources. Scoped, like every other read on the API."""
    return list(
        (
            await session.execute(
                select(WebhookSource)
                .where(WebhookSource.account_id == tenant.account_id)
                .order_by(WebhookSource.id)
            )
        )
        .scalars()
        .all()
    )


async def create_source(
    session: AsyncSession,
    tenant: Tenant,
    provider: WebhookProvider,
    label: str,
    signing_secret: str | None = None,
) -> WebhookSource:
    """Register an endpoint. Caller owns the transaction.

    ``signing_secret`` is supplied for Stripe and Razorpay, because the provider issues
    it and we must hold the same bytes they sign with. For a custom source there is no
    external party, so one is generated here.
    """
    source = WebhookSource(
        account_id=tenant.account_id,
        provider=provider,
        source_token=generate_source_token(),
        signing_secret=signing_secret or generate_signing_secret(),
        label=label,
    )
    session.add(source)
    await session.flush()
    return source
