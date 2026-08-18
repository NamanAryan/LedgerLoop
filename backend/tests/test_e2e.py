"""End-to-end: real API, real worker, real generator, real containers.

Every other test drives one component with the others held still. This one starts the
whole system the way ``docker compose up`` does -- uvicorn serving on a socket, a
matcher loop, an outbox relay, and a sweeper all running concurrently -- points the
synthetic generator at it for 15 seconds, and then checks the engine's own counts
against the generator's independently-kept ground truth.

That comparison is the project's central correctness claim. The generator reimplements
the layer rules rather than importing them (see scripts/generate_load.py), so a bug in
the matcher cannot hide by being mirrored on both sides of the assertion.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
import uvicorn

from ledgerloop.api.app import create_app
from ledgerloop.config import Settings
from ledgerloop.queue.relay import OutboxRelay
from ledgerloop.queue.streams import StreamClient
from ledgerloop.worker.loop import MatcherWorker
from ledgerloop.worker.sweeper import Sweeper
from scripts.generate_load import Config, GroundTruth, run

RUN_SECONDS = 15.0
RATE = 40.0  # 600 transactions: enough for every category to appear, fast enough to run
SETTLE_TIMEOUT = 60.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def e2e_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            # 5s rather than the production 300s so the sweeper's verdict lands inside
            # the test, but long enough that a ledger batch flushing 200ms behind its
            # gateway side is never mistaken for a break.
            "unmatched_after_s": 5,
            "sweep_interval_s": 1,
            # Healthy consumers must not steal each other's in-flight work.
            "claim_min_idle_ms": 30_000,
            "claim_interval_s": 5.0,
            "stream_block_ms": 100,
        }
    )


@pytest_asyncio.fixture
async def live_stack(e2e_settings: Settings) -> AsyncIterator[str]:
    """API on a real socket plus matcher, relay and sweeper, all running for real."""
    from ledgerloop.db.session import build_engine, build_sessionmaker
    from ledgerloop.queue.streams import build_redis

    port = _free_port()
    app = create_app(e2e_settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    server_task = asyncio.create_task(server.serve())
    # uvicorn exposes readiness as a plain bool, not an awaitable, so polling is the
    # only option here -- it resolves in a few tens of milliseconds.
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.05)

    engine = build_engine(e2e_settings)
    sessions = build_sessionmaker(engine)
    redis = build_redis(e2e_settings)
    stream = StreamClient(redis, e2e_settings)
    await stream.ensure_group()

    stop = asyncio.Event()
    workers = [
        asyncio.create_task(MatcherWorker(sessions, stream, e2e_settings, "e2e-matcher").run(stop)),
        asyncio.create_task(OutboxRelay(sessions, stream, e2e_settings).run_forever(stop)),
        asyncio.create_task(Sweeper(sessions, e2e_settings).run_forever(stop)),
    ]

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)
        server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
        await redis.aclose()
        await engine.dispose()


async def _wait_for_settlement(base_url: str, truth: GroundTruth) -> dict[str, float]:
    """Poll /v1/stats until the engine has produced every expected result.

    Polling to a target beats sleeping a fixed interval: the test passes as soon as the
    work is genuinely done, and a hang produces a diagnosable timeout rather than a
    flaky assertion on a half-drained queue.
    """
    deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT
    last: dict[str, float] = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        while asyncio.get_running_loop().time() < deadline:
            last = (await client.get("/v1/stats", params={"window": "1h"})).json()
            produced = last["matched"] + last["unmatched"] + last["drift"] + last["duplicates"]
            if produced >= truth.expected_results:
                return last
            await asyncio.sleep(0.5)
    return last


@pytest.mark.slow
async def test_generator_ground_truth_matches_the_engine(live_stack: str):
    cfg = Config(
        base_url=live_stack,
        rate=RATE,
        duration=RUN_SECONDS,
        drop_rate=0.08,  # ledger side missing -> unmatched_gateway_only
        duplicate_rate=0.05,  # gateway posted twice -> duplicate
        drift_rate=0.06,  # small amount mismatch -> amount_drift
        time_skew_ms=3000.0,  # past the 2s exact window -> exercises layer 2
        currency="INR",
        ledger_batch=25,
        ledger_flush_ms=150.0,
        concurrency=16,
        seed=20260818,
        verify=False,
        settle_s=0.0,
        json_out=None,
    )

    truth = await run(cfg)
    assert truth.transactions_planned > 100, "generator did not offer a meaningful load"
    assert not truth.http_errors, f"ingestion errors during the run: {truth.http_errors}"

    stats = await _wait_for_settlement(live_stack, truth)

    # The four counts the engine reports, against what the generator knows it injected.
    assert stats["matched"] == truth.matched
    assert stats["drift"] == truth.amount_drift
    assert stats["unmatched"] == truth.unmatched_gateway_only + truth.unmatched_ledger_only
    assert stats["duplicates"] == truth.duplicates

    assert stats["match_rate"] == pytest.approx(truth.expected_match_rate, abs=1e-6)
    assert stats["open_exceptions"] == truth.amount_drift + truth.unmatched_gateway_only + (
        truth.unmatched_ledger_only
    )
    assert stats["latency_ms"]["p99"] is not None


@pytest.mark.slow
async def test_every_category_actually_occurred(live_stack: str):
    """Guards the test above: with all injection rates non-zero, a run that produced no
    duplicates or no drift would still "pass" its equality assertions while proving
    nothing. This fails if the workload degenerated."""
    cfg = Config(
        base_url=live_stack,
        rate=RATE,
        duration=5.0,
        drop_rate=0.15,
        duplicate_rate=0.15,
        drift_rate=0.15,
        time_skew_ms=3000.0,
        currency="INR",
        ledger_batch=25,
        ledger_flush_ms=150.0,
        concurrency=16,
        seed=7,
        verify=False,
        settle_s=0.0,
        json_out=None,
    )
    truth = await run(cfg)
    stats = await _wait_for_settlement(live_stack, truth)

    assert truth.matched > 0 and stats["matched"] > 0
    assert truth.amount_drift > 0 and stats["drift"] > 0
    assert truth.duplicates > 0 and stats["duplicates"] > 0
    assert truth.unmatched_gateway_only > 0 and stats["unmatched"] > 0
    assert truth.matched_time_drift > 0, "time skew never crossed the 2s exact window"
