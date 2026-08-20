/**
 * Pushing rows into the backend.
 *
 * The two endpoints have different shapes and this file respects that rather than
 * flattening it:
 *
 * * **Ledger** is a batch sync. Up to 1000 entries per request, because that is the
 *   cap the request model enforces, and because the backend writes a whole batch in
 *   two statements rather than two per entry — at 1000 rows that is the difference
 *   between a 20ms request and a 2s one.
 * * **Gateway** is a webhook. One transaction per request, because a webhook is
 *   inherently one transaction and the endpoint takes an `Idempotency-Key` header that
 *   describes exactly one submission.
 *
 * Which means posting N gateway rows is N requests, and the only responsible way to do
 * that from a browser is a bounded pool. Firing 5,000 unbounded `fetch` calls would
 * exhaust the browser's connection pool, bury the API under a burst it has no reason
 * to absorb, and produce a progress bar that jumps from 0 to 100.
 *
 * Ledger goes first. Both orders are correct — the matcher resolves a pair whenever
 * the second side lands — but loading the cheap side first means the counterparties
 * are already waiting, so matches start resolving during the gateway phase and the
 * dashboard has something true to show while the slow half runs.
 */

import { postGatewayWebhook, postLedgerSync, ApiError } from './client'
import { LEDGER_BATCH_MAX, type GatewayWebhookIn, type LedgerEntryIn } from './types'
import { toDecimalString } from '../lib/money'
import type { SourceRow } from '../lib/csv'

/**
 * Simultaneous in-flight webhook posts.
 *
 * Browsers cap concurrent connections per origin around six for HTTP/1.1 and allow far
 * more over HTTP/2, so this is about not overwhelming the *server*, not the client. A
 * free-tier instance running the matcher on the same event loop as ingestion is the
 * constraint here; twelve keeps it busy without starving the read path that the
 * dashboard is polling at the same time.
 */
export const DEFAULT_CONCURRENCY = 12

export interface IngestProgress {
  phase: 'ledger' | 'gateway'
  /** Rows finished, successfully or not. */
  completed: number
  total: number
  accepted: number
  /** The backend already held this idempotency key. Success, not failure. */
  duplicates: number
  failed: number
}

export interface SideSummary {
  submitted: number
  accepted: number
  duplicates: number
  failed: number
}

export interface IngestSummary {
  gateway: SideSummary
  ledger: SideSummary
  elapsedMs: number
  /** Wall clock at completion. The dashboard counts the sweep window from here. */
  finishedAt: number
  /** Capped: a systemic failure produces the same message thousands of times. */
  errors: string[]
}

const MAX_REPORTED_ERRORS = 10

export interface IngestOptions {
  concurrency?: number
  signal?: AbortSignal
  onProgress?: (progress: IngestProgress) => void
}

/** A row on the wire. The instant becomes ISO 8601; the amount becomes a decimal string. */
function toLedgerEntry(row: SourceRow): LedgerEntryIn {
  return {
    // entry_id is required and is the merchant system's own line reference. When the
    // file carries no such column the idempotency key stands in: it is unique per row
    // by construction, which is the only property entry_id is used for here.
    entry_id: row.idempotencyKey.slice(0, 128),
    txn_id: row.txnId,
    amount: toDecimalString(row.amount),
    currency: row.currency,
    occurred_at: new Date(row.occurredAt).toISOString(),
    idempotency_key: row.idempotencyKey.slice(0, 255),
  }
}

function toGatewayWebhook(row: SourceRow): GatewayWebhookIn {
  return {
    txn_id: row.txnId,
    amount: toDecimalString(row.amount),
    currency: row.currency,
    occurred_at: new Date(row.occurredAt).toISOString(),
    // gateway_ref is the gateway's own handle on the payment. Same reasoning as
    // entry_id: absent a real one, the row's unique key is the honest stand-in.
    gateway_ref: row.idempotencyKey.slice(0, 128),
  }
}

function chunk<T>(items: readonly T[], size: number): T[][] {
  const out: T[][] = []
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size))
  return out
}

/**
 * Post both sides. Resolves with what the backend acknowledged.
 *
 * Note what this does *not* return: any judgement about whether the data reconciled.
 * That answer only exists in the backend, arrives asynchronously, and is read by
 * polling `/v1/stats`. Ingestion finishing means the rows are durable and queued —
 * which is exactly what the endpoints' 202 says.
 */
export async function ingestRows(
  gateway: readonly SourceRow[],
  ledger: readonly SourceRow[],
  options: IngestOptions = {},
): Promise<IngestSummary> {
  const { concurrency = DEFAULT_CONCURRENCY, signal, onProgress } = options
  const startedAt = performance.now()

  const summary: IngestSummary = {
    gateway: { submitted: gateway.length, accepted: 0, duplicates: 0, failed: 0 },
    ledger: { submitted: ledger.length, accepted: 0, duplicates: 0, failed: 0 },
    errors: [],
    // Overwritten on the way out. Present here so the object is a complete
    // IngestSummary from the start rather than a cast that hides a missing field.
    elapsedMs: 0,
    finishedAt: 0,
  }

  const note = (error: unknown) => {
    if (summary.errors.length >= MAX_REPORTED_ERRORS) return
    const message =
      error instanceof ApiError
        ? `${error.status}: ${error.detail}`
        : error instanceof Error
          ? error.message
          : String(error)
    if (!summary.errors.includes(message)) summary.errors.push(message)
  }

  // --- ledger: batched -----------------------------------------------------
  let ledgerDone = 0
  const report = (phase: 'ledger' | 'gateway', completed: number, total: number) => {
    const side = phase === 'ledger' ? summary.ledger : summary.gateway
    onProgress?.({
      phase,
      completed,
      total,
      accepted: side.accepted,
      duplicates: side.duplicates,
      failed: side.failed,
    })
  }

  report('ledger', 0, ledger.length)
  for (const batch of chunk(ledger, LEDGER_BATCH_MAX)) {
    if (signal?.aborted) throw new DOMException('ingest aborted', 'AbortError')
    try {
      const ack = await postLedgerSync(batch.map(toLedgerEntry), signal)
      summary.ledger.accepted += ack.accepted
      summary.ledger.duplicates += ack.duplicates
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error
      // The batch is one transaction on the server: a failure means none of it landed.
      summary.ledger.failed += batch.length
      note(error)
    }
    ledgerDone += batch.length
    report('ledger', ledgerDone, ledger.length)
  }

  // --- gateway: bounded pool -----------------------------------------------
  let gatewayDone = 0
  report('gateway', 0, gateway.length)

  let next = 0
  const runner = async (): Promise<void> => {
    for (;;) {
      const index = next++
      if (index >= gateway.length) return
      if (signal?.aborted) throw new DOMException('ingest aborted', 'AbortError')

      const row = gateway[index] as SourceRow
      try {
        const ack = await postGatewayWebhook(
          toGatewayWebhook(row),
          row.idempotencyKey.slice(0, 255),
          signal,
        )
        // duplicate:true is the idempotency layer working, not a failure. It is
        // counted separately so the dashboard can show it as such.
        if (ack.result.duplicate) summary.gateway.duplicates += 1
        else summary.gateway.accepted += 1
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') throw error
        summary.gateway.failed += 1
        note(error)
      }

      gatewayDone += 1
      // Reporting every row would re-render the progress bar thousands of times for no
      // visible gain. Every 25 is smooth at any pool size, and the final row always
      // reports so the bar cannot stop short of the end.
      if (gatewayDone % 25 === 0 || gatewayDone === gateway.length) {
        report('gateway', gatewayDone, gateway.length)
      }
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(concurrency, Math.max(gateway.length, 1)) }, runner),
  )

  summary.elapsedMs = performance.now() - startedAt
  summary.finishedAt = Date.now()
  return summary
}
