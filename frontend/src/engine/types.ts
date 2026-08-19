/** Domain types, mirroring the backend's native enums. */

import type { Minor } from './money'

export type Side = 'gateway' | 'ledger'

export type ReconStatus =
  | 'matched'
  | 'unmatched_gateway_only'
  | 'unmatched_ledger_only'
  | 'duplicate'
  | 'amount_drift'

export type MatchLayer =
  | 'exact'
  | 'time_drift'
  | 'amount_drift'
  | 'duplicate'
  | 'unmatched_sweep'

/** Statuses that require a human to look at them. */
export const EXCEPTION_STATUSES: ReadonlySet<ReconStatus> = new Set<ReconStatus>([
  'amount_drift',
  'unmatched_gateway_only',
  'unmatched_ledger_only',
])

/** Statuses excluded from active counts, per the duplicate-suppression rule. */
export const SUPPRESSED_STATUSES: ReadonlySet<ReconStatus> = new Set<ReconStatus>(['duplicate'])

/** The subset of a raw row that matching actually depends on. */
export interface TxnFacts {
  readonly side: Side
  readonly rowId: number
  readonly txnId: string
  readonly amount: Minor
  readonly currency: string
  /** Epoch milliseconds. Both sides are absolute instants, never wall-clock strings. */
  readonly occurredAt: number
  readonly idempotencyKey: string
}

/** Tuning knobs, passed explicitly so nothing here reads global config. */
export interface MatchConfig {
  /** Layer 1 window, milliseconds. */
  readonly exactWindowMs: number
  /** Layers 2 and 3 window, milliseconds. */
  readonly driftWindowMs: number
  /** Layer 3 percentage allowance, in basis points. 100 == 1%. */
  readonly amountDriftBps: number
  /** Layer 3 flat floor, in minor units. */
  readonly amountDriftFloor: Minor
}

export const DEFAULT_MATCH_CONFIG: MatchConfig = {
  exactWindowMs: 2_000,
  driftWindowMs: 60_000,
  amountDriftBps: 100,
  amountDriftFloor: 1_000,
}

/** A terminal classification for one transaction, or one pair of them. */
export interface Decision {
  readonly status: ReconStatus
  readonly layer: MatchLayer
  readonly gatewayRowId: number | null
  readonly ledgerRowId: number | null
  readonly notes: string | null
}

export function opensException(decision: Decision): boolean {
  return EXCEPTION_STATUSES.has(decision.status)
}

/** One resolved line in the reconciliation output: a pair, or an orphan. */
export interface ReconRow {
  readonly id: number
  readonly txnId: string
  readonly status: ReconStatus
  readonly layer: MatchLayer
  readonly currency: string
  readonly gateway: TxnFacts | null
  readonly ledger: TxnFacts | null
  readonly notes: string | null
  /** Signed skew, ledger minus gateway, in ms. Null when only one side exists. */
  readonly skewMs: number | null
  /** Signed difference, gateway minus ledger, in minor units. Null when one-sided. */
  readonly amountDelta: Minor | null
}

export interface ReconTotals {
  readonly matchedExact: number
  readonly matchedTimeDrift: number
  readonly amountDrift: number
  readonly unmatchedGateway: number
  readonly unmatchedLedger: number
  readonly duplicates: number
}

export interface ReconStats {
  readonly totals: ReconTotals
  /** Rows the match rate is computed over: everything but duplicates. */
  readonly active: number
  readonly matched: number
  readonly matchRate: number
  readonly exceptions: number
  readonly gatewayRows: number
  readonly ledgerRows: number
  /** Wall-clock milliseconds spent matching, excluding parse and generation. */
  readonly elapsedMs: number
  /** p50/p99 of |ledger - gateway| skew across matched pairs, in ms. */
  readonly skewP50Ms: number
  readonly skewP99Ms: number
}

export interface ReconResult {
  readonly rows: ReconRow[]
  readonly stats: ReconStats
  readonly config: MatchConfig
  readonly runId: string
  readonly startedAt: number
}

/** What the synthetic generator injected, recorded before the engine ever runs. */
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
