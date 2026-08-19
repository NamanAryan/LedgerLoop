/**
 * The matching engine's pure core.
 *
 * Everything here is a function of its arguments. No clock, no globals, no I/O, no
 * mutation of inputs. The caller assembles the candidate rows and passes them in;
 * this module decides. That split is why a boundary case at exactly 60.000s is a
 * one-line test rather than a fixture.
 *
 * Layer order, each running only on what the previous did not resolve:
 *
 *   1. exact        same txn_id, same currency, same amount, |dt| <= 2s
 *   2. time drift   same txn_id, same currency, same amount, |dt| <= 60s
 *   3. amount drift same txn_id, same currency, |dt| <= 60s,
 *                   |d amount| <= max(1% of the gateway amount, 10 major units)
 *   4. duplicate    the same idempotency key submitted more than once on one side
 *   5. deferred     nothing matched -> both sides eventually reported unmatched
 *
 * A pair that shares a txn_id but breaks currency, or drifts further than layer 3
 * allows, is deliberately *not* matched. Silently pairing a 100 USD gateway line
 * with a 100 INR ledger line would be worse than reporting a break.
 */

import { formatMinor, toleranceDisplay, withinTolerance } from './money'
import type { Decision, MatchConfig, MatchLayer, ReconStatus, TxnFacts } from './types'

/** Layer precedence. Lower wins when several counterparties are viable. */
const LAYER_RANK: Record<string, number> = {
  exact: 0,
  time_drift: 1,
  amount_drift: 2,
}

/** Absolute distance between two occurrence instants, in milliseconds. */
export function timeDelta(a: TxnFacts, b: TxnFacts): number {
  return Math.abs(a.occurredAt - b.occurredAt)
}

function gatewayAndLedger(a: TxnFacts, b: TxnFacts): [TxnFacts, TxnFacts] {
  return a.side === 'gateway' ? [a, b] : [b, a]
}

/**
 * Run layers 1-3 against one specific counterparty.
 *
 * Returns null when this pair is not matchable at all — different txn_id, different
 * currency, same side, or drift beyond every window.
 */
export function classifyPair(
  candidate: TxnFacts,
  other: TxnFacts,
  config: MatchConfig,
): { status: ReconStatus; layer: MatchLayer } | null {
  if (candidate.side === other.side) return null // a row never reconciles against its own side
  if (candidate.txnId !== other.txnId) return null
  if (candidate.currency !== other.currency) return null // cross-currency is never a match

  const dt = timeDelta(candidate, other)
  const sameAmount = candidate.amount === other.amount

  // Layer 1 — exact.
  if (sameAmount && dt <= config.exactWindowMs) {
    return { status: 'matched', layer: 'exact' }
  }

  // Layer 2 — time drift. Still a match: the money moved, the clocks disagreed.
  if (sameAmount && dt <= config.driftWindowMs) {
    return { status: 'matched', layer: 'time_drift' }
  }

  // Layer 3 — amount drift. Not a match: a human confirms the shortfall.
  if (!sameAmount && dt <= config.driftWindowMs) {
    const [gateway, ledger] = gatewayAndLedger(candidate, other)
    // Tolerance is a percentage *of the gateway amount*: the gateway is the
    // authoritative record of what the customer was actually charged, so the
    // allowance must not stretch when the ledger is the side that is wrong.
    if (
      withinTolerance(gateway.amount, ledger.amount, config.amountDriftBps, config.amountDriftFloor)
    ) {
      return { status: 'amount_drift', layer: 'amount_drift' }
    }
  }

  return null
}

/**
 * Best counterparty first: strongest layer, then closest in time, then closest in
 * amount, then lowest row id.
 *
 * The final row-id term is not cosmetic — it makes the choice deterministic when two
 * counterparties are otherwise indistinguishable, so the same input always produces
 * the same output regardless of the order rows arrived in.
 */
function compareCandidates(
  candidate: TxnFacts,
  a: { other: TxnFacts; layer: MatchLayer },
  b: { other: TxnFacts; layer: MatchLayer },
): number {
  const rank = (LAYER_RANK[a.layer] ?? 99) - (LAYER_RANK[b.layer] ?? 99)
  if (rank !== 0) return rank

  const time = timeDelta(candidate, a.other) - timeDelta(candidate, b.other)
  if (time !== 0) return time

  const amount =
    Math.abs(candidate.amount - a.other.amount) - Math.abs(candidate.amount - b.other.amount)
  if (amount !== 0) return amount

  return a.other.rowId - b.other.rowId
}

/**
 * Layers 1-3 against every available counterparty. Null means "defer" — no
 * counterparty in this candidate set is a legal pairing, and the sweeper (layer 5)
 * will report the row unmatched.
 */
export function decide(
  candidate: TxnFacts,
  counterparties: readonly TxnFacts[],
  config: MatchConfig,
): Decision | null {
  let best: { other: TxnFacts; status: ReconStatus; layer: MatchLayer } | null = null

  for (const other of counterparties) {
    const verdict = classifyPair(candidate, other, config)
    if (verdict === null) continue
    const contender = { other, status: verdict.status, layer: verdict.layer }
    if (best === null || compareCandidates(candidate, contender, best) < 0) {
      best = contender
    }
  }

  if (best === null) return null

  const [gateway, ledger] = gatewayAndLedger(candidate, best.other)

  let notes: string | null = null
  if (best.layer === 'time_drift') {
    const drift = timeDelta(candidate, best.other) / 1000
    notes = `clock drift ${drift.toFixed(3)}s within ${(config.driftWindowMs / 1000).toFixed(0)}s window`
  } else if (best.layer === 'amount_drift') {
    const diff = gateway.amount - ledger.amount
    const tol = toleranceDisplay(gateway.amount, config.amountDriftBps, config.amountDriftFloor)
    const signed = `${diff >= 0 ? '+' : '-'}${formatMinor(Math.abs(diff))}`
    notes =
      `amount drift ${signed} (gateway ${formatMinor(gateway.amount)} vs ` +
      `ledger ${formatMinor(ledger.amount)}), tolerance ${formatMinor(tol)}`
  }

  return {
    status: best.status,
    layer: best.layer,
    gatewayRowId: gateway.rowId,
    ledgerRowId: ledger.rowId,
    notes,
  }
}

/**
 * Layer 4. The row already existed when this submission arrived.
 *
 * Note what is *not* here: no counterparty lookup. A duplicate is decided entirely by
 * the idempotency key having been seen before. Duplicates are suppressed from active
 * counts, so this never competes with layers 1-3 for the same row.
 */
export function decideDuplicate(candidate: TxnFacts, submissions: number): Decision {
  return {
    status: 'duplicate',
    layer: 'duplicate',
    gatewayRowId: candidate.side === 'gateway' ? candidate.rowId : null,
    ledgerRowId: candidate.side === 'ledger' ? candidate.rowId : null,
    notes: `${submissions} submissions of idempotency key; first receipt retained`,
  }
}

/** Layer 5. The sweeper gave up waiting for a counterparty. */
export function decideUnmatched(candidate: TxnFacts, reason: string): Decision {
  const gatewaySide = candidate.side === 'gateway'
  return {
    status: gatewaySide ? 'unmatched_gateway_only' : 'unmatched_ledger_only',
    layer: 'unmatched_sweep',
    gatewayRowId: gatewaySide ? candidate.rowId : null,
    ledgerRowId: gatewaySide ? null : candidate.rowId,
    notes: reason,
  }
}
