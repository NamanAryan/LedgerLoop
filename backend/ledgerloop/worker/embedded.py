"""The worker's background loops, hosted inside the API process.

``worker/main.py`` runs the matcher, the outbox relay and the sweeper as their own
container, and that is the right shape: a slow match must not slow a webhook response,
and the two scale on different signals. This module is the same three loops on the
API's event loop instead, for deployments where a second always-on process costs money
that a demo does not have.

What moves is the process boundary and nothing else. The loops are the identical
``MatcherWorker``, ``OutboxRelay`` and ``Sweeper`` objects, driven by the identical
``asyncio.Event`` shutdown protocol. There is no second implementation to keep in step,
which is the only reason this is an acceptable thing to offer at all.

What it costs, stated so nobody has to rediscover it:

* **Backpressure isolation.** A matching backlog now competes with ingestion for one
  event loop. Under sustained load the API's p99 degrades, and a gateway seeing slow
  responses retries transactions that were never lost.
* **Independent scaling.** Ingestion is cheap and spiky, matching is expensive. One
  process means one scaling knob for both.
* **Deploy coupling.** Restarting the API now also restarts the matcher.

None of those are correctness problems, which is the point -- effectively-once still
comes from the partial unique indexes, not from where the code runs.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.config import Settings
from ledgerloop.observability.logging import get_logger
from ledgerloop.queue.relay import OutboxRelay
from ledgerloop.queue.streams import StreamClient
from ledgerloop.worker.loop import MatcherWorker
from ledgerloop.worker.main import default_consumer_name
from ledgerloop.worker.sweeper import Sweeper

log = get_logger("ledgerloop.worker.embedded")


class EmbeddedWorker:
    """Owns the background tasks for one API process."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        stream: StreamClient,
        settings: Settings,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._stream = stream
        self._settings = settings
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Spawn the loops. Returns as soon as they are scheduled, never blocking
        startup -- the API must be answering /health before the first match runs."""
        settings = self._settings

        for index in range(settings.worker_concurrency):
            worker = MatcherWorker(
                self._sessionmaker, self._stream, settings, default_consumer_name(index)
            )
            self._tasks.append(
                asyncio.create_task(worker.run(self._stop), name=f"embedded-matcher-{index}")
            )

        if settings.enable_relay:
            relay = OutboxRelay(self._sessionmaker, self._stream, settings)
            self._tasks.append(
                asyncio.create_task(relay.run_forever(self._stop), name="embedded-relay")
            )

        if settings.enable_sweeper:
            sweeper = Sweeper(self._sessionmaker, settings)
            self._tasks.append(
                asyncio.create_task(sweeper.run_forever(self._stop), name="embedded-sweeper")
            )

        log.warning(
            "worker.embedded_in_api",
            tasks=[task.get_name() for task in self._tasks],
            detail=(
                "matching shares the API event loop; ingestion latency is no longer "
                "isolated from matching load. Run worker/main.py as its own process "
                "wherever that is affordable."
            ),
        )

    async def stop(self) -> None:
        """Signal the loops and wait for them.

        Each loop finishes and acks the message in hand before returning, so this is a
        clean drain rather than a cancellation -- the same guarantee the standalone
        worker gives on SIGTERM. Cancellation is only the fallback for a task that
        ignores the event.
        """
        if not self._tasks:
            return

        self._stop.set()
        done, pending = await asyncio.wait(self._tasks, timeout=self._shutdown_timeout())

        for task in pending:
            log.warning("worker.embedded_task_cancelled", task=task.get_name())
            task.cancel()
        if pending:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if exc is not None:
                log.error("worker.embedded_task_failed", task=task.get_name(), error=str(exc))

        self._tasks.clear()
        log.info("worker.embedded_stopped")

    def _shutdown_timeout(self) -> float:
        """Long enough for a blocked XREADGROUP to return and the loop to notice the
        stop event, with headroom for the message in hand."""
        return max(self._settings.stream_block_ms / 1000 * 2, 10.0)
