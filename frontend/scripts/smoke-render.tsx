/**
 * Render smoke test.
 *
 * `npm run verify` proves the engine is right; this proves the screens actually
 * mount and that a real reconciliation result survives the trip into the table,
 * the cascade, and the exception pane. It renders to a string in Node, so it
 * catches the class of crash — a bad destructure, an undefined lookup — that a
 * typecheck cannot see and that would otherwise only show up as a blank page.
 *
 *   npm run smoke
 */

import type { ReactNode } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { Landing } from '../src/screens/Landing'
import { ChooseSource } from '../src/screens/ChooseSource'
import { Reconcile } from '../src/screens/Reconcile'
import { Dashboard } from '../src/screens/Dashboard'
import { DEFAULT_GENERATOR_CONFIG, generate, toCsv } from '../src/engine/generate'
import { reconcile } from '../src/engine/reconcile'
import { coerceRows, guessMapping, parseCsv } from '../src/engine/csv'
import { DEFAULT_MATCH_CONFIG } from '../src/engine/types'

let failures = 0
function check(name: string, run: () => string, mustContain: string[]) {
  try {
    const html = run()
    const missing = mustContain.filter((needle) => !html.includes(needle))
    if (missing.length > 0) {
      failures += 1
      console.log(`  FAIL ${name} — missing: ${missing.join(', ')}`)
    } else {
      console.log(`  ok   ${name}`)
    }
  } catch (error) {
    failures += 1
    console.log(`  FAIL ${name} — threw: ${error instanceof Error ? error.message : error}`)
  }
}

const noop = () => {}

/** Screens read the router (links, navigation), so each render needs one. */
const at = (path: string, node: ReactNode) =>
  renderToString(<MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>)

console.log('\nroutes: render')

check('/ landing', () => at('/', <Landing />), [
  'Financial clarity,',
  'effortlessly reconciled',
  'Start reconciling',
  'Multi-layer matching',
])

/* A real round trip: generate -> CSV -> parse -> map -> coerce -> reconcile. */
const run = generate({ ...DEFAULT_GENERATOR_CONFIG, count: 400 }, DEFAULT_MATCH_CONFIG)
const gatewayCsv = parseCsv(toCsv(run.gateway, 'gateway'))
const ledgerCsv = parseCsv(toCsv(run.ledger, 'ledger'))
const gatewayMap = guessMapping(gatewayCsv.headers)
const ledgerMap = guessMapping(ledgerCsv.headers)

const reconcileProps = {
  generator: DEFAULT_GENERATOR_CONFIG,
  onGeneratorChange: noop,
  onRunGenerated: noop,
  gateway: null,
  ledger: null,
  gatewayMap: guessMapping([]),
  ledgerMap: guessMapping([]),
  fallbackCurrency: 'INR',
  fileError: { gateway: null, ledger: null },
  runError: null,
  running: false,
  onFile: noop,
  onMapChange: noop,
  onFallbackCurrency: noop,
  onRunUploaded: noop,
}

check('/reconcile chooser', () => at('/reconcile', <ChooseSource />), [
  'Reconcile',
  'Test data',
  'Upload',
  'Configure a run',
  'Choose files',
])

check(
  '/reconcile/test',
  () => at('/reconcile/test', <Reconcile mode="test" {...reconcileProps} />),
  ['Test data', 'Upload', 'Sources', 'Generate and reconcile', 'Drop rate', 'Time skew', 'Seed'],
)

check(
  '/reconcile/upload, no files',
  () => at('/reconcile/upload', <Reconcile mode="upload" {...reconcileProps} />),
  ['Gateway transactions', 'Ledger entries', 'gateway-sample.csv'],
)

check(
  '/reconcile/upload, both files mapped',
  () =>
    at(
      '/reconcile/upload',
      <Reconcile
        mode="upload"
        {...reconcileProps}
        gateway={{ name: 'gateway.csv', size: 1024, parsed: gatewayCsv }}
        ledger={{ name: 'ledger.csv', size: 900, parsed: ledgerCsv }}
        gatewayMap={gatewayMap}
        ledgerMap={ledgerMap}
      />,
    ),
  ['Gateway columns', 'Ledger columns', 'Fallback currency'],
)

console.log('\ncsv: round trip through the mapping layer')
const coercedGateway = coerceRows(gatewayCsv, {
  side: 'gateway',
  map: gatewayMap,
  fallbackCurrency: 'INR',
  idOffset: 1,
})
const coercedLedger = coerceRows(ledgerCsv, {
  side: 'ledger',
  map: ledgerMap,
  fallbackCurrency: 'INR',
  idOffset: 1_000_001,
})

const viaCsv = reconcile({
  gateway: coercedGateway.rows,
  ledger: coercedLedger.rows,
  config: DEFAULT_MATCH_CONFIG,
  runId: 'CSV',
})
const direct = reconcile({
  gateway: run.gateway,
  ledger: run.ledger,
  config: DEFAULT_MATCH_CONFIG,
  runId: 'DIR',
})

if (coercedGateway.rejectedCount + coercedLedger.rejectedCount > 0) {
  failures += 1
  console.log(
    `  FAIL parser rejected rows it wrote itself: ${coercedGateway.rejectedCount + coercedLedger.rejectedCount}`,
  )
} else {
  console.log('  ok   every generated row survives CSV export and re-import')
}

if (JSON.stringify(viaCsv.stats.totals) !== JSON.stringify(direct.stats.totals)) {
  failures += 1
  console.log(
    `  FAIL CSV path disagrees with the direct path: ${JSON.stringify(viaCsv.stats.totals)} vs ${JSON.stringify(direct.stats.totals)}`,
  )
} else {
  console.log('  ok   CSV path and direct path reach identical verdicts')
}

console.log('\ncsv: the shipped samples auto-map')
{
  const gwHeaders = [
    'Transaction Ref',
    'Gross Amount',
    'Currency Code',
    'Captured At',
    'Idempotency Key',
  ]
  const ldHeaders = ['entry_id', 'txn_reference', 'posted_value', 'ccy', 'posted_at']
  const expected = [
    [
      'gateway sample',
      guessMapping(gwHeaders),
      {
        txn_id: 'Transaction Ref',
        amount: 'Gross Amount',
        currency: 'Currency Code',
        timestamp: 'Captured At',
        idempotency_key: 'Idempotency Key',
      },
    ],
    [
      'ledger sample',
      guessMapping(ldHeaders),
      {
        txn_id: 'txn_reference',
        amount: 'posted_value',
        currency: 'ccy',
        timestamp: 'posted_at',
        idempotency_key: 'entry_id',
      },
    ],
  ] as const

  for (const [name, got, want] of expected) {
    if (JSON.stringify(got) !== JSON.stringify(want)) {
      failures += 1
      console.log(`  FAIL ${name} mapping — got ${JSON.stringify(got)}`)
    } else {
      console.log(`  ok   ${name} maps all five fields from mismatched headers`)
    }
  }
}

console.log('\nroutes: results')
check(
  '/results with a real run',
  () =>
    at(
      '/results',
      <Dashboard
        run={direct}
        truth={run.truth}
        totalMs={12.5}
        source="Synthetic · smoke"
        resolutions={{}}
        onResolutionChange={noop}
      />,
    ),
  [
    'Reconciliation',
    'Matched',
    'Duplicates',
    'Processing',
    'Resolution by layer',
    'Injected vs detected',
    'New run',
    'Transaction ID',
  ],
)

console.log(failures === 0 ? '\nAll render checks passed.\n' : `\n${failures} check(s) failed.\n`)
process.exit(failures === 0 ? 0 : 1)
