/**
 * Synthetic two-stream generator with recorded ground truth.
 *
 * The point of this module is not to make pretty demo data. It is to inject a known
 * number of known defects so the **backend's** output can be checked against them. If
 * 200 breaks were injected and the dashboard reports 196, the difference is a bug —
 * and the ground-truth panel puts that comparison on screen instead of asking anyone
 * to take the match rate on faith.
 *
 * This is the one place in the frontend that reasons about the layer rules, and it is
 * not a matcher. It never looks at a reconciliation result and classifies it; it
 * records what it *constructed*, before anything is sent. That distinction is the
 * whole value: if the generator asked the engine what it thought, a bug in the engine
 * would be mirrored into the ground truth and the two would agree on the wrong answer.
 *
 * Which is also why `BACKEND_RULES` below is a transcription of the backend's defaults
 * rather than a value fetched from it. A prediction that reads its answer from the
 * thing it is predicting is not a prediction.
 */

import type { Minor } from './money'
import type { SourceRow } from './csv'

/**
 * The backend's matching windows, transcribed from `ledgerloop/config.py`.
 *
 * Keep in step with the deployment. If the server runs a different drift window than
 * this claims, the injected-vs-detected panel will report disagreements that are
 * configuration drift rather than engine bugs — which is worth knowing, but should be
 * read as "these two are out of step", not "the matcher is broken".
 */
export const BACKEND_RULES = {
  /** Layer 1 window. `exact_window_s: 2` */
  exactWindowMs: 2_000,
  /** Layers 2 and 3 window. `drift_window_s: 60` */
  driftWindowMs: 60_000,
  /** Layer 3 percentage allowance, in basis points. `amount_drift_pct: 0.01` */
  amountDriftBps: 100,
  /** Layer 3 flat floor, in minor units. `amount_drift_abs: 10.0` */
  amountDriftFloor: 1_000,
} as const

export type BackendRules = typeof BACKEND_RULES

export interface GeneratorConfig {
  /** Transaction pairs to plan, before duplicates are added. */
  count: number
  /** Share of pairs whose ledger side is never emitted. 0..1 */
  dropRate: number
  /** Share of pairs whose gateway row is submitted twice. 0..1 */
  duplicateRate: number
  /** Share of pairs whose ledger amount disagrees with the gateway. 0..1 */
  driftRate: number
  /** Mean clock offset between the two sides, in milliseconds. */
  timeSkewMs: number
  currency: string
  seed: number
  /**
   * Instant the generated window ends at. Defaults to "now" so a run reads like a
   * recent settlement batch, but it is a parameter rather than a call to the clock
   * inside the loop — otherwise the same seed would produce different timestamps on
   * every run and "reproducible" would be a claim this module could not keep.
   */
  anchorMs?: number
}

export const DEFAULT_GENERATOR_CONFIG: GeneratorConfig = {
  count: 600,
  dropRate: 0.04,
  duplicateRate: 0.02,
  driftRate: 0.03,
  timeSkewMs: 400,
  currency: 'INR',
  seed: 20260819,
}

/**
 * mulberry32 — small, fast, and seeded.
 *
 * Seeded on purpose: a run the user can reproduce is a run they can argue with. The
 * same seed and knobs always produce the same streams and the same ground truth.
 */
function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function gauss(next: () => number, mean: number, stdDev: number): number {
  // Box-Muller. u is nudged off zero because log(0) is not a clock offset.
  const u = Math.max(next(), Number.EPSILON)
  const v = next()
  return mean + stdDev * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v)
}

export interface GroundTruth {
  readonly matchedExact: number
  readonly matchedTimeDrift: number
  readonly amountDrift: number
  readonly unmatchedGatewayOnly: number
  readonly unmatchedBoth: number
  readonly duplicates: number
  readonly gatewayRows: number
  readonly ledgerRows: number
}

interface PlannedTxn {
  amount: Minor
  ledgerAmount: Minor
  occurredAt: number
  ledgerOccurredAt: number
  dropped: boolean
}

type Verdict =
  | 'matched_exact'
  | 'matched_time_drift'
  | 'amount_drift'
  | 'unmatched_gateway_only'
  | 'unmatched_both'

/** Predict the engine's verdict for one planned pair, from the rules alone. */
function classify(txn: PlannedTxn, rules: BackendRules): Verdict {
  if (txn.dropped) return 'unmatched_gateway_only'

  const dt = Math.abs(txn.ledgerOccurredAt - txn.occurredAt)
  if (dt > rules.driftWindowMs) return 'unmatched_both'

  if (txn.ledgerAmount !== txn.amount) {
    // Integer basis-point arithmetic, mirroring the backend's Decimal comparison:
    // max(1% of the gateway amount, the flat floor), with both sides scaled up so no
    // step touches a binary fraction.
    const tolerance = Math.max(
      Math.abs(txn.amount) * rules.amountDriftBps,
      rules.amountDriftFloor * 10_000,
    )
    if (Math.abs(txn.amount - txn.ledgerAmount) * 10_000 <= tolerance) return 'amount_drift'
    return 'unmatched_both'
  }

  return dt <= rules.exactWindowMs ? 'matched_exact' : 'matched_time_drift'
}

export interface GeneratedRun {
  gateway: SourceRow[]
  ledger: SourceRow[]
  truth: GroundTruth
  runId: string
}

export function generate(cfg: GeneratorConfig, rules: BackendRules = BACKEND_RULES): GeneratedRun {
  const next = rng(cfg.seed)
  const runId = `${cfg.seed.toString(36).toUpperCase().slice(-4)}${Date.now().toString(36).toUpperCase().slice(-4)}`

  // Spread the run across a plausible window rather than stacking every transaction on
  // one instant: roughly 100ms apart, clamped to somewhere between a minute and six
  // hours, so timestamps read like a real settlement window at any volume.
  const spanMs = Math.min(6 * 3_600_000, Math.max(60_000, cfg.count * 100))
  const startAt = (cfg.anchorMs ?? Date.now()) - spanMs
  const step = spanMs / Math.max(1, cfg.count)

  const gateway: SourceRow[] = []
  const ledger: SourceRow[] = []
  const truth = {
    matchedExact: 0,
    matchedTimeDrift: 0,
    amountDrift: 0,
    unmatchedGatewayOnly: 0,
    unmatchedBoth: 0,
    duplicates: 0,
    gatewayRows: 0,
    ledgerRows: 0,
  }

  let line = 1

  for (let i = 0; i < cfg.count; i += 1) {
    const txnId = `TXN-${runId}-${i.toString().padStart(6, '0')}`

    // 50.00 to 50,000.00 in the run's currency, held as minor units from here on.
    const amount = 5_000 + Math.floor(next() * 4_995_000)
    const occurredAt = Math.round(startAt + i * step + next() * step)

    const dropped = next() < cfg.dropRate
    const duplicate = next() < cfg.duplicateRate
    const drifted = next() < cfg.driftRate

    let ledgerAmount = amount
    if (drifted) {
      // 0.2%-0.8% of the gateway amount: comfortably inside the 1% band, so the
      // expected classification is unambiguous. The sign varies so both a short and a
      // long ledger are exercised.
      const fraction = (0.002 + next() * 0.006) * (next() < 0.5 ? 1 : -1)
      ledgerAmount = Math.round(amount * (1 + fraction))
      // Tiny amounts can round the drift away entirely; nudge so a pair marked
      // "drifted" genuinely differs.
      if (ledgerAmount === amount) ledgerAmount = amount + 1
    }

    const skewMs = Math.round(gauss(next, cfg.timeSkewMs, Math.max(cfg.timeSkewMs * 0.3, 1)))
    const ledgerOccurredAt = occurredAt + skewMs

    switch (classify({ amount, ledgerAmount, occurredAt, ledgerOccurredAt, dropped }, rules)) {
      case 'matched_exact':
        truth.matchedExact += 1
        break
      case 'matched_time_drift':
        truth.matchedTimeDrift += 1
        break
      case 'amount_drift':
        truth.amountDrift += 1
        break
      case 'unmatched_gateway_only':
        truth.unmatchedGatewayOnly += 1
        break
      case 'unmatched_both':
        truth.unmatchedBoth += 1
        break
    }

    // The run id is part of every key, so two runs of the same seed are genuinely
    // different submissions rather than one enormous duplicate. Re-posting a *single*
    // run is still a no-op, which is what makes an interrupted upload safe to resume.
    const gatewayRow: SourceRow = {
      side: 'gateway',
      line: line++,
      txnId,
      amount,
      currency: cfg.currency,
      occurredAt,
      idempotencyKey: `gw-${runId}-${txnId}`,
    }
    gateway.push(gatewayRow)

    if (duplicate) {
      // A retry of the same submission: the *same* idempotency key. This is what a
      // gateway actually sends when a client times out and retries, and the backend
      // is expected to answer 202 with duplicate:true rather than store it twice.
      gateway.push({ ...gatewayRow, line: line++ })
      truth.duplicates += 1
    }

    if (!dropped) {
      ledger.push({
        side: 'ledger',
        line: line++,
        txnId,
        amount: ledgerAmount,
        currency: cfg.currency,
        occurredAt: ledgerOccurredAt,
        idempotencyKey: `ldg-${runId}-${txnId}`,
      })
    }
  }

  truth.gatewayRows = gateway.length
  truth.ledgerRows = ledger.length

  return { gateway, ledger, truth, runId }
}

/** One line of the injected-vs-detected comparison. */
export interface TruthCheck {
  label: string
  injected: number
  detected: number
  agrees: boolean
  hint: string
}

/** What the backend reported, in the shape this comparison needs. */
export interface DetectedCounts {
  matchedExact: number
  matchedTimeDrift: number
  amountDrift: number
  unmatchedGateway: number
  unmatchedLedger: number
  duplicates: number
}

/**
 * Line up what was injected against what the backend reported.
 *
 * The two mappings that are not one-to-one are the interesting ones. A pair planned as
 * `unmatched_both` shows up on *both* orphan counts, because the gateway row and the
 * ledger row each end up alone — so the gateway line adds the drops and the both-sided
 * breaks together, and the ledger line counts only the latter.
 */
export function compareToTruth(truth: GroundTruth, detected: DetectedCounts): TruthCheck[] {
  const lines: Omit<TruthCheck, 'agrees'>[] = [
    {
      label: 'Matched · exact',
      injected: truth.matchedExact,
      detected: detected.matchedExact,
      hint: 'layer 1 — same amount, within 2s',
    },
    {
      label: 'Matched · time drift',
      injected: truth.matchedTimeDrift,
      detected: detected.matchedTimeDrift,
      hint: 'layer 2 — same amount, within 60s',
    },
    {
      label: 'Amount drift',
      injected: truth.amountDrift,
      detected: detected.amountDrift,
      hint: 'layer 3 — inside tolerance, still an exception',
    },
    {
      label: 'Unmatched · gateway',
      injected: truth.unmatchedGatewayOnly + truth.unmatchedBoth,
      detected: detected.unmatchedGateway,
      hint: 'dropped ledger rows plus both-sided breaks',
    },
    {
      label: 'Unmatched · ledger',
      injected: truth.unmatchedBoth,
      detected: detected.unmatchedLedger,
      hint: 'the ledger half of each both-sided break',
    },
    {
      label: 'Duplicates',
      injected: truth.duplicates,
      detected: detected.duplicates,
      hint: 'layer 4 — repeat idempotency keys, suppressed',
    },
  ]

  return lines.map((line) => ({ ...line, agrees: line.injected === line.detected }))
}

/** Serialise a generated side to CSV, so a demo run can leave the browser. */
export function toCsv(rows: readonly SourceRow[], side: 'gateway' | 'ledger'): string {
  const idColumn = side === 'gateway' ? 'idempotency_key' : 'entry_ref'
  const header = `txn_id,amount,currency,timestamp,${idColumn}\n`
  const body = rows
    .map((row) => {
      const amount = (row.amount / 100).toFixed(2)
      return `${row.txnId},${amount},${row.currency},${new Date(row.occurredAt).toISOString()},${row.idempotencyKey}`
    })
    .join('\n')
  return header + body + '\n'
}

/**
 * Guard rails for the control panel.
 *
 * The ceiling is far lower than it was when the engine ran in this tab. Every gateway
 * row is now its own HTTP request — a webhook is one transaction by definition — so
 * 50,000 rows is 50,000 round trips, which is minutes of waiting against a free-tier
 * instance that is also running the matcher. 10,000 is about twenty seconds and still
 * produces a statistically interesting book.
 */
export const LIMITS = {
  count: { min: 100, max: 10_000, step: 100 },
  rate: { min: 0, max: 0.5, step: 0.005 },
  timeSkewMs: { min: 0, max: 90_000, step: 50 },
} as const

export function clampGeneratorConfig(cfg: GeneratorConfig): GeneratorConfig {
  const clamp = (value: number, min: number, max: number) =>
    Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : min
  return {
    ...cfg,
    count: Math.round(clamp(cfg.count, LIMITS.count.min, LIMITS.count.max)),
    dropRate: clamp(cfg.dropRate, LIMITS.rate.min, LIMITS.rate.max),
    duplicateRate: clamp(cfg.duplicateRate, LIMITS.rate.min, LIMITS.rate.max),
    driftRate: clamp(cfg.driftRate, LIMITS.rate.min, LIMITS.rate.max),
    timeSkewMs: clamp(cfg.timeSkewMs, LIMITS.timeSkewMs.min, LIMITS.timeSkewMs.max),
  }
}
