# Deploying LedgerLoop — free tier

Frontend to **Vercel**, backend to **Render**, the scheduled sweep to **GitHub
Actions**. Nothing here costs money.

The two halves are independent — the web app calls no API — so you can ship the
frontend alone and have a working public demo in five minutes with nothing running
behind it.

## Topology

| Resource | Platform | Type | Cost | What it does |
|---|---|---|---|---|
| `ledgerloop-web` | Vercel | Static SPA | Free | The whole engine, in a Web Worker |
| `ledgerloop-db` | Render | PostgreSQL 15 | Free | The record of truth |
| `ledgerloop-redis` | Render | Key Value | Free | The transport |
| `ledgerloop-api` | Render | Web service | Free | API **+ matcher + relay**, one process |
| `sweep` | GitHub Actions | Scheduled workflow | Free | Layer 5, every 15 min |

### What "free" costs you

Render has **no free instance type for background workers or cron jobs**. The two
services that would normally host the background loops cannot exist on this plan, so:

- **The matcher and relay move into the API process** (`LEDGERLOOP_EMBED_WORKER=true`).
- **The sweeper moves to GitHub Actions**, which is a real cron on someone else's free
  tier, invoking the same `python -m ledgerloop.worker.sweep` a Render Cron Job would.

This is a deployment concession, not a redesign. The loops are the same objects driven
by the same shutdown protocol; only the process boundary moved. There is no second
implementation to keep in step — that is the only reason it is acceptable.

**What you give up**, written down so nobody has to rediscover it:

- **Backpressure isolation.** Matching now competes with ingestion for one event loop.
  Under sustained load the API's p99 degrades, and a gateway seeing slow responses
  retries transactions that were never lost. This is precisely what
  `worker/main.py` exists to prevent.
- **Independent scaling.** One process, one scaling knob, for two workloads with
  different cost profiles.
- **Deploy coupling.** Restarting the API restarts the matcher.

**What you do not give up: correctness.** Effectively-once still comes from the partial
unique indexes on `reconciliation_results`, not from where the code runs. Idempotency,
the outbox, and the sweeper's match-before-declaring-a-break rule are all unchanged.

Measured on the real thing, embedded, with no worker container: `matched` / `exact` at
**87 ms** match latency, and a retried webhook returning `202 duplicate:true`.

---

## Part 1 — Frontend on Vercel

Static SPA. No environment variables, no API base URL, no CORS. Builds to 282 kB
(89 kB gzipped) with the Web Worker as its own chunk.

1. Push the repo to GitHub.
2. Vercel → **Add New → Project** → import the repo.
3. **Set the Root Directory to `frontend`.** The only setting that matters, and the
   only one that will break the build if you miss it.
4. Leave everything else alone — `frontend/vercel.json` supplies the Vite preset, the
   `dist` output, and the SPA rewrite that keeps `/results` from 404ing on a refresh.
5. **Deploy.**

Check locally first if you like:

```bash
cd frontend
npm install
npm run build      # tsc --noEmit && vite build -> dist/
npm run check      # typecheck + engine verification + render smoke test
```

---

## Part 2 — Backend on Render

1. Render Dashboard → **New → Blueprint**.
2. Connect the repo. Render reads `render.yaml` and lists three free resources.
3. It prompts for one value, the only one marked `sync: false`:

   | Variable | Set it to |
   |---|---|
   | `LEDGERLOOP_CORS_ORIGINS` | Your Vercel URL, e.g. `https://ledgerloop.vercel.app` |

   No trailing slash; comma-separated for several. The web app never calls the API, so
   this only matters if you point a browser client at the service — but the `/docs`
   "Try it out" button is one of those.
4. **Apply.** The API runs `alembic upgrade head` before uvicorn, so the schema creates
   itself on first boot.

**Migrations run from the API container, which is safe at exactly one instance.**
Alembic takes no lock that would make N replicas racing `upgrade head` on boot safe.
The free plan gives you one instance, so this holds for as long as the plan does. See
*Upgrading* below for the fix when it stops holding.

### Verify

```bash
API=https://ledgerloop-api.onrender.com     # your URL

curl -s "$API/health"
curl -s "$API/ready"        # reports Postgres and Redis individually
```

Push a matching pair through. **Use a current timestamp** so the 1-hour stats window
picks it up, and expect the first call to take ~1 minute if the instance is asleep:

```bash
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

curl -s -X POST "$API/v1/gateway/webhook" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -d "{\"txn_id\":\"DEMO-1\",\"amount\":\"2100.00\",\"currency\":\"INR\",
       \"occurred_at\":\"$NOW\",\"gateway_ref\":\"gw-1\"}"

curl -s -X POST "$API/v1/ledger/sync" \
  -H 'Content-Type: application/json' \
  -d "{\"entries\":[{\"entry_id\":\"E-1\",\"txn_id\":\"DEMO-1\",\"amount\":\"2100.00\",
       \"currency\":\"INR\",\"occurred_at\":\"$NOW\",\"idempotency_key\":\"demo-ledger-1\"}]}"

sleep 4
curl -s "$API/v1/transactions?limit=1"     # -> "status":"matched","match_layer":"exact"
curl -s "$API/v1/stats?window=1h"
```

Repost the **identical** gateway webhook with the same `Idempotency-Key` to see
`202` with `"duplicate": true, "submissions": 2` — never a `409`.

The startup log confirms the embedded topology:

```
worker.embedded_in_api  tasks=['embedded-matcher-0', 'embedded-relay']
api.started             embedded_worker=True
```

---

## Part 3 — The sweep cron on GitHub Actions

`.github/workflows/sweep.yml` runs every 15 minutes.

1. Render → `ledgerloop-db` → copy the **External** Database URL. (The internal
   hostname only resolves inside Render's network.)
2. GitHub → repo → **Settings → Secrets and variables → Actions → New repository
   secret**:

   | Name | Value |
   |---|---|
   | `LEDGERLOOP_DATABASE_URL` | The external URL from step 1 |

   A bare `postgresql://` scheme is fine — `config.py` upgrades it to asyncpg.
3. **Actions → sweep → Run workflow** to trigger it once without waiting for a tick.

Healthy output:

```
sweep.started    max_passes=20 unmatched_after_s=900
sweep.finished   resolved=0 passes=1
```

`resolved=0` is the steady state — nothing has been waiting past the window. A failed
sweep exits non-zero and fails the run, which is the health signal; nothing else is
watching this job.

**Minutes budget:** ~2,880 runs/month at roughly a minute each. Free and unlimited for
a **public** repository. A **private** one gets 2,000 minutes/month, so switch the cron
to `*/30 * * * *` or `0 * * * *` there — and raise `LEDGERLOOP_UNMATCHED_AFTER_S` on
both the workflow and the Render service to match.

### Why this is safe while the API is asleep

The sweeper connects straight to Postgres and never touches the API or Redis — it
imports nothing from `queue/`. So a spun-down free instance does not stop breaks from
being detected.

More importantly, the sweeper **attempts a real match on every stale row before
declaring it unmatched**. A pair that both arrived while the API was down is matched
here, not reported as a break. Only genuinely orphaned rows get marked.

That is why `LEDGERLOOP_UNMATCHED_AFTER_S` is **900s** on the free deploy rather than
the 300s production default: the window must exceed worst-case end-to-end match
latency, and on free hosting that includes a cold start plus the gap between ticks.
Keep the value identical in `render.yaml` and the workflow — if the workflow's window
were shorter, the sweep would declare breaks the matcher was still entitled to resolve.

---

## Free-tier caveats

**The API spins down after 15 minutes idle**, and the embedded matcher spins down with
it. First request after that pays a cold start of about a minute. Nothing is lost:
ingestion resumes, the relay drains the outbox, and the matcher works through the
stream backlog. A webhook sender would time out and retry, which the idempotency layer
handles correctly — the first retry simply does the work.

You have 750 free instance-hours/month, and 24/7 is ~730, so a keep-alive ping would
just fit — but it would consume essentially all of them, and any second free service
would then run out. Prefer the cold start.

**Free Postgres expires after 30 days.** Render deletes it. Back up anything you care
about or move to a paid database before then. This is the hard deadline on the whole
free deploy.

**Free Key Value has no persistence.** A restart loses the stream. Survivable, and
worth understanding why: every row is still in Postgres, the worker recreates the
consumer group on `NOGROUP`, and the sweeper re-attempts a real match before declaring
anything a break. A Redis wipe costs latency, not correctness.

`render.yaml` sets `maxmemoryPolicy: noeviction` deliberately. Under `allkeys-lru` a
full instance silently drops stream entries, and the sweeper would then classify those
transactions as unmatched — fabricating breaks for payments that reconcile fine, which
is this project's worst failure mode. Under `noeviction`, `XADD` fails loudly, the
outbox retries, and `ledgerloop_outbox_backlog` climbs where you can see it. Same
reason `LEDGERLOOP_STREAM_MAXLEN` is `50000`, not 1,000,000: on a 25 MB instance the
default would let Redis hit its memory ceiling long before `MAXLEN` ever trimmed.

**GitHub delays scheduled workflows** under load. Treat the cron as "at least every 15
minutes". The sweeper is idempotent and the window absorbs the jitter.

---

## Upgrading off the free tier

Three independent steps, in the order worth doing them.

**1. Split the matcher back out** (restores backpressure isolation). Set
`LEDGERLOOP_EMBED_WORKER=false` on the API, and add to `render.yaml`:

```yaml
  - type: worker
    name: ledgerloop-matcher
    runtime: docker
    region: singapore
    plan: starter
    rootDir: backend
    dockerfilePath: ./Dockerfile
    dockerCommand: python -m ledgerloop.worker.main
    envVars:
      - key: LEDGERLOOP_DATABASE_URL
        fromDatabase: { name: ledgerloop-db, property: connectionString }
      - key: LEDGERLOOP_REDIS_URL
        fromService: { type: keyvalue, name: ledgerloop-redis, property: connectionString }
      - key: LEDGERLOOP_ENABLE_RELAY
        value: "true"
      - key: LEDGERLOOP_ENABLE_SWEEPER
        value: "false"
```

Then set `LEDGERLOOP_ENABLE_RELAY=false` on the API. Scaling the matcher is instance
count — the consumer group coordinates, and the relay scales with it for free because
`FOR UPDATE SKIP LOCKED` lets N relays drain one outbox without blocking.

From `BENCHMARKS.md`: the same 100 tx/s workload gives a match p50 of 724 ms with one
matcher and 90 ms with three.

**2. Move the sweep onto Render Cron** (removes the GitHub dependency and the external
database exposure). Delete `.github/workflows/sweep.yml` and add:

```yaml
  - type: cron
    name: ledgerloop-sweeper
    runtime: docker
    region: singapore
    plan: starter
    rootDir: backend
    dockerfilePath: ./Dockerfile
    schedule: "*/5 * * * *"
    dockerCommand: python -m ledgerloop.worker.sweep
    envVars:
      - key: LEDGERLOOP_DATABASE_URL
        fromDatabase: { name: ledgerloop-db, property: connectionString }
      - key: LEDGERLOOP_UNMATCHED_AFTER_S
        value: "300"
```

With an always-on matcher you can drop `LEDGERLOOP_UNMATCHED_AFTER_S` back to `300` —
on both the cron and the API. Shorten the schedule before shortening the window: the
window is a correctness knob, the schedule is only a latency knob.

**3. Fix migrations** once the API runs more than one instance. Add
`preDeployCommand: alembic upgrade head` to the API service and drop the
`alembic upgrade head &&` from its `dockerCommand`. That is the Render equivalent of
the one-shot `migrate` service in `docker-compose.yml` and `fly.toml`'s
`release_command`. It needs a paid instance type, which is why it is not the default.

---

## What to watch

| Metric | Meaning |
|---|---|
| `ledgerloop_outbox_backlog` | Sustained growth = the relay is behind or Redis is refusing writes |
| `ledgerloop_queue_pending` | Delivered-but-unacked; growth = the matcher is dying or stuck |
| `ledgerloop_queue_depth` | Stream length — the scaling signal |
| `unmatched_gateway_only` vs `unmatched_ledger_only` | Which side stopped posting, not just that reconciliation is imperfect |

Prometheus exposition at `$API/metrics`; interactive docs at `$API/docs`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Vercel build fails, "no package.json" | Root Directory not set | Set it to `frontend` |
| Hard refresh on `/results` 404s | SPA rewrite missing | `vercel.json` handles it; confirm Root Directory |
| API boot: "must use the asyncpg driver" | A `postgresql+psycopg://` DSN | Use the `fromDatabase` reference; bare `postgresql://` is upgraded automatically |
| Nothing ever matches | `EMBED_WORKER` not set | Startup log must show `embedded_worker=True` |
| `outbox_backlog` climbing | Redis unreachable or full | Check Key Value status; rows are safe and republish on recovery |
| Everything lands `unmatched_*` | Sweep window shorter than real match latency | Raise `LEDGERLOOP_UNMATCHED_AFTER_S` on **both** the API and the workflow |
| Sweep workflow fails | Wrong DSN, or the internal URL | Use Render's **External** Database URL |
| Sweep workflow stopped running | GitHub disables schedules after 60 days of repo inactivity | Push a commit, or re-enable it in the Actions tab |
| First request hangs ~60s | Free-tier cold start | Expected |

---

## Other targets

`railway.json` and `fly.toml` / `fly.worker.toml` are still current. Fly is the closest
match to the full three-process topology — `fly deploy` for the API and
`fly deploy -c fly.worker.toml` for the matcher — and its `release_command` runs
migrations as a properly gated release step.
