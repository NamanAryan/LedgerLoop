/**
 * The orchestrator: two streams in, one resolved ledger of decisions out.
 *
 * `matching.ts` decides about one candidate against a handful of counterparties.
 * This module's job is to make sure it only ever sees a handful — 50,000 rows
 * compared pairwise would be 2.5 billion comparisons, so rows are bucketed by
 * txn_id first and the quadratic part is confined to a bucket that is almost always
 * one row deep. The result is linear in practice, with an O(n log n) tail from the
 * final sort.
 */

import { decide, decideDuplicate, decideUnmatched } from './matching'
import type {
  MatchConfig,
  ReconResult,
  ReconRow,
  ReconStats,
  ReconTotals,
  TxnFacts,
} from './types'

function percentile(sorted: readonly number[], q: number): number {
  if (sorted.length === 0) return 0
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1))
  return sorted[index] ?? 0
}

/**
 * Layer 4, applied as a pre-pass over one side.
 *
 * The backend gets this for free from a unique constraint at ingestion. In the
 * browser there is no database, so the equivalent is an explicit first-wins pass:
 * the first row carrying a given idempotency key is retained, and every later one is
 * classified `duplicate` and withheld from layers 1-3.
 */
function splitDuplicates(rows: readonly TxnFacts[]): {
  retained: TxnFacts[]
  duplicates: { row: TxnFacts; submissions: number }[]
} {
  const seen = new Map<string, number>()
  const retained: TxnFacts[] = []
  const duplicates: { row: TxnFacts; submissions: number }[] = []

  for (const row of rows) {
    // An empty idempotency key is not a claim of uniqueness, so it is never treated
    // as a collision — a CSV without that column would otherwise collapse to one row.
    if (row.idempotencyKey === '') {
      retained.push(row)
      continue
    }
    const count = (seen.get(row.idempotencyKey) ?? 0) + 1
    seen.set(row.idempotencyKey, count)
    if (count === 1) retained.push(row)
    else duplicates.push({ row, submissions: count })
  }

  return { retained, duplicates }
}

export interface ReconcileInput {
  gateway: readonly TxnFacts[]
  ledger: readonly TxnFacts[]
  config: MatchConfig
  runId: string
}

export function reconcile({ gateway, ledger, config, runId }: ReconcileInput): ReconResult {
  const startedAt = Date.now()
  const t0 = performance.now()

  const gatewaySplit = splitDuplicates(gateway)
  const ledgerSplit = splitDuplicates(ledger)

  // Bucket by txn_id. Currency is deliberately *not* part of the key: a gateway line
  // and a ledger line that share an id but disagree on currency must land in the same
  // bucket so the engine can refuse them explicitly, rather than never meeting and
  // being reported as two unrelated orphans with no explanation.
  const buckets = new Map<string, { gw: TxnFacts[]; ld: TxnFacts[] }>()
  const bucketFor = (id: string) => {
    let bucket = buckets.get(id)
    if (bucket === undefined) {
      bucket = { gw: [], ld: [] }
      buckets.set(id, bucket)
    }
    return bucket
  }
  for (const row of gatewaySplit.retained) bucketFor(row.txnId).gw.push(row)
  for (const row of ledgerSplit.retained) bucketFor(row.txnId).ld.push(row)

  const rows: ReconRow[] = []
  const totals = {
    matchedExact: 0,
    matchedTimeDrift: 0,
    amountDrift: 0,
    unmatchedGateway: 0,
    unmatchedLedger: 0,
    duplicates: 0,
  }
  const skews: number[] = []
  let nextId = 1

  for (const bucket of buckets.values()) {
    const availableLedger = new Set(bucket.ld)

    for (const gw of bucket.gw) {
      const candidates = [...availableLedger]
      const decision = decide(gw, candidates, config)

      if (decision === null) {
        rows.push({
          id: nextId++,
          txnId: gw.txnId,
          status: 'unmatched_gateway_only',
          layer: 'unmatched_sweep',
          currency: gw.currency,
          gateway: gw,
          ledger: null,
          notes: decideUnmatched(gw, describeOrphan(gw, candidates, config)).notes,
          skewMs: null,
          amountDelta: null,
        })
        totals.unmatchedGateway += 1
        continue
      }

      const partner = candidates.find((row) => row.rowId === decision.ledgerRowId)
      if (partner !== undefined) availableLedger.delete(partner)

      const skew = partner ? partner.occurredAt - gw.occurredAt : null
      if (skew !== null && decision.status === 'matched') skews.push(Math.abs(skew))
      if (decision.layer === 'exact') totals.matchedExact += 1
      else if (decision.layer === 'time_drift') totals.matchedTimeDrift += 1
      else if (decision.layer === 'amount_drift') totals.amountDrift += 1

      rows.push({
        id: nextId++,
        txnId: gw.txnId,
        status: decision.status,
        layer: decision.layer,
        currency: gw.currency,
        gateway: gw,
        ledger: partner ?? null,
        notes: decision.notes,
        skewMs: skew,
        amountDelta: partner ? gw.amount - partner.amount : null,
      })
    }

    // Whatever the gateway side did not claim has no counterparty by definition.
    for (const ld of availableLedger) {
      rows.push({
        id: nextId++,
        txnId: ld.txnId,
        status: 'unmatched_ledger_only',
        layer: 'unmatched_sweep',
        currency: ld.currency,
        gateway: null,
        ledger: ld,
        notes: decideUnmatched(ld, describeOrphan(ld, bucket.gw, config)).notes,
        skewMs: null,
        amountDelta: null,
      })
      totals.unmatchedLedger += 1
    }
  }

  for (const { row, submissions } of [...gatewaySplit.duplicates, ...ledgerSplit.duplicates]) {
    const decision = decideDuplicate(row, submissions)
    rows.push({
      id: nextId++,
      txnId: row.txnId,
      status: decision.status,
      layer: decision.layer,
      currency: row.currency,
      gateway: row.side === 'gateway' ? row : null,
      ledger: row.side === 'ledger' ? row : null,
      notes: decision.notes,
      skewMs: null,
      amountDelta: null,
    })
    totals.duplicates += 1
  }

  // Newest first, the way an ops feed reads. The txn_id tiebreak keeps the order
  // total, so the same input always renders in the same sequence.
  rows.sort((a, b) => {
    const at = Math.max(a.gateway?.occurredAt ?? 0, a.ledger?.occurredAt ?? 0)
    const bt = Math.max(b.gateway?.occurredAt ?? 0, b.ledger?.occurredAt ?? 0)
    if (at !== bt) return bt - at
    return a.txnId < b.txnId ? -1 : a.txnId > b.txnId ? 1 : 0
  })

  skews.sort((a, b) => a - b)
  const elapsedMs = performance.now() - t0

  const frozenTotals: ReconTotals = totals
  const matched = totals.matchedExact + totals.matchedTimeDrift
  const active = matched + totals.amountDrift + totals.unmatchedGateway + totals.unmatchedLedger

  const stats: ReconStats = {
    totals: frozenTotals,
    active,
    matched,
    matchRate: active === 0 ? 0 : matched / active,
    exceptions: totals.amountDrift + totals.unmatchedGateway + totals.unmatchedLedger,
    gatewayRows: gateway.length,
    ledgerRows: ledger.length,
    elapsedMs,
    skewP50Ms: percentile(skews, 0.5),
    skewP99Ms: percentile(skews, 0.99),
  }

  return { rows, stats, config, runId, startedAt }
}

/**
 * Say *why* a row ended up orphaned, given what it was actually offered.
 *
 * "No counterparty" is true but useless to an operator. When a row with the same
 * txn_id existed and was still rejected, the specific reason — wrong currency, out
 * of window, drift past tolerance — is what makes the exception queue actionable
 * rather than a list of shrugs.
 */
function describeOrphan(
  row: TxnFacts,
  candidates: readonly TxnFacts[],
  config: MatchConfig,
): string {
  const opposites = candidates.filter((other) => other.side !== row.side)
  if (opposites.length === 0) {
    return 'no counterparty with this txn_id on the other side'
  }

  const currencyClash = opposites.find((other) => other.currency !== row.currency)
  if (currencyClash) {
    return `counterparty found but currency differs (${row.currency} vs ${currencyClash.currency}); cross-currency pairing is never a match`
  }

  const nearest = opposites.reduce((best, other) =>
    Math.abs(other.occurredAt - row.occurredAt) < Math.abs(best.occurredAt - row.occurredAt)
      ? other
      : best,
  )
  const dt = Math.abs(nearest.occurredAt - row.occurredAt)
  if (dt > config.driftWindowMs) {
    return `nearest counterparty is ${(dt / 1000).toFixed(1)}s away, outside the ${(config.driftWindowMs / 1000).toFixed(0)}s window`
  }
  if (nearest.amount !== row.amount) {
    return 'counterparty in window but the amount difference exceeds layer 3 tolerance'
  }
  return 'counterparty already claimed by another row with this txn_id'
}
