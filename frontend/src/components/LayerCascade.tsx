/**
 * The layer cascade — what each of the five layers actually resolved.
 *
 * A match rate says how much reconciled. It does not say *why*, and "why" is the
 * question an operator asks first: a book that matches 99% at layer 1 is healthy,
 * and a book that matches 99% only after layer 2 rescues it has a clock problem
 * somewhere upstream. The layers run in order, each seeing only what the previous
 * one could not resolve, so the counts below decompose one stream rather than
 * tally five independent ones.
 *
 * The rule under each row is not decoration: its filled length is that layer's
 * share of every decision the run made, so the five fills sum to the whole
 * stream. The divider is the chart.
 */

import { formatCount, formatPercent } from '../format'
import type { ReconTotals } from '../engine/types'
import { Numeral } from './primitives'

interface Layer {
  name: string
  rule: string
  count: number
  fill: string
}

export function LayerCascade({ totals }: { totals: ReconTotals }) {
  const layers: Layer[] = [
    {
      name: 'Exact',
      rule: 'Same amount, within two seconds',
      count: totals.matchedExact,
      fill: 'bg-sage',
    },
    {
      name: 'Time drift',
      rule: 'Same amount, within sixty seconds',
      count: totals.matchedTimeDrift,
      fill: 'bg-sage/60',
    },
    {
      name: 'Amount drift',
      rule: 'Inside tolerance, flagged for review',
      count: totals.amountDrift,
      fill: 'bg-rose',
    },
    {
      name: 'Duplicate',
      rule: 'Repeat idempotency key',
      count: totals.duplicates,
      fill: 'bg-gold',
    },
    {
      name: 'Sweep',
      rule: 'No counterparty found on either side',
      count: totals.unmatchedGateway + totals.unmatchedLedger,
      fill: 'bg-rose/60',
    },
  ]

  const decided = layers.reduce((sum, layer) => sum + layer.count, 0)
  let remaining = decided

  return (
    <div className="px-6 sm:px-8">
      {layers.map((layer, index) => {
        const share = decided === 0 ? 0 : layer.count / decided
        // What this layer was handed, before it took its cut.
        const carriedIn = remaining
        remaining -= layer.count

        return (
          <div key={layer.name} className="relative py-6">
            <div className="flex items-baseline justify-between gap-4">
              <div className="flex items-baseline gap-5">
                <Numeral n={index + 1} className="w-6 shrink-0" />
                <h3 className="text-base font-normal tracking-tight text-cream">
                  {layer.name}
                </h3>
              </div>
              <div className="shrink-0 font-display text-2xl font-light leading-none text-cream">
                {formatCount(layer.count)}
              </div>
            </div>

            <div className="mt-1.5 flex flex-col gap-0.5 pl-11 text-xs font-light text-slate sm:flex-row sm:items-baseline sm:justify-between sm:gap-8">
              <p>
                {layer.rule} · {formatCount(carriedIn)} reached this layer
              </p>
              <p className="shrink-0 sm:text-right">{formatPercent(share, 1)} of all rows</p>
            </div>

            {/* The row's own divider, filled to this layer's share. */}
            <div
              className="absolute inset-x-0 bottom-0 h-px overflow-hidden bg-line"
              role="img"
              aria-label={`${layer.name}: ${formatCount(layer.count)} rows, ${formatPercent(share, 1)} of all decisions`}
            >
              <span
                className={`block h-full transition-[width] duration-700 ease-refined ${layer.count === 0 ? 'bg-line-2' : layer.fill}`}
                style={{ width: layer.count === 0 ? '2px' : `${Math.max(share * 100, 0.5)}%` }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}
