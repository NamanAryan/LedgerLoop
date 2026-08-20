# LedgerLoop — web

The dashboard and ingestion client for the LedgerLoop reconciliation engine. React 19 +
TypeScript + Vite.

**It computes nothing about matching.** Both data paths push rows into the FastAPI
backend, and every number on screen is read back from it. The five-layer engine lives in
`backend/ledgerloop/matching/core.py` and nowhere else — there is exactly one
implementation of the rules, and it is the one running against Postgres.

```bash
npm install
cp .env.example .env.local     # point VITE_API_BASE_URL at your backend
npm run dev                    # http://localhost:5173
npm run check                  # typecheck
npm run build                  # tsc --noEmit && vite build -> dist/
```

The backend has to be running. From the repo root: `docker compose up -d --build`.

## What it does

Two transaction streams go in — gateway records and ledger entries — and the backend
classifies every row. Five layers run in order, each seeing only what the one above it
could not resolve:

| Layer | Rule | Result |
| --- | --- | --- |
| 1 · exact | same `txn_id`, same currency, same amount, \|Δt\| ≤ 2s | matched |
| 2 · time drift | same amount, \|Δt\| ≤ 60s | matched — the money moved, the clocks disagreed |
| 3 · amount drift | \|Δt\| ≤ 60s, \|Δamount\| ≤ max(1% of gateway, 10 major units) | **exception**, not a match |
| 4 · duplicate | idempotency key already seen on that side | suppressed from active counts |
| 5 · sweep | nothing matched after the unmatched window | unmatched, with the reason |

## The two ways in

**Upload.** Two CSVs, parsed with PapaParse, with a column-mapping step because two
exports never agree on header names. Ledger rows go to `POST /v1/ledger/sync` in batches
of 1000 — the endpoint's own cap. Gateway rows go to `POST /v1/gateway/webhook`, one per
request, because a webhook is one transaction by definition; they run through a bounded
pool of 12 with a progress bar rather than thousands of unbounded `fetch` calls.

**Synthetic.** The generator builds gateway/ledger pairs applying the drop, duplicate,
drift and skew rates you set, then posts them to those same two endpoints. It is the
upload path with a different source of rows.

## Design notes

**Ingestion is not reconciliation, and the UI never conflates them.** The endpoints
answer `202`: the row is durable and queued, not matched. So the progress overlay ends
when the last row is *accepted*, and the dashboard then shows the counts converging as
the backend works. Unmatched is high immediately after an upload and falls as
counterparties arrive — that is correct behaviour, and the banner says so rather than
letting it read as a broken engine.

**Ground truth is still computed independently — and now it means more.** The generator
records what it injected from the layer rules directly, never by asking the engine. It
used to check a matcher in the same bundle; it now checks the real backend, across a
network, against Postgres. The *Injected vs detected* panel is that comparison on screen.

**Money is never a float.** Amounts are parsed from CSV into integer minor units and
serialised to the decimal strings the API takes. `parseMinor` is string-based because
`Math.round(parseFloat(s) * 100)` is silently wrong for inputs like `8.115`. A JSON
number would be parsed to a float server-side, reintroducing the error `numeric(18,2)`
exists to prevent.

**Rejected rows are counted, never dropped.** A tool that silently discards eleven
malformed CSV rows has manufactured eleven breaks. Every refusal is surfaced with a
reason and a line number, and the checks mirror the API's own request models — so a row
that survives parsing is one the backend will not 422 mid-upload. Ambiguous dates like
`03/04/2026` are rejected rather than guessed.

**Idempotency keys are derived from row content**, not from a batch nonce, when the file
carries no key column. Re-uploading the same file is then recognised as a repeat rather
than counted twice. The synthetic generator prefixes its run id instead, because two
runs are genuinely different transactions that happen to look alike.

**Pagination is the server's.** The feed uses the opaque cursor from
`GET /v1/transactions` and appends. The cursor is never parsed or incremented here — the
moment a client does arithmetic on a cursor, the server can no longer change what one
means.

## Configuration

| Variable | Purpose |
| --- | --- |
| `VITE_API_BASE_URL` | Backend origin. Baked in at build time, so changing it needs a rebuild. |

The backend must list this app's origin in `LEDGERLOOP_CORS_ORIGINS` or every request
fails preflight. It ships allowing `http://localhost:5173`.

## Deploying

Render static site, declared in the repo-root `render.yaml` alongside the API. See
`DEPLOY.md`. Vercel works identically — set the root directory to `frontend` and supply
`VITE_API_BASE_URL`.

## Layout

```
src/
  api/           the backend, and nothing else
    types.ts       the wire contract, transcribed from schemas.py
    client.ts      fetch layer — retries 5xx, never 4xx; duplicate:true is success
    ingest.ts      batching, the bounded pool, progress
  lib/
    money.ts       minor units in, decimal strings out, display
    csv.ts         PapaParse + column mapping + coercion, counting every refusal
    generate.ts    synthetic streams + independently derived ground truth
  components/    table, cascade, exception pane, primitives
  screens/       landing, test-data controls, upload + mapping, dashboard
```

## Sample data

`public/samples/` holds a matched CSV pair whose two files deliberately disagree on every
header name — that is the situation the column-mapping screen exists for. Regenerate with
`npm run samples`.
