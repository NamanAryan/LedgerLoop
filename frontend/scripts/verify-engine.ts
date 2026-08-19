/**
 * Engine verification, runnable outside the browser.
 *
 * The dashboard shows an injected-vs-detected panel, which is only worth anything if
 * it is checked. This script sweeps a range of generator settings and fails the build
 * if any classification count disagrees with what was injected. It also pins the
 * layer boundaries directly, because "2 seconds" and "60 seconds" are the two numbers
 * the whole engine turns on and an off-by-one at the boundary would be invisible in
 * aggregate counts.
 *
 *   npm run verify
 */

import { compareToTruth, generate, type GeneratorConfig } from '../src/engine/generate'
import { classifyPair } from '../src/engine/matching'
import { parseMinor, formatMinor, withinTolerance } from '../src/engine/money'
import { reconcile } from '../src/engine/reconcile'
import { DEFAULT_MATCH_CONFIG, type TxnFacts } from '../src/engine/types'

let failures = 0

function check(name: string, condition: boolean, detail = '') {
  if (condition) {
    console.log(`  ok   ${name}`)
  } else {
    failures += 1
    console.log(`  FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  }
}

/* --- money ---------------------------------------------------------------- */

console.log('\nmoney: exact minor units')
check('parses a plain decimal', parseMinor('2100.50') === 210050)
check('parses grouped input', parseMinor('1,204.00') === 120400)
check('parses a negative', parseMinor('-3.4') === -340)
check('truncates past scale rather than rounding up', parseMinor('8.119') === 811)
check('rejects text', parseMinor('n/a') === null)
check('rejects empty', parseMinor('  ') === null)
check('round-trips', formatMinor(parseMinor('50000.00') as number) === '50,000.00')
// The float trap this module exists to avoid: 0.1 + 0.2 !== 0.3 in binary.
check(
  'sums without binary drift',
  (parseMinor('0.10') as number) + (parseMinor('0.20') as number) === parseMinor('0.30'),
)

console.log('\nmoney: layer 3 tolerance')
const bps = DEFAULT_MATCH_CONFIG.amountDriftBps
const floor = DEFAULT_MATCH_CONFIG.amountDriftFloor
check('1% of a large ticket is allowed', withinTolerance(1_000_000, 990_000, bps, floor))
check('just past 1% is refused', !withinTolerance(1_000_000, 989_999, bps, floor))
check('flat floor covers small tickets', withinTolerance(5_000, 4_100, bps, floor))
check('past the floor is refused', !withinTolerance(5_000, 3_999, bps, floor))

/* --- layer boundaries ----------------------------------------------------- */

const base = Date.UTC(2026, 7, 19, 12, 0, 0)
const facts = (side: 'gateway' | 'ledger', amount: number, offsetMs: number): TxnFacts => ({
  side,
  rowId: side === 'gateway' ? 1 : 2,
  txnId: 'TXN-1',
  amount,
  currency: 'INR',
  occurredAt: base + offsetMs,
  idempotencyKey: `${side}-1`,
})

console.log('\nlayers: boundaries')
const at = (offsetMs: number, ledgerAmount = 210000) =>
  classifyPair(facts('gateway', 210000, 0), facts('ledger', ledgerAmount, offsetMs), DEFAULT_MATCH_CONFIG)

check('0s is exact', at(0)?.layer === 'exact')
check('exactly 2.000s is still exact', at(2_000)?.layer === 'exact')
check('2.001s falls to time drift', at(2_001)?.layer === 'time_drift')
check('exactly 60.000s is still time drift', at(60_000)?.layer === 'time_drift')
check('60.001s matches nothing', at(60_001) === null)
check('amount drift inside window and tolerance', at(1_000, 209_000)?.layer === 'amount_drift')
check('amount drift is not a match', at(1_000, 209_000)?.status === 'amount_drift')
check('amount drift past tolerance defers', at(1_000, 150_000) === null)

console.log('\nlayers: refusals')
const usd: TxnFacts = { ...facts('ledger', 210000, 0), currency: 'USD' }
check(
  'cross-currency never pairs',
  classifyPair(facts('gateway', 210000, 0), usd, DEFAULT_MATCH_CONFIG) === null,
)
check(
  'a row never pairs with its own side',
  classifyPair(facts('gateway', 210000, 0), facts('gateway', 210000, 0), DEFAULT_MATCH_CONFIG) ===
    null,
)

/* --- determinism ---------------------------------------------------------- */

console.log('\nengine: determinism')
const cfgA: GeneratorConfig = {
  count: 2_000,
  dropRate: 0.05,
  duplicateRate: 0.03,
  driftRate: 0.04,
  timeSkewMs: 500,
  currency: 'INR',
  seed: 12345,
  anchorMs: Date.UTC(2026, 7, 19, 18, 0, 0),
}
const runOne = generate(cfgA, DEFAULT_MATCH_CONFIG)
const runTwo = generate(cfgA, DEFAULT_MATCH_CONFIG)
check(
  'the same seed and anchor produce identical streams',
  JSON.stringify(runOne.gateway) === JSON.stringify(runTwo.gateway) &&
    JSON.stringify(runOne.ledger) === JSON.stringify(runTwo.ledger),
)

const shifted = generate({ ...cfgA, anchorMs: Date.UTC(2027, 0, 1, 9, 30, 0) }, DEFAULT_MATCH_CONFIG)
check(
  'moving the anchor changes timestamps but not verdicts',
  shifted.gateway[0]?.occurredAt !== runOne.gateway[0]?.occurredAt &&
    JSON.stringify(shifted.truth) === JSON.stringify(runOne.truth),
)

const resultOne = reconcile({ ...runOne, config: DEFAULT_MATCH_CONFIG, runId: 'A' })
// Reversing the input order must not change a single verdict: the sort key in
// `decide` exists precisely so arrival order cannot influence the outcome.
const reversed = reconcile({
  gateway: [...runOne.gateway].reverse(),
  ledger: [...runOne.ledger].reverse(),
  config: DEFAULT_MATCH_CONFIG,
  runId: 'A',
})
check(
  'input order does not change the totals',
  JSON.stringify(resultOne.stats.totals) === JSON.stringify(reversed.stats.totals),
  `${JSON.stringify(resultOne.stats.totals)} vs ${JSON.stringify(reversed.stats.totals)}`,
)

/* --- ground truth sweep --------------------------------------------------- */

console.log('\nengine: injected vs detected')

const sweep: GeneratorConfig[] = [
  { count: 1_000, dropRate: 0, duplicateRate: 0, driftRate: 0, timeSkewMs: 0, currency: 'INR', seed: 1 },
  { count: 5_000, dropRate: 0.05, duplicateRate: 0.02, driftRate: 0.03, timeSkewMs: 400, currency: 'INR', seed: 7 },
  { count: 5_000, dropRate: 0.2, duplicateRate: 0.1, driftRate: 0.15, timeSkewMs: 1_500, currency: 'USD', seed: 99 },
  { count: 10_000, dropRate: 0.01, duplicateRate: 0.05, driftRate: 0.08, timeSkewMs: 30_000, currency: 'EUR', seed: 424242 },
  // Deliberately past the drift window: most pairs should break on both sides.
  { count: 2_000, dropRate: 0.02, duplicateRate: 0.01, driftRate: 0.02, timeSkewMs: 80_000, currency: 'INR', seed: 5 },
]

for (const cfg of sweep) {
  const run = generate(cfg, DEFAULT_MATCH_CONFIG)
  const result = reconcile({
    gateway: run.gateway,
    ledger: run.ledger,
    config: DEFAULT_MATCH_CONFIG,
    runId: run.runId,
  })
  const checks = compareToTruth(run.truth, result.stats.totals)
  const disagreements = checks.filter((line) => !line.agrees)

  check(
    `n=${cfg.count} drop=${cfg.dropRate} dup=${cfg.duplicateRate} drift=${cfg.driftRate} skew=${cfg.timeSkewMs}ms`,
    disagreements.length === 0,
    disagreements
      .map((line) => `${line.label}: injected ${line.injected}, detected ${line.detected}`)
      .join('; '),
  )
}

/* --- scale ---------------------------------------------------------------- */

console.log('\nengine: scale')
const big = generate({ ...cfgA, count: 50_000 }, DEFAULT_MATCH_CONFIG)
const started = performance.now()
const bigResult = reconcile({
  gateway: big.gateway,
  ledger: big.ledger,
  config: DEFAULT_MATCH_CONFIG,
  runId: big.runId,
})
const elapsed = performance.now() - started
const rows = big.gateway.length + big.ledger.length
console.log(
  `  info 50,000 pairs (${rows.toLocaleString()} rows) in ${elapsed.toFixed(0)}ms ` +
    `= ${Math.round(rows / (elapsed / 1000)).toLocaleString()} rows/s`,
)
check('50k pairs reconcile under 3s', elapsed < 3_000, `took ${elapsed.toFixed(0)}ms`)
check(
  '50k ground truth agrees',
  compareToTruth(big.truth, bigResult.stats.totals).every((line) => line.agrees),
)
check(
  'every input row is accounted for exactly once',
  bigResult.rows.reduce(
    (sum, row) => sum + (row.gateway ? 1 : 0) + (row.ledger ? 1 : 0),
    0,
  ) === rows,
)

console.log(
  failures === 0 ? '\nAll engine checks passed.\n' : `\n${failures} check(s) failed.\n`,
)
process.exit(failures === 0 ? 0 : 1)
