#!/usr/bin/env python
"""Synthetic transaction generator with local ground truth.

This is how the engine's correctness is *proved* rather than asserted. It fabricates a
payment stream with known defects injected at known rates, tracks what it did in
memory, and then reports what it injected. Comparing that report against the engine's
own counts is the whole test: if the engine says 41 unmatched and the generator says it
dropped 40, something is wrong, and the difference is the bug.

Two design points worth defending:

* **Pacing is decoupled from HTTP.** A planner emits transactions on a fixed schedule
  into queues; separate sender tasks drain them. If the API slows down, the queues grow
  and that shows up in the achieved-rate report -- rather than the generator silently
  self-throttling and reporting a rate it never actually offered.
* **Injected drift is always inside the engine's tolerance band** (0.2%-0.8%, against a
  1% threshold). The point is to verify that layer 3 classifies drift correctly, not to
  probe the boundary -- boundaries are covered exhaustively by the unit tests, where a
  failure names the exact case instead of showing up as a count that is off by three.

Usage:
    python scripts/generate_load.py --rate 500 --duration 30 \\
        --drop-rate 0.05 --duplicate-rate 0.03 --drift-rate 0.02 --time-skew-ms 400 --verify
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

TWOPLACES = Decimal("0.01")

# Mirrors the engine's defaults. The generator predicts what the engine will decide, so
# these have to agree; a mismatch here would produce a false correctness failure.
EXACT_WINDOW_S = 2.0
DRIFT_WINDOW_S = 60.0
AMOUNT_DRIFT_PCT = Decimal("0.01")
AMOUNT_DRIFT_ABS = Decimal("10.00")


@dataclass
class GroundTruth:
    """What the generator believes it injected, in the engine's own vocabulary."""

    matched: int = 0
    matched_exact: int = 0
    matched_time_drift: int = 0
    amount_drift: int = 0
    unmatched_gateway_only: int = 0
    unmatched_ledger_only: int = 0
    duplicates: int = 0

    transactions_planned: int = 0
    gateway_requests_sent: int = 0
    ledger_entries_sent: int = 0
    ledger_batches_sent: int = 0
    http_errors: Counter[str] = field(default_factory=Counter)
    gateway_latencies_ms: list[float] = field(default_factory=list)
    ledger_latencies_ms: list[float] = field(default_factory=list)

    @property
    def expected_results(self) -> int:
        """Reconciliation rows the engine should end up with."""
        return (
            self.matched
            + self.amount_drift
            + self.unmatched_gateway_only
            + self.unmatched_ledger_only
            + self.duplicates
        )

    @property
    def expected_active(self) -> int:
        """Denominator of the engine's match rate: duplicates excluded."""
        return (
            self.matched
            + self.amount_drift
            + self.unmatched_gateway_only
            + self.unmatched_ledger_only
        )

    @property
    def expected_match_rate(self) -> float:
        return self.matched / self.expected_active if self.expected_active else 0.0


@dataclass(frozen=True)
class Config:
    base_url: str
    rate: float
    duration: float
    drop_rate: float
    duplicate_rate: float
    drift_rate: float
    time_skew_ms: float
    currency: str
    ledger_batch: int
    ledger_flush_ms: float
    concurrency: int
    seed: int | None
    verify: bool
    settle_s: float
    json_out: str | None


@dataclass(frozen=True)
class PlannedTxn:
    txn_id: str
    amount: Decimal
    ledger_amount: Decimal
    currency: str
    occurred_at: datetime
    ledger_occurred_at: datetime
    idempotency_key: str
    entry_id: str
    duplicate: bool
    dropped: bool


def _money(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def plan_transaction(cfg: Config, rng: random.Random, seq: int, run_id: str) -> PlannedTxn:
    txn_id = f"TXN-{run_id}-{seq:08d}"
    amount = _money(Decimal(rng.uniform(50, 50_000)))
    occurred_at = datetime.now(UTC)

    dropped = rng.random() < cfg.drop_rate
    duplicate = rng.random() < cfg.duplicate_rate
    drifted = rng.random() < cfg.drift_rate

    if drifted:
        # 0.2%-0.8% of the gateway amount: comfortably inside the 1% band, so the
        # expected classification is unambiguous. Sign varies so both a short and a
        # long ledger are exercised.
        fraction = Decimal(str(rng.uniform(0.002, 0.008))) * (1 if rng.random() < 0.5 else -1)
        ledger_amount = _money(amount + amount * fraction)
        if ledger_amount == amount:  # tiny amounts can round the drift away
            ledger_amount = _money(amount + Decimal("0.01"))
    else:
        ledger_amount = amount

    # Skew is drawn around the requested mean so a run exercises a spread of clock
    # offsets rather than one constant value.
    skew_ms = rng.gauss(cfg.time_skew_ms, max(cfg.time_skew_ms * 0.3, 1.0))
    ledger_occurred_at = occurred_at + timedelta(milliseconds=skew_ms)

    return PlannedTxn(
        txn_id=txn_id,
        amount=amount,
        ledger_amount=ledger_amount,
        currency=cfg.currency,
        occurred_at=occurred_at,
        ledger_occurred_at=ledger_occurred_at,
        idempotency_key=f"gw-{txn_id}",
        entry_id=f"LEDG-{run_id}-{seq:08d}",
        duplicate=duplicate,
        dropped=dropped,
    )


def classify(txn: PlannedTxn) -> str:
    """Predict the engine's verdict for this transaction.

    Deliberately reimplements the layer rules from the spec rather than importing
    ledgerloop.matching. If both sides shared an implementation, a bug in the matcher
    would be mirrored in the ground truth and the comparison would agree on the wrong
    answer -- which is the one failure mode this whole script exists to catch.
    """
    if txn.dropped:
        return "unmatched_gateway_only"

    dt = abs((txn.ledger_occurred_at - txn.occurred_at).total_seconds())
    if dt > DRIFT_WINDOW_S:
        return "unmatched_both"

    if txn.ledger_amount != txn.amount:
        tolerance = max(abs(txn.amount) * AMOUNT_DRIFT_PCT, AMOUNT_DRIFT_ABS)
        if abs(txn.amount - txn.ledger_amount) <= tolerance:
            return "amount_drift"
        return "unmatched_both"

    return "matched_exact" if dt <= EXACT_WINDOW_S else "matched_time_drift"


def record(truth: GroundTruth, txn: PlannedTxn) -> None:
    verdict = classify(txn)
    if verdict == "matched_exact":
        truth.matched += 1
        truth.matched_exact += 1
    elif verdict == "matched_time_drift":
        truth.matched += 1
        truth.matched_time_drift += 1
    elif verdict == "amount_drift":
        truth.amount_drift += 1
    elif verdict == "unmatched_gateway_only":
        truth.unmatched_gateway_only += 1
    elif verdict == "unmatched_both":
        truth.unmatched_gateway_only += 1
        truth.unmatched_ledger_only += 1

    if txn.duplicate:
        # The retry produces a `duplicate` result of its own; the original still
        # reaches whatever verdict it was going to reach.
        truth.duplicates += 1


def gateway_body(txn: PlannedTxn) -> dict[str, Any]:
    return {
        "txn_id": txn.txn_id,
        "amount": str(txn.amount),
        "currency": txn.currency,
        "occurred_at": txn.occurred_at.isoformat(),
        "gateway_ref": f"REF-{txn.txn_id}",
    }


def ledger_body(txn: PlannedTxn) -> dict[str, Any]:
    return {
        "entry_id": txn.entry_id,
        "txn_id": txn.txn_id,
        "amount": str(txn.ledger_amount),
        "currency": txn.currency,
        "occurred_at": txn.ledger_occurred_at.isoformat(),
        "idempotency_key": f"ld-{txn.txn_id}",
    }


async def gateway_sender(
    client: httpx.AsyncClient, queue: asyncio.Queue[PlannedTxn | None], truth: GroundTruth
) -> None:
    while True:
        txn = await queue.get()
        if txn is None:
            queue.task_done()
            return
        # A duplicate is the same key posted twice -- exactly what a gateway does when
        # it does not receive our 202 and retries.
        attempts = 2 if txn.duplicate else 1
        for _ in range(attempts):
            started = time.perf_counter()
            try:
                response = await client.post(
                    "/v1/gateway/webhook",
                    json=gateway_body(txn),
                    headers={"Idempotency-Key": txn.idempotency_key},
                )
                truth.gateway_latencies_ms.append((time.perf_counter() - started) * 1000)
                truth.gateway_requests_sent += 1
                if response.status_code != 202:
                    truth.http_errors[f"gateway:{response.status_code}"] += 1
            except Exception as exc:  # noqa: BLE001
                truth.http_errors[f"gateway:{type(exc).__name__}"] += 1
        queue.task_done()


async def ledger_sender(
    client: httpx.AsyncClient,
    queue: asyncio.Queue[PlannedTxn | None],
    truth: GroundTruth,
    cfg: Config,
) -> None:
    """Batch ledger entries the way a real merchant sync would: periodic flushes of up
    to N entries, not one request per line."""
    batch: list[PlannedTxn] = []
    deadline = time.monotonic() + cfg.ledger_flush_ms / 1000

    async def flush() -> None:
        nonlocal batch, deadline
        if not batch:
            return
        payload = {"entries": [ledger_body(txn) for txn in batch]}
        started = time.perf_counter()
        try:
            response = await client.post("/v1/ledger/sync", json=payload)
            truth.ledger_latencies_ms.append((time.perf_counter() - started) * 1000)
            truth.ledger_entries_sent += len(batch)
            truth.ledger_batches_sent += 1
            if response.status_code != 202:
                truth.http_errors[f"ledger:{response.status_code}"] += 1
        except Exception as exc:  # noqa: BLE001
            truth.http_errors[f"ledger:{type(exc).__name__}"] += 1
        batch = []
        deadline = time.monotonic() + cfg.ledger_flush_ms / 1000

    while True:
        timeout = max(deadline - time.monotonic(), 0.001)
        try:
            txn = await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            await flush()
            continue

        if txn is None:
            queue.task_done()
            await flush()
            return

        batch.append(txn)
        queue.task_done()
        if len(batch) >= cfg.ledger_batch or time.monotonic() >= deadline:
            await flush()


async def planner(
    cfg: Config,
    truth: GroundTruth,
    gateway_queue: asyncio.Queue[PlannedTxn | None],
    ledger_queue: asyncio.Queue[PlannedTxn | None],
) -> None:
    """Emit transactions on a fixed wall-clock schedule.

    Scheduling against absolute deadlines rather than sleeping for a fixed interval
    keeps the run from drifting: any slow iteration is absorbed by the next sleep
    instead of pushing every subsequent transaction later.
    """
    rng = random.Random(cfg.seed)
    run_id = uuid.uuid4().hex[:8]
    interval = 1.0 / cfg.rate
    start = time.monotonic()
    seq = 0

    while True:
        elapsed = time.monotonic() - start
        if elapsed >= cfg.duration:
            break
        target = start + seq * interval
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        txn = plan_transaction(cfg, rng, seq, run_id)
        record(truth, txn)
        truth.transactions_planned += 1
        seq += 1

        await gateway_queue.put(txn)
        if not txn.dropped:
            await ledger_queue.put(txn)


async def fetch_engine_counts(base_url: str, window: str = "24h") -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
            response = await client.get("/v1/stats", params={"window": window})
            response.raise_for_status()
            return dict(response.json())
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not read /v1/stats: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


async def run(cfg: Config) -> GroundTruth:
    truth = GroundTruth()
    gateway_queue: asyncio.Queue[PlannedTxn | None] = asyncio.Queue(maxsize=cfg.concurrency * 8)
    ledger_queue: asyncio.Queue[PlannedTxn | None] = asyncio.Queue(maxsize=cfg.concurrency * 8)

    limits = httpx.Limits(
        max_connections=cfg.concurrency, max_keepalive_connections=cfg.concurrency
    )
    async with httpx.AsyncClient(
        base_url=cfg.base_url, timeout=httpx.Timeout(30.0), limits=limits
    ) as client:
        senders = [
            asyncio.create_task(gateway_sender(client, gateway_queue, truth))
            for _ in range(cfg.concurrency)
        ]
        flushers = [
            asyncio.create_task(ledger_sender(client, ledger_queue, truth, cfg))
            for _ in range(max(cfg.concurrency // 4, 1))
        ]

        started = time.monotonic()
        await planner(cfg, truth, gateway_queue, ledger_queue)

        for _ in senders:
            await gateway_queue.put(None)
        for _ in flushers:
            await ledger_queue.put(None)
        await asyncio.gather(*senders, *flushers)
        wall = time.monotonic() - started

    truth_wall = wall
    print_summary(cfg, truth, truth_wall)
    return truth


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(round((pct / 100) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def print_summary(cfg: Config, truth: GroundTruth, wall_s: float) -> None:
    achieved = truth.transactions_planned / wall_s if wall_s else 0.0
    print()
    print("=" * 72)
    print("LOAD RUN SUMMARY")
    print("=" * 72)
    print(f"  target rate        : {cfg.rate:,.0f} tx/sec for {cfg.duration:g}s")
    print(f"  achieved rate      : {achieved:,.1f} tx/sec over {wall_s:.2f}s wall")
    print(f"  transactions       : {truth.transactions_planned:,}")
    print(f"  gateway requests   : {truth.gateway_requests_sent:,}")
    print(
        f"  ledger entries     : {truth.ledger_entries_sent:,} "
        f"in {truth.ledger_batches_sent:,} batches"
    )
    print()
    print("  INJECTED (ground truth)")
    print(f"    matched                  : {truth.matched:,}")
    print(f"      exact                  : {truth.matched_exact:,}")
    print(f"      time drift             : {truth.matched_time_drift:,}")
    print(f"    amount drift             : {truth.amount_drift:,}")
    print(f"    unmatched gateway only   : {truth.unmatched_gateway_only:,}")
    print(f"    unmatched ledger only    : {truth.unmatched_ledger_only:,}")
    print(f"    duplicates               : {truth.duplicates:,}")
    print(f"    -> expected results      : {truth.expected_results:,}")
    print(f"    -> expected match rate   : {truth.expected_match_rate:.4f}")
    print()
    for label, samples in (
        ("gateway POST", truth.gateway_latencies_ms),
        ("ledger  POST", truth.ledger_latencies_ms),
    ):
        if samples:
            print(
                f"  {label} latency ms  : "
                f"p50={percentile(samples, 50):.1f} "
                f"p95={percentile(samples, 95):.1f} "
                f"p99={percentile(samples, 99):.1f} "
                f"mean={statistics.fmean(samples):.1f}"
            )
    if truth.http_errors:
        print()
        print("  HTTP ERRORS")
        for key, count in sorted(truth.http_errors.items()):
            print(f"    {key}: {count}")
    print("=" * 72)


def print_verification(truth: GroundTruth, stats: dict[str, Any]) -> bool:
    """Compare ground truth against the engine. Returns True when every count agrees."""
    expected_unmatched = truth.unmatched_gateway_only + truth.unmatched_ledger_only
    rows = [
        ("matched", truth.matched, stats.get("matched")),
        ("amount drift", truth.amount_drift, stats.get("drift")),
        ("unmatched", expected_unmatched, stats.get("unmatched")),
        ("duplicates", truth.duplicates, stats.get("duplicates")),
    ]
    print()
    print("=" * 72)
    print("VERIFICATION  (generator ground truth vs engine /v1/stats)")
    print("=" * 72)
    print(f"  {'category':<24}{'injected':>12}{'engine':>12}{'delta':>10}   ")
    ok = True
    for label, expected, actual in rows:
        actual_int = int(actual) if actual is not None else 0
        delta = actual_int - expected
        if delta != 0:
            ok = False
        verdict = "OK" if not delta else "MISMATCH"
        print(f"  {label:<24}{expected:>12,}{actual_int:>12,}{delta:>+10,}   {verdict}")
    engine_rate = float(stats.get("match_rate", 0.0))
    print(f"  {'match rate':<24}{truth.expected_match_rate:>12.4f}{engine_rate:>12.4f}")
    latency = stats.get("latency_ms") or {}
    print(
        f"  engine match latency ms : p50={latency.get('p50')} "
        f"p95={latency.get('p95')} p99={latency.get('p99')}"
    )
    print("=" * 72)
    print("RESULT:", "ALL COUNTS MATCH" if ok else "MISMATCH -- investigate")
    return ok


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        description="Emit a synthetic reconciliation workload with known injected defects.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--rate", type=float, default=100.0, help="target transactions/second")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds to run")
    parser.add_argument(
        "--drop-rate", type=float, default=0.0, help="fraction where the ledger side is dropped"
    )
    parser.add_argument(
        "--duplicate-rate", type=float, default=0.0, help="fraction where the gateway is sent twice"
    )
    parser.add_argument(
        "--drift-rate", type=float, default=0.0, help="fraction with a small amount mismatch"
    )
    parser.add_argument(
        "--time-skew-ms", type=float, default=0.0, help="mean gateway/ledger clock skew"
    )
    parser.add_argument("--currency", default="INR")
    parser.add_argument("--ledger-batch", type=int, default=50, help="max entries per ledger POST")
    parser.add_argument("--ledger-flush-ms", type=float, default=200.0)
    parser.add_argument("--concurrency", type=int, default=32, help="in-flight HTTP requests")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for a reproducible run")
    parser.add_argument(
        "--verify", action="store_true", help="after settling, compare against /v1/stats"
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=10.0,
        help="seconds to wait before verifying, so the matcher can drain",
    )
    parser.add_argument("--json-out", default=None, help="write ground truth to this JSON file")
    args = parser.parse_args(argv)

    for name in ("drop_rate", "duplicate_rate", "drift_rate"):
        value = getattr(args, name)
        if not 0.0 <= value <= 1.0:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    if args.rate <= 0:
        parser.error("--rate must be positive")

    return Config(
        base_url=args.base_url,
        rate=args.rate,
        duration=args.duration,
        drop_rate=args.drop_rate,
        duplicate_rate=args.duplicate_rate,
        drift_rate=args.drift_rate,
        time_skew_ms=args.time_skew_ms,
        currency=args.currency,
        ledger_batch=args.ledger_batch,
        ledger_flush_ms=args.ledger_flush_ms,
        concurrency=args.concurrency,
        seed=args.seed,
        verify=args.verify,
        settle_s=args.settle,
        json_out=args.json_out,
    )


def write_ground_truth(path: str, cfg: Config, truth: GroundTruth) -> None:
    """Machine-readable ground truth, consumed by scripts/benchmark.py."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "config": cfg.__dict__,
                "matched": truth.matched,
                "matched_exact": truth.matched_exact,
                "matched_time_drift": truth.matched_time_drift,
                "amount_drift": truth.amount_drift,
                "unmatched_gateway_only": truth.unmatched_gateway_only,
                "unmatched_ledger_only": truth.unmatched_ledger_only,
                "duplicates": truth.duplicates,
                "transactions": truth.transactions_planned,
                "expected_results": truth.expected_results,
                "expected_match_rate": truth.expected_match_rate,
                "gateway_requests_sent": truth.gateway_requests_sent,
                "ledger_entries_sent": truth.ledger_entries_sent,
                "http_errors": dict(truth.http_errors),
            },
            handle,
            indent=2,
        )


async def main_async(cfg: Config) -> int:
    truth = await run(cfg)

    if cfg.json_out:
        # Off the event loop: the benchmark harness reads this file immediately after,
        # and a blocking write here would stall anything still draining.
        await asyncio.to_thread(write_ground_truth, cfg.json_out, cfg, truth)
        print(f"  ground truth written to {cfg.json_out}")

    if not cfg.verify:
        return 0

    # Unmatched rows only reach a terminal state after the sweeper's window elapses, so
    # verifying earlier than that would report breaks the engine has not been asked to
    # declare yet.
    print(f"\n  settling for {cfg.settle_s:g}s so the matcher and sweeper can drain...")
    await asyncio.sleep(cfg.settle_s)
    stats = await fetch_engine_counts(cfg.base_url)
    if stats is None:
        return 2
    return 0 if print_verification(truth, stats) else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
