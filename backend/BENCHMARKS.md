# LedgerLoop benchmarks

Generated 2026-08-18 09:46 UTC by `scripts/benchmark.py`.

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

- duration per rate: **10s**
- drop rate (ledger side missing): **5%**
- duplicate rate (gateway posted twice): **3%**
- drift rate (small amount mismatch): **2%**
- mean gateway/ledger clock skew: **3000 ms**
- ledger batch size: **100**, flushed every **200 ms**
- generator concurrency: **64** in-flight requests

## Results

| offered tx/s | achieved tx/s | txns | ingest p50 | ingest p95 | ingest p99 | match p50 | match p95 | match p99 | match rate | settled | correct |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|
| 200 | 56.8 | 1,084 | 658.4 ms | 3,484.6 ms | 5,280.4 ms | 6,851.5 ms | 15,153.7 ms | 15,321.7 ms | 0.9816 | 90.1 s | **FAIL** |

## Correctness detail

| offered tx/s | category | injected | engine | delta |
|---:|:--|---:|---:|---:|
| 200 | matched | 1,015 | 1,015 | +0 |
| 200 | amount drift | 19 | 19 | +0 |
| 200 | unmatched | 50 | 0 | -50 |
| 200 | duplicates | 37 | 37 | +0 |

## Environment

- API base URL: `http://api:8000`
- Stack: `docker compose up -d --build` (postgres:15-alpine, redis:7-alpine,
  one API container, one worker container running matcher + relay + sweeper).
- The generator runs on the host, so ingest latency includes the Docker port
  mapping hop. Numbers are comparative across rates, not absolute hardware claims.

## Reproducing

```bash
docker compose up -d --build
python scripts/benchmark.py --rates 200 --duration 10
```
