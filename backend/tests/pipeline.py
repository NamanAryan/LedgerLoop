"""Drive the async pipeline deterministically from a test.

Production runs the relay and the matcher as endless loops. A test that started those
loops and slept would be slow and flaky in equal measure, so instead it drives the same
objects one batch at a time and knows exactly when the work is finished. The code under
test is identical -- only the scheduling is.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ledgerloop.config import Settings
from ledgerloop.queue.relay import OutboxRelay
from ledgerloop.queue.streams import StreamClient
from ledgerloop.worker.loop import MatcherWorker, ProcessOutcome


async def publish_all(
    sessions: async_sessionmaker[AsyncSession], stream: StreamClient, settings: Settings
) -> int:
    """Drain the outbox onto the stream until it is empty."""
    relay = OutboxRelay(sessions, stream, settings)
    published = 0
    while True:
        count = await relay.drain_once()
        if count == 0:
            return published
        published += count


async def consume_all(
    worker: MatcherWorker, stream: StreamClient, *, limit: int = 10_000
) -> list[ProcessOutcome]:
    """Process and ack every entry currently on the stream."""
    outcomes: list[ProcessOutcome] = []
    while len(outcomes) < limit:
        entries = await stream.read(worker.consumer_name, count=256, block_ms=50)
        if not entries:
            return outcomes
        for entry_id, message in entries:
            outcomes.append(await worker.process(message, entry_id))
            await stream.ack(entry_id)
    return outcomes


async def run_pipeline(
    sessions: async_sessionmaker[AsyncSession],
    stream: StreamClient,
    settings: Settings,
    consumer_name: str = "test-worker",
) -> list[ProcessOutcome]:
    """Outbox -> stream -> matcher, to completion."""
    await publish_all(sessions, stream, settings)
    worker = MatcherWorker(sessions, stream, settings, consumer_name)
    return await consume_all(worker, stream)
