"""Matcher worker entry point: ``python -m ledgerloop.worker.main``.

A separate process from the API, and its own container, for two reasons that are worth
being precise about:

* **Backpressure isolation.** If matching slows down -- a long sweep, a Postgres
  hiccup, a burst of drift cases -- ingestion keeps returning 202 at full speed. The
  stream absorbs the difference. An in-process background task would share the API's
  event loop, so a slow matcher would show up as slow webhook responses, and the
  gateway would start retrying transactions that were never actually lost.
* **Independent scaling.** Ingestion is cheap and spiky; matching is the expensive
  part. Consumer groups let matcher containers be scaled on their own, and Redis
  distributes entries across them with no coordination on our side.

The process runs three cooperating tasks: the matcher loop(s), the outbox relay, and
the sweeper. They share one event loop and one connection pool, and all three shut
down together on SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys

from prometheus_client import start_http_server

from ledgerloop.config import Settings, get_settings
from ledgerloop.db.session import build_engine, build_sessionmaker
from ledgerloop.observability.logging import configure_logging, get_logger
from ledgerloop.observability.metrics import REGISTRY
from ledgerloop.queue.relay import OutboxRelay
from ledgerloop.queue.streams import StreamClient, build_redis
from ledgerloop.worker.loop import MatcherWorker
from ledgerloop.worker.sweeper import Sweeper

log = get_logger("ledgerloop.worker.main")


def default_consumer_name(index: int = 0) -> str:
    """Stable per-container identity.

    Hostname-based because in Docker/Kubernetes the hostname is the container name, so
    a restarted worker reclaims its own pending entries instead of orphaning them under
    a name nothing will ever use again.
    """
    return f"{socket.gethostname()}-{os.getpid()}-{index}"


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings)

    engine = build_engine(settings)
    sessionmaker = build_sessionmaker(engine)
    redis = build_redis(settings)
    stream = StreamClient(redis, settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: add_signal_handler is unsupported on the Proactor loop. The
            # fallback still gives a clean shutdown for local development.
            signal.signal(sig, lambda *_: stop.set())

    try:
        start_http_server(settings.worker_metrics_port, registry=REGISTRY)
        log.info("worker.metrics_listening", port=settings.worker_metrics_port)
    except OSError as exc:
        log.warning("worker.metrics_unavailable", error=str(exc))

    tasks: list[asyncio.Task[None]] = []
    for index in range(settings.worker_concurrency):
        worker = MatcherWorker(sessionmaker, stream, settings, default_consumer_name(index))
        tasks.append(asyncio.create_task(worker.run(stop), name=f"matcher-{index}"))

    if settings.enable_relay:
        relay = OutboxRelay(sessionmaker, stream, settings)
        tasks.append(asyncio.create_task(relay.run_forever(stop), name="relay"))

    if settings.enable_sweeper:
        sweeper = Sweeper(sessionmaker, settings)
        tasks.append(asyncio.create_task(sweeper.run_forever(stop), name="sweeper"))

    log.info("worker.ready", tasks=[task.get_name() for task in tasks])

    try:
        # Every task exits on its own once `stop` is set, having finished and acked the
        # message in hand. Nothing is cancelled mid-write.
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        await redis.aclose()
        await engine.dispose()
        log.info("worker.shutdown_complete")


def main() -> int:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
