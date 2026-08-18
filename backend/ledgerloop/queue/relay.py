"""Outbox relay: moves committed outbox rows onto the Redis Stream.

The claim query is the interesting part:

    SELECT ... WHERE published_at IS NULL ORDER BY id
    LIMIT :n FOR UPDATE SKIP LOCKED

``FOR UPDATE`` locks the rows this relay is about to publish. ``SKIP LOCKED`` makes a
second relay step over them and take the next batch instead of blocking behind the
first. That is what lets the relay scale horizontally without any coordination, leader
election, or partition assignment.

Delivery is at-least-once, not exactly-once. If the process dies after XADD and before
COMMIT, the rows unlock still unpublished and the next pass republishes them. Chasing
exactly-once here would need a distributed transaction across Postgres and Redis; we
get the same end result far more cheaply by making the *consumer* idempotent, which
the partial unique indexes on reconciliation_results already do.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.config import Settings
from ledgerloop.db.models import OutboxEvent
from ledgerloop.observability.logging import get_logger
from ledgerloop.observability.metrics import (
    OUTBOX_BACKLOG,
    OUTBOX_PUBLISHED_TOTAL,
    record_error,
)
from ledgerloop.queue.messages import IngestMessage
from ledgerloop.queue.streams import StreamClient

log = get_logger("ledgerloop.relay")


class OutboxRelay:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        stream: StreamClient,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._stream = stream
        self._settings = settings

    async def drain_once(self) -> int:
        """Publish one batch. Returns how many rows were published."""
        async with self._sessionmaker() as session:
            # Isolation: READ COMMITTED (PostgreSQL default). SKIP LOCKED gives this
            # batch exclusive ownership of its rows regardless of snapshot semantics,
            # so a stricter level would only add serialisation failures between relays.
            async with session.begin():
                claim = (
                    select(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                    .order_by(OutboxEvent.id)
                    .limit(self._settings.outbox_batch_size)
                    .with_for_update(skip_locked=True)
                )
                rows = list((await session.execute(claim)).scalars().all())
                if not rows:
                    return 0

                messages = [IngestMessage.from_payload(row.payload) for row in rows]
                await self._stream.publish_many(messages)

                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id.in_([row.id for row in rows]))
                    .values(published_at=func.now(), attempts=OutboxEvent.attempts + 1)
                )

        OUTBOX_PUBLISHED_TOTAL.inc(len(rows))
        return len(rows)

    async def _refresh_backlog_gauge(self) -> None:
        async with self._sessionmaker() as session:
            backlog = (
                await session.execute(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(OutboxEvent.published_at.is_(None))
                )
            ).scalar_one()
        OUTBOX_BACKLOG.set(backlog)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Drain continuously; sleep only when the outbox is empty.

        A full batch means more work is waiting, so we go straight round again rather
        than sleeping -- under load the relay behaves like a tight loop, and at idle it
        polls gently.
        """
        log.info("relay.started", batch_size=self._settings.outbox_batch_size)
        while not stop.is_set():
            try:
                published = await self.drain_once()
                if published == 0:
                    await self._refresh_backlog_gauge()
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=self._settings.outbox_poll_interval_s
                        )
                    except TimeoutError:
                        pass
                elif published < self._settings.outbox_batch_size:
                    await self._refresh_backlog_gauge()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- the relay must outlive any single failure
                record_error("relay", exc)
                log.error("relay.batch_failed", error=str(exc), exc_info=True)
                # Back off briefly. Rows stay unpublished and are retried, so the only
                # cost of an error here is latency, never a lost event.
                await asyncio.sleep(min(self._settings.outbox_poll_interval_s * 5, 2.0))
        log.info("relay.stopped")
