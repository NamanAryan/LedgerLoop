"""Per-caller write rate limiting, in Redis.

``POST /v1/gateway/webhook`` accepts writes with no credential at all -- that is what
keeps the public demo and the load generator working -- which makes it an open write
into the database. A limit is the thing standing between "anyone can try the demo" and
"anyone can fill the disk".

Fixed window, not a sliding log or a token bucket. A fixed window admits at most 2x the
configured rate across a window boundary, and that is fine here: this is a guard on an
open endpoint, not a billing meter, and the alternatives cost either a sorted set per
caller (memory that scales with request volume rather than caller count) or a Lua script
to stay atomic. ``INCR`` plus ``EXPIRE`` is two commands in one pipeline and the counter
disposes of itself.

Redis, not in-process state, because the API runs multiple workers and multiple
replicas; a per-process counter would multiply the real limit by the replica count and
would reset on every deploy.

**It fails open.** If Redis is unreachable the request is allowed. This is the same
judgement the outbox makes elsewhere in this codebase -- Redis being down degrades the
system, it does not break it. Failing closed would turn a Redis blip into a total
ingestion outage, which is a far larger incident than the one the limiter prevents.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

from ledgerloop.observability.logging import get_logger

log = get_logger("ledgerloop.api.ratelimit")

_WINDOW_S = 60


@dataclass(frozen=True, slots=True)
class RateLimitVerdict:
    allowed: bool
    #: Seconds until the current window rolls over. Sent as ``Retry-After`` on a 429 so
    #: a well-behaved client backs off by the right amount instead of guessing.
    retry_after_s: int
    limit: int
    remaining: int


async def check(redis: Redis, bucket: str, limit: int) -> RateLimitVerdict:
    """Count one request against ``bucket``. Never raises."""
    now = int(time.time())
    window_start = now - (now % _WINDOW_S)
    retry_after = window_start + _WINDOW_S - now
    key = f"ledgerloop:ratelimit:{bucket}:{window_start}"

    try:
        pipeline = redis.pipeline()
        pipeline.incr(key)
        # Expiry is set on every hit rather than only on creation. Setting it once
        # requires knowing whether the INCR created the key, and a crash between the
        # two commands would otherwise leave a counter that never expires and silently
        # locks that caller out forever.
        pipeline.expire(key, _WINDOW_S)
        count = int((await pipeline.execute())[0])
    except Exception as exc:  # noqa: BLE001 -- see the module docstring: fail open
        log.warning("ratelimit.unavailable", error=str(exc), bucket=bucket)
        return RateLimitVerdict(allowed=True, retry_after_s=0, limit=limit, remaining=limit)

    return RateLimitVerdict(
        allowed=count <= limit,
        retry_after_s=retry_after,
        limit=limit,
        remaining=max(limit - count, 0),
    )
