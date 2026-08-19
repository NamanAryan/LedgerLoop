# LedgerLoop — web

A standalone payment reconciliation app. React 19 + TypeScript + Vite, no backend: the
five-layer matching engine is a TypeScript port of `backend/ledgerloop/matching/core.py`
and runs in a Web Worker in the browser.

```bash
npm install
npm run dev       # http://localhost:5173
npm run check     # typecheck + engine verification + render smoke test
npm run build     # tsc --noEmit && vite build -> dist/
```

## What it does

Two transaction streams go in — gateway records and ledger entries — and every row comes
out classified, with the reason attached. Five layers run in order, each seeing only what
the one above it could not resolve:

| Layer | Rule | Result |
| --- | --- | --- |
| 1 · exact | same `txn_id`, same currency, same amount, \|Δt\| ≤ 2s | matched |
| 2 · time drift | same amount, \|Δt\| ≤ 60s | matched — the money moved, the clocks disagreed |
| 3 · amount drift | \|Δt\| ≤ 60s, \|Δamount\| ≤ max(1% of gateway, 10 major units) | **exception**, not a match |
| 4 · duplicate | idempotency key already seen on that side | suppressed from active counts |
| 5 · sweep | nothing matched | unmatched, with the specific reason |

Two refusals are deliberate. A pair sharing a `txn_id` across **different currencies**
never matches — pairing 100 USD with 100 INR would be worse than reporting a break. And a
row whose nearest counterparty sits outside the drift window defers rather than guessing.

## Design notes

**Money is never a float.** Amounts are integer minor units end to end. Layer 3's
percentage tolerance is carried as basis points and compared with both sides scaled up, so
no decision path ever touches a binary fraction. `parseMinor` is string-based because
`Math.round(parseFloat(s) * 100)` is silently wrong for inputs like `8.115`.

**Ground truth is computed independently.** The synthetic generator records what it
injected using its own reading of the layer rules rather than calling the matcher. If both
sides shared an implementation, a bug in the matcher would be mirrored in the ground truth
and the two would agree on the wrong answer. The dashboard's *Injected vs detected* panel
is that comparison, on screen.

**Rejected rows are counted, never dropped.** A tool that silently discards eleven
malformed CSV rows has manufactured eleven breaks. Every refusal is surfaced with a reason
and a line number. Ambiguous date formats like `03/04/2026` are rejected rather than
guessed.

**Bucketed matching.** 50,000 pairs compared pairwise is 2.5 billion comparisons. Rows are
bucketed by `txn_id` first, so the quadratic part is confined to a bucket that is almost
always one row deep — roughly 100k rows in ~150ms on a laptop.

## Verification

`npm run verify` sweeps the generator across five defect profiles and fails if any
classification count disagrees with what was injected. It also pins the layer boundaries
directly (2.000s matches exact, 2.001s does not; 60.000s matches, 60.001s does not),
checks that input order cannot change a verdict, and asserts every input row is accounted
for exactly once at 50k scale.

`npm run smoke` renders every screen in Node and round-trips a generated run through CSV
export, parsing, column mapping, and coercion — asserting the CSV path reaches identical
verdicts to the direct path.

## Deploying to Vercel

The app is a static SPA. Set the project's **root directory to `frontend`**; `vercel.json`
supplies the rest (Vite preset, `dist` output, SPA rewrite). No environment variables and
no server.

## Sample data

`public/samples/` holds a matched CSV pair whose two files deliberately disagree on every
header name — that is the situation the column-mapping screen exists for. Regenerate with
`npm run samples`.

## Layout

```
src/
  engine/        the reconciliation core — no React imports anywhere in here
    money.ts       integer minor units, exact tolerance arithmetic
    matching.ts    layers 1-5, pure functions of their arguments
    reconcile.ts   bucketing and orchestration
    generate.ts    synthetic streams + independently derived ground truth
    csv.ts         RFC 4180 parser, header guessing, coercion
    worker.ts      the engine, off the main thread
  components/    table, cascade, exception pane, primitives
  screens/       landing, test-data controls, upload + mapping, dashboard
```

`legacy/` holds the earlier vanilla-JS prototype that talked to the FastAPI backend. It is
superseded by this app and can be deleted.
