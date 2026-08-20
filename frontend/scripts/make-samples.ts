/**
 * Emit the sample CSV pair shipped in `public/samples/`.
 *
 * The two files deliberately disagree on every header name, because that is the
 * situation the mapping screen exists for. A sample pair with identical headers
 * would demonstrate nothing.
 *
 *   npm run samples
 */

import { mkdirSync, writeFileSync } from 'node:fs'
import { generate } from '../src/lib/generate'
import { formatMinor } from '../src/lib/money'
import type { SourceRow } from '../src/lib/csv'

const run = generate(
  {
    count: 1_200,
    dropRate: 0.045,
    duplicateRate: 0.02,
    driftRate: 0.035,
    timeSkewMs: 600,
    currency: 'INR',
    seed: 4711,
    anchorMs: Date.UTC(2026, 7, 19, 17, 45, 0),
  },
)

function csv(headers: string[], rows: readonly SourceRow[], cell: (row: SourceRow) => string[]) {
  return [headers.join(','), ...rows.map((row) => cell(row).join(','))].join('\n') + '\n'
}

const gateway = csv(
  ['Transaction Ref', 'Gross Amount', 'Currency Code', 'Captured At', 'Idempotency Key'],
  run.gateway,
  (row) => [
    row.txnId,
    formatMinor(row.amount).replace(/,/g, ''),
    row.currency,
    new Date(row.occurredAt).toISOString(),
    row.idempotencyKey,
  ],
)

const ledger = csv(
  ['entry_id', 'txn_reference', 'posted_value', 'ccy', 'posted_at'],
  run.ledger,
  (row) => [
    row.idempotencyKey.replace('ldg-', 'ENTRY-'),
    row.txnId,
    formatMinor(row.amount).replace(/,/g, ''),
    row.currency,
    new Date(row.occurredAt).toISOString(),
  ],
)

mkdirSync('public/samples', { recursive: true })
writeFileSync('public/samples/gateway-sample.csv', gateway, 'utf8')
writeFileSync('public/samples/ledger-sample.csv', ledger, 'utf8')

console.log(
  `wrote public/samples/gateway-sample.csv (${run.gateway.length} rows) and ` +
    `public/samples/ledger-sample.csv (${run.ledger.length} rows)`,
)
