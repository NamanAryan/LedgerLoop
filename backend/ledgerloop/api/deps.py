"""FastAPI dependencies.

Everything hangs off ``app.state`` rather than module-level singletons, so a test can
build an app against a throwaway database and Redis without monkeypatching imports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerloop.config import Settings
from ledgerloop.queue.streams import StreamClient


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
StreamDep = Annotated[StreamClient, Depends(get_stream)]
IdempotencyKeyDep = Annotated[str, Depends(get_idempotency_key)]
