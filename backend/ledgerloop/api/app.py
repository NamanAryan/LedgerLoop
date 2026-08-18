"""FastAPI application factory.

A factory, not a module-level ``app = FastAPI()``: tests build an app bound to a
throwaway database and Redis, and nothing has to be monkeypatched to make that work.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ledgerloop.api import routes_ingest, routes_ops, routes_read
from ledgerloop.config import Settings, get_settings
from ledgerloop.db.session import build_engine, build_sessionmaker
from ledgerloop.observability.logging import configure_logging, get_logger
from ledgerloop.queue.streams import StreamClient, build_redis

log = get_logger("ledgerloop.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings)
        redis = build_redis(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = build_sessionmaker(engine)
        app.state.stream = StreamClient(redis, settings)

        # The API creates the consumer group so a cold start in any service order
        # works: whichever of API or worker comes up first, the group exists before
        # the first message is published.
        try:
            await app.state.stream.ensure_group()
        except Exception as exc:  # noqa: BLE001
            # Not fatal. Ingestion still commits to Postgres, the outbox holds the
            # events, and the relay drains them once Redis returns.
            log.warning("stream.group_create_failed", error=str(exc))

        log.info("api.started", service=settings.service_name)
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()
            log.info("api.stopped")

    app = FastAPI(
        title="LedgerLoop",
        version="0.1.0",
        summary="Payment reconciliation engine.",
        description=(
            "Ingests a payment gateway's webhook feed and a merchant's ledger, "
            "reconciles them asynchronously, and reports matches, drift, and breaks."
        ),
        lifespan=lifespan,
    )
    # Read-only GETs plus one POST (exception resolve), so the preflight surface is
    # small. Credentials are off: there is no cookie session to protect, and leaving
    # them on would rule out the wildcard fallback a self-hosted deployment may want.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    app.include_router(routes_ingest.router)
    app.include_router(routes_read.router)
    app.include_router(routes_ops.router)
    return app


app = create_app()
