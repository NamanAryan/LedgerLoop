/**
 * The layer cascade — what each of the five layers actually resolved.
 *
 * A match rate says how much reconciled. It does not say *why*, and "why" is the
 * question an operator asks first: a book that matches 99% at layer 1 is healthy, and
 * a book that matches 99% only after layer 2 rescues it has a clock problem somewhere
 * upstream. The layers run in order, each seeing only what the previous one could not
 * resolve, so the counts below decompose one stream rather than tally five.
 *
 * Every number here comes from `GET /v1/stats`. The one derivation is layer 1, which
 * the API does not report directly: it returns `matched` and `matched_via_time_drift`,
 * and exact is the remainder. That split is only possible because the backend keys the
 * time-drift count on `match_layer` rather than `status` — both are matches, and
 * conflating them would hide exactly how much of the match rate leans on tolerance.
 *
 * The rule under each row is not decoration: its filled length is that layer's share
 * of every decision made, so the five fills sum to the whole stream.
 */

import { formatCount, formatPercent } from '../format'
import type { StatsOut } from '../api/types'
import { Numeral } from './primitives'

interface Layer {
  name: string
  rule: string
  count: number
  fill: string
}

export function LayerCascade({ stats }: { stats: StatsOut }) {
  // Matched, minus the ones that needed layer 2. Clamped at zero: the two figures are
  // read from one query so they cannot legitimately disagree, but a negative bar from
  // an unexpected server response would be a worse bug than a flat one.
  const exact = Math.max(stats.matched - stats.matched_via_time_drift, 0)

  const layers: Layer[] = [
    {
      name: 'Exact',
      rule: 'Same amount, within two seconds',
      count: exact,
      fill: 'bg-sage',
    },
    {
      name: 'Time drift',
      rule: 'Same amount, within sixty seconds',
      count: stats.matched_via_time_drift,
      fill: 'bg-sage/60',
    },
    {
      name: 'Amount drift',
      rule: 'Inside tolerance, flagged for review',
      count: stats.drift,
      fill: 'bg-rose',
    },
    {
      name: 'Duplicate',
      rule: 'Repeat idempotency key',
      count: stats.duplicates,
      fill: 'bg-gold',
    },
    {
      name: 'Sweep',
      rule: 'No counterparty found on either side',
      count: stats.unmatched,
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
                <h3 className="text-base font-normal tracking-tight text-cream">{layer.name}</h3>
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
