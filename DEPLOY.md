# Deploying LedgerLoop

Frontend to **Vercel**, backend to **Render**. The two are independent — the web app
calls no API, so you can ship it alone and have a working public demo in five minutes
with nothing running behind it.

## Topology

| Resource | Platform | Type | Plan | What it does |
|---|---|---|---|---|
| `ledgerloop-web` | Vercel | Static SPA | Free | The whole engine, in a Web Worker |
| `ledgerloop-db` | Render | PostgreSQL 15 | Free | The record of truth |
| `ledgerloop-redis` | Render | Key Value | Free | The transport |
| `ledgerloop-api` | Render | Web service | Free | Ingestion + read path |
| `ledgerloop-matcher` | Render | Background worker | **Paid** | Matcher + outbox relay |
| `ledgerloop-sweeper` | Render | **Cron job** | **Paid** | Layer 5, every 5 min |

Everything is declared in `render.yaml` at the repo root, so the Render side is one
Blueprint deploy rather than six hand-configured services.

### Cost, plainly

Render has **no free instance type for background workers or cron jobs.** The API,
Postgres, and Key Value are free; the matcher and the sweeper are not. That is the
whole running cost, and it is unavoidable if you want the real asynchronous
architecture rather than a single process pretending to be one. Check current rates on
Render's pricing page — the matcher is a flat monthly instance, the cron job is billed
for the seconds it actually runs, and a sweep takes well under a second.

**If you only want a free public demo:** deploy the Vercel frontend and stop. It is the
complete five-layer engine with CSV upload, synthetic generation, and the
injected-vs-detected verification panel. It needs no backend, no database, and no
account.

---

## Part 1 — Frontend on Vercel

The app is a static SPA. No environment variables, no API base URL, no CORS. Verified
building clean at `282 kB` (89 kB gzipped), with the Web Worker emitted as its own
chunk.

1. Push the repo to GitHub.
2. Vercel → **Add New → Project** → import the repo.
3. **Set the Root Directory to `frontend`.** This is the only setting that matters and
   the only one that will break the build if you miss it.
4. Leave everything else alone — `frontend/vercel.json` supplies the Vite preset, the
   `dist` output directory, and the SPA rewrite that keeps `/results` from 404ing on a
   hard refresh.
5. **Deploy.**

Local equivalents, if you want to check before pushing:

```bash
cd frontend
npm install
npm run build      # tsc --noEmit && vite build -> dist/
npm run check      # typecheck + engine verification + render smoke test
```

`npm run check` is worth running: it sweeps the generator across five defect profiles
and fails if any classification count disagrees with what was injected, and it pins the
layer boundaries exactly (2.000s matches, 2.001s does not).

---

## Part 2 — Backend on Render

### 2.1 Deploy the blueprint

1. Render Dashboard → **New → Blueprint**.
2. Connect the repo. Render finds `render.yaml` and lists all five resources.
3. It will prompt for one value, because it is the only one marked `sync: false`:

   | Variable | Set it to |
   |---|---|
   | `LEDGERLOOP_CORS_ORIGINS` | Your Vercel URL, e.g. `https://ledgerloop.vercel.app` |

   The shipped web app never calls the API, so this only matters if you point a browser
   client at the service — but the `/docs` "Try it out" button is one of those. No
   trailing slash. Comma-separated for several origins.
4. **Apply.** First build takes a few minutes; the matcher and sweeper wait for the
   database.

### 2.2 What happens on first boot

The API's start command runs `alembic upgrade head` before `uvicorn`, so the schema is
created on the first deploy. Nothing else needs doing.

**This is safe at exactly one API instance and only one.** Alembic takes no lock that
would make N replicas racing `upgrade head` on boot safe. The moment you scale the API
past 1, or move it to a paid plan, switch to Render's pre-deploy hook — add to the
`ledgerloop-api` service in `render.yaml`:

```yaml
    preDeployCommand: alembic upgrade head
```

and drop the `alembic upgrade head &&` from `dockerCommand`. That is the Render
equivalent of the one-shot `migrate` service in `docker-compose.yml` and the
`release_command` in `fly.toml`. It needs a paid instance type, which is why it is not
the default here.

### 2.3 Verify it works

```bash
API=https://ledgerloop-api.onrender.com     # your URL

curl -s "$API/health"
curl -s "$API/ready"        # reports Postgres and Redis individually
```

Then push a matching pair through it. **Use a current timestamp** so the 1-hour stats
window picks it up:

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

sleep 3
curl -s "$API/v1/transactions?limit=5"
curl -s "$API/v1/stats?window=1h"
```

You should see one result with `"status": "matched"` and `"match_layer": "exact"`.

Prove idempotency by reposting the **identical** gateway webhook with the same
`Idempotency-Key`:

```bash
# -> 202 with "duplicate": true, "submissions": 2. Never a 409.
```

Interactive docs are at `$API/docs`; Prometheus exposition at `$API/metrics`.

### 2.4 Verify the cron job

Render → `ledgerloop-sweeper` → **Trigger Run**. Logs should show:

```
sweep.started    max_passes=20 unmatched_after_s=300
sweep.finished   resolved=0 passes=1
```

`resolved=0` is the healthy steady state — it means nothing has been waiting past the
window. A non-zero exit is the failure signal; the job logs `sweep.failed` and exits 1
rather than reporting success on a broken sweep.

---

## Part 3 — Operating it

### Scaling

**The matcher is the lever, and it is measurable.** From `BENCHMARKS.md`: the same
100 tx/s workload gives a match p50 of 724 ms with one matcher and 90 ms with three.
Raise the instance count on `ledgerloop-matcher` — the consumer group coordinates, so
N workers never double-process a message, and the partial unique indexes catch it at
the database if they race. No configuration change.

The outbox relay scales with it for free: `FOR UPDATE SKIP LOCKED` lets N relays drain
one outbox without blocking each other.

### The cron schedule

`*/5 * * * *`, UTC. Detection latency for a break is up to
`schedule interval + unmatched_after_s` — about 10 minutes at the defaults.

**Shorten the schedule, not the window.** The window (`LEDGERLOOP_UNMATCHED_AFTER_S`,
300s) is a correctness knob: it must exceed worst-case end-to-end match latency, or the
sweeper reaches a row before the matcher does and reports a counterparty that *did*
arrive as an unmatched break. The schedule is only a latency knob. This exact failure
is why the 1,000 tx/s row in `BENCHMARKS.md` is marked FAIL.

### What to watch

| Metric | Meaning |
|---|---|
| `ledgerloop_outbox_backlog` | Sustained growth = the relay is falling behind or Redis is refusing writes |
| `ledgerloop_queue_pending` | Delivered-but-unacked; growth = workers dying or stuck mid-message |
| `ledgerloop_queue_depth` | Stream length vs. matcher count — the scaling signal |
| `unmatched_gateway_only` vs `unmatched_ledger_only` | Which side stopped posting, not just that reconciliation is imperfect |

The cron sweeper is ephemeral, so nothing scrapes it. Its run history in the Render
dashboard is the observable, which is why its exit code carries the signal.

---

## Free-tier caveats

Four things that will bite you, in the order you will hit them.

**The API spins down after 15 minutes idle.** First request after that pays a cold
start of roughly a minute. Fine for a demo, wrong for anything real — and note that a
webhook sender would time out and retry, which the idempotency layer handles correctly
but which does mean the first retry does the work.

**Free Postgres expires after 30 days.** Render deletes it. Back up anything you care
about, or move to a paid database before then.

**Free Key Value has no persistence.** A restart loses the stream. This is genuinely
survivable here and it is worth understanding why: every row is still in Postgres, the
worker recreates the consumer group on `NOGROUP`, and the sweeper re-attempts a real
match on each stale row *before* declaring it a break. So a Redis wipe costs latency,
not correctness — the unmatched count stays honest.

`render.yaml` sets `maxmemoryPolicy: noeviction` deliberately. Under `allkeys-lru` a
full instance silently drops stream entries, and the sweeper would then classify those
transactions as unmatched — fabricating breaks for payments that reconcile fine, which
is this project's worst failure mode. Under `noeviction`, `XADD` fails loudly, the
outbox retries, and `ledgerloop_outbox_backlog` climbs where you can see it.

`LEDGERLOOP_STREAM_MAXLEN` is set to `50000` rather than the 1,000,000 default for the
same reason: on a 25 MB instance the default would let Redis hit its memory ceiling
long before `MAXLEN` ever trimmed. Raise it with the plan, not on its own.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Vercel build fails, "no package.json" | Root Directory not set | Set it to `frontend` |
| Hard refresh on `/results` 404s | SPA rewrite missing | `vercel.json` handles it; confirm Root Directory |
| API boot: "must use the asyncpg driver" | A `postgresql+psycopg://` DSN | Use the `fromDatabase` reference; bare `postgresql://` is upgraded automatically |
| API healthy, nothing ever matches | Matcher not running, or relay disabled everywhere | Check `ledgerloop-matcher` is live and has `LEDGERLOOP_ENABLE_RELAY=true` |
| `outbox_backlog` climbing | Redis unreachable or out of memory | Check Key Value status; rows are safe and republish on recovery |
| Everything lands `unmatched_*` | Sweep window shorter than real match latency | Raise `LEDGERLOOP_UNMATCHED_AFTER_S`, or scale the matcher |
| Sweep cron exits 1 | Database unreachable | Check the DSN reference and database status |
| First request hangs ~60s | Free-tier cold start | Expected; move to a paid instance to remove it |

---

## Other targets

`railway.json` and `fly.toml` / `fly.worker.toml` are still in the repo and still
current. Fly is the closest match to this topology — `fly deploy` for the API and
`fly deploy -c fly.worker.toml` for the matcher — and its `release_command` runs
migrations as a proper gated release step without needing a paid tier for it.
