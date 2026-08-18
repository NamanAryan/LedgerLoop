"""Redis Streams wrapper.

Streams, not pub/sub. Pub/sub is fire-and-forget: a subscriber that is down when a
message is published never sees it, and there is no acknowledgement, so a matcher
crash silently loses transactions. A stream with a consumer group gives us

  * durable retention (the entry stays until trimmed, not until delivered),
  * XACK, so an unacked entry stays in the group's pending list,
  * automatic work distribution across N consumers in the same group,
  * XAUTOCLAIM, so a dead consumer's in-flight work is recoverable.

That is the difference between "a worker restart costs a few unreconciled payments"
and "a worker restart costs nothing".
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from ledgerloop.config import Settings, get_settings
from ledgerloop.queue.messages import IngestMessage


def build_redis(settings: Settings | None = None) -> Redis:
    settings = settings or get_settings()
    # decode_responses: we only ever put JSON strings on the stream, so decoding at
    # the client keeps every call site free of .decode() noise.
    return Redis.from_url(str(settings.redis_url), decode_responses=True)


class StreamClient:
    """Thin, testable wrapper over the handful of stream commands we use."""

    def __init__(self, redis: Redis, settings: Settings | None = None) -> None:
        self._redis = redis
        self._settings = settings or get_settings()

    @property
    def redis(self) -> Redis:
        return self._redis

    @property
    def key(self) -> str:
        return self._settings.stream_key

    @property
    def group(self) -> str:
        return self._settings.consumer_group

    # --- producer ---------------------------------------------------------
    async def publish(self, message: IngestMessage) -> str:
        entry_id: str = await self._redis.xadd(
            self.key,
            message.to_fields(),
            # approximate trimming: exact MAXLEN would force Redis to walk the radix
            # tree on every insert. '~' trims at node boundaries, which is O(1)-ish
            # and bounds memory just as well. Postgres is the durable record anyway.
            maxlen=self._settings.stream_maxlen,
            approximate=True,
        )
        return entry_id

    async def publish_many(self, messages: list[IngestMessage]) -> list[str]:
        if not messages:
            return []
        pipe = self._redis.pipeline(transaction=False)
        for message in messages:
            pipe.xadd(
                self.key,
                message.to_fields(),
                maxlen=self._settings.stream_maxlen,
                approximate=True,
            )
        return list(await pipe.execute())

    # --- consumer ---------------------------------------------------------
    async def ensure_group(self) -> None:
        """Create the stream and consumer group if absent. Safe to call concurrently.

        MKSTREAM so a worker can start before any producer has run -- otherwise a
        cold deploy order (worker first) would crash-loop on a missing key.
        """
        try:
            await self._redis.xgroup_create(self.key, self.group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(
        self, consumer: str, *, count: int | None = None, block_ms: int | None = None
    ) -> list[tuple[str, IngestMessage]]:
        """XREADGROUP with '>' -- only entries never delivered to this group.

        Blocking read rather than a poll loop: idle workers cost one blocked
        connection instead of a steady stream of empty round trips.
        """
        response: Any = await self._redis.xreadgroup(
            groupname=self.group,
            consumername=consumer,
            streams={self.key: ">"},
            count=count or self._settings.stream_batch_size,
            block=block_ms if block_ms is not None else self._settings.stream_block_ms,
        )
        return _flatten(response)

    async def claim_stale(
        self, consumer: str, *, min_idle_ms: int, count: int = 64
    ) -> list[tuple[str, IngestMessage]]:
        """XAUTOCLAIM: adopt entries a dead consumer never acked.

        This is the crash-recovery path. Without it, a worker that dies mid-message
        leaves that entry pending forever and the transaction is never reconciled --
        it would eventually surface as a false 'unmatched' exception.
        """
        result: Any = await self._redis.xautoclaim(
            name=self.key,
            groupname=self.group,
            consumername=consumer,
            min_idle_time=min_idle_ms,
            count=count,
        )
        # (next_cursor, entries, deleted) in redis-py >= 5
        entries = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
        return _decode_entries(entries)

    async def ack(self, *entry_ids: str) -> int:
        if not entry_ids:
            return 0
        acked: int = await self._redis.xack(self.key, self.group, *entry_ids)
        return acked

    # --- introspection (metrics, /ready) ----------------------------------
    async def depth(self) -> int:
        length: int = await self._redis.xlen(self.key)
        return length

    async def pending_count(self) -> int:
        try:
            summary: Any = await self._redis.xpending(self.key, self.group)
        except ResponseError:
            return 0  # group not created yet
        if not summary:
            return 0
        if isinstance(summary, dict):
            return int(summary.get("pending", 0))
        return int(summary[0])


def _flatten(response: Any) -> list[tuple[str, IngestMessage]]:
    """XREADGROUP returns [[stream_key, [(id, fields), ...]], ...]."""
    out: list[tuple[str, IngestMessage]] = []
    for _stream, entries in response or []:
        out.extend(_decode_entries(entries))
    return out


def _decode_entries(entries: Any) -> list[tuple[str, IngestMessage]]:
    out: list[tuple[str, IngestMessage]] = []
    for entry_id, fields in entries or []:
        if isinstance(entry_id, bytes):
            entry_id = entry_id.decode()
        out.append((entry_id, IngestMessage.from_fields(fields)))
    return out
