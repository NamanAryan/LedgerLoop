/**
 * The layer cascade — what each of the five layers actually resolved.
 *
 * A match rate says how much reconciled. It does not say *why*, and "why" is the
 * question an operator asks first: a book that matches 99% at layer 1 is healthy,
 * and a book that matches 99% only after layer 2 rescues it has a clock problem
 * somewhere upstream. The layers run in order, each seeing only what the previous
 * one could not resolve, so the counts below are a decomposition of one stream
 * rather than five independent tallies.
 */

import { formatCount, formatPercent } from '../format'
import type { ReconTotals } from '../engine/types'

interface Layer {
  id: string
  name: string
  rule: string
  count: number
  color: string
}

export function LayerCascade({ totals }: { totals: ReconTotals }) {
  const layers: Layer[] = [
    {
      id: 'L1',
      name: 'Exact',
      rule: 'same amount, within 2s',
      count: totals.matchedExact,
      color: 'var(--ok)',
    },
    {
      id: 'L2',
      name: 'Time drift',
      rule: 'same amount, within 60s',
      count: totals.matchedTimeDrift,
      color: 'var(--ok)',
    },
    {
      id: 'L3',
      name: 'Amount drift',
      rule: 'inside tolerance, flagged',
      count: totals.amountDrift,
      color: 'var(--bad)',
    },
    {
      id: 'L4',
      name: 'Duplicate',
      rule: 'repeat idempotency key',
      count: totals.duplicates,
      color: 'var(--warn)',
    },
    {
      id: 'L5',
      name: 'Sweep',
      rule: 'no counterparty found',
      count: totals.unmatchedGateway + totals.unmatchedLedger,
      color: 'var(--bad)',
    },
  ]

  const decided = layers.reduce((sum, layer) => sum + layer.count, 0)

  return (
    <div className="cascade">
      {layers.map((layer) => {
        const share = decided === 0 ? 0 : layer.count / decided
        return (
          <div className="cascade-row" key={layer.id}>
            <div className="cascade-label">
              <b>{layer.id}</b>
              {layer.name}
            </div>
            <div
              className="cascade-track"
              role="img"
              aria-label={`${layer.name}: ${formatCount(layer.count)} rows, ${formatPercent(share, 1)} of all decisions`}
            >
              {/* A layer that resolved nothing still shows a hairline, so the row
                  reads as "zero" rather than as a rendering failure. */}
              <span
                className="cascade-fill"
                style={{
                  width: layer.count === 0 ? '1px' : `${Math.max(share * 100, 0.4)}%`,
                  background: layer.count === 0 ? 'var(--line-strong)' : layer.color,
                }}
              />
            </div>
            <div className="cascade-meta">
              {formatCount(layer.count)} <em>{formatPercent(share, 1)}</em>
            </div>
          </div>
        )
      })}
      <div className="cascade-row" style={{ gridTemplateColumns: '1fr' }}>
        <span className="field-hint" style={{ margin: 0 }}>
          Layers run in order; each sees only what the one above it could not resolve.
        </span>
      </div>
    </div>
  )
}
