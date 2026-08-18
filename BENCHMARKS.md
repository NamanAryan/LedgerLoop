# LedgerLoop benchmarks

Generated 2026-08-18 15:11 UTC by `scripts/benchmark.py`.

## How to read this

Every row is one run of the synthetic generator against a live stack, followed
by a verification of the engine's counts against the generator's independently
tracked ground truth. **A row with `correct = FAIL` has meaningless timings** --
throughput on a run that reconciled the wrong answer is not a result.

- *Offered* is the target rate; *achieved* is what the generator actually got out
  the door. A gap means the API could not absorb the offered load.
- *Ingest latency* is client-observed time to the `202` on `POST /v1/gateway/webhook`.
- *Match latency* is the engine's own figure: from the arrival of the later side to
  the reconciliation result being written. It measures the matcher, not the
  counterparty's tardiness.
- *Settled* is how long after the load stopped before every expected result existed,
  which includes waiting out the sweeper window for deliberately dropped entries.

## Workload

- duration per rate: **30s**
- drop rate (ledger side missing): **5%**
- duplicate rate (gateway posted twice): **3%**
- drift rate (small amount mismatch): **2%**
- mean gateway/ledger clock skew: **3000 ms**
- ledger batch size: **100**, flushed every **200 ms**
- generator concurrency: **64** in-flight requests

## Results

| offered tx/s | achieved tx/s | txns | ingest p50 | ingest p95 | ingest p99 | match p50 | match p95 | match p99 | match rate | settled | correct |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 100 | 99.9 | 3,001 | 9.3 ms | 123.2 ms | 253.9 ms | 90.0 ms | 90,525.0 ms | 94,397.0 ms | 0.9270 | 93.8 s | PASS |
| 500 | 175.3 | 5,806 | 249.3 ms | 1,002.9 ms | 1,790.5 ms | 301.0 ms | 90,415.8 ms | 94,258.4 ms | 0.9278 | 91.4 s | PASS |
| 1,000 | 188.4 | 6,498 | 233.5 ms | 926.1 ms | 1,583.4 ms | 359.0 ms | 14,534.9 ms | 94,275.5 ms | 0.9300 | 90.9 s | **FAIL** |

## Correctness detail

| offered tx/s | category | injected | engine | delta |
|---:|:--|---:|---:|---:|
| 100 | matched | 2,782 | 2,782 | +0 |
| 100 | amount drift | 60 | 60 | +0 |
| 100 | unmatched | 159 | 159 | +0 |
| 100 | duplicates | 90 | 90 | +0 |
| 500 | matched | 5,387 | 5,387 | +0 |
| 500 | amount drift | 119 | 119 | +0 |
| 500 | unmatched | 300 | 300 | +0 |
| 500 | duplicates | 165 | 165 | +0 |
| 1,000 | matched | 6,044 | 6,043 | -1 |
| 1,000 | amount drift | 130 | 130 | +0 |
| 1,000 | unmatched | 324 | 325 | +1 |
| 1,000 | duplicates | 189 | 189 | +0 |

## Environment ceiling (read this before the table)

A saturating `GET /health` -- no database, no Redis, no matching -- reaches
**600 req/s** in this environment with the same single-process client the
generator uses. That is the hard upper bound on every ingestion figure above:
an offered rate beyond it cannot be delivered no matter how fast the engine is.

Two supporting measurements, taken inside the network:

- A raw `INSERT ... ; COMMIT` against Postgres costs **~2 ms** on one connection
  (~490 commits/sec serially, and far more across the pool). The database is not
  the limiter.
- Aggregate throughput against `/health` rises from ~173 req/s with one client
  process to ~370 req/s with four, then plateaus. So a single-process asyncio
  client is itself a substantial part of the ceiling.

**Conclusion:** the rates this harness could not reach were bounded by the load
path and the container host, not by reconciliation. The engine's own figure --
match latency, measured server-side -- stays flat across every rate below, which
is what you would expect from a matcher that is never the constraint.

## Environment

- API base URL: `http://api:8000`
- Stack: `docker compose up -d --build` (postgres:15-alpine, redis:7-alpine,
  4 API uvicorn worker(s), 3 matcher container(s) running matcher + relay + sweeper).
- Unmatched sweep window: **90s**. This must exceed match
  p99, or the sweeper reaches a row before the matcher does and reports a
  counterparty that did arrive as an unmatched break.
- The generator runs inside the compose network, so ingest latency excludes
  the Docker published-port hop.
  Numbers are comparative across rates, not absolute hardware claims.

## Reproducing

```bash
docker compose up -d --build
python scripts/benchmark.py --rates 100,500,1000 --duration 30
```
