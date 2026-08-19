/** Route `/results` — the reconciliation dashboard, with exceptions expanding in place. */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { compareToTruth } from '../engine/generate'
import { EXCEPTION_STATUSES, type GroundTruth, type ReconResult } from '../engine/types'
import {
  formatCount,
  formatDuration,
  formatPercent,
  formatThroughput,
} from '../format'
import { LayerCascade } from '../components/LayerCascade'
import { Eyebrow, MetricTile, Panel, PanelBody, PanelHead } from '../components/primitives'
import { TransactionTable } from '../components/TransactionTable'
import type { Resolution } from '../components/ExceptionDetail'

type FilterKey = 'all' | 'open' | 'matched' | 'unmatched' | 'duplicates' | 'drift'

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'open', label: 'Open' },
  { key: 'matched', label: 'Matched' },
  { key: 'unmatched', label: 'Unmatched' },
  { key: 'duplicates', label: 'Duplicates' },
  { key: 'drift', label: 'Drift' },
]

export function Dashboard({
  run,
  truth,
  totalMs,
  source,
  resolutions,
  onResolutionChange,
}: {
  run: ReconResult
  truth: GroundTruth | null
  totalMs: number
  source: string
  resolutions: Record<number, Resolution>
  onResolutionChange: (id: number, next: Resolution) => void
}) {
  const [filter, setFilter] = useState<FilterKey>('all')
  const { stats, rows } = run

  const groups = useMemo(() => {
    const matched = rows.filter((row) => row.status === 'matched')
    const unmatched = rows.filter(
      (row) =>
        row.status === 'unmatched_gateway_only' || row.status === 'unmatched_ledger_only',
    )
    const duplicates = rows.filter((row) => row.status === 'duplicate')
    const drift = rows.filter((row) => row.status === 'amount_drift')
    const exceptions = rows.filter((row) => EXCEPTION_STATUSES.has(row.status))
    return { matched, unmatched, duplicates, drift, exceptions }
  }, [rows])

  /* Unresolved exceptions are a filter over the same feed, not a second table:
     the queue and the list were always the same rows read two ways. */
  const open = groups.exceptions.filter(
    (row) => (resolutions[row.id]?.resolvedAt ?? null) === null,
  )

  const views: Record<FilterKey, typeof rows> = {
    all: rows,
    open,
    matched: groups.matched,
    unmatched: groups.unmatched,
    duplicates: groups.duplicates,
    drift: groups.drift,
  }

  const truthChecks =
    truth === null
      ? null
      : compareToTruth(truth, {
          matchedExact: stats.totals.matchedExact,
          matchedTimeDrift: stats.totals.matchedTimeDrift,
          amountDrift: stats.totals.amountDrift,
          unmatchedGateway: stats.totals.unmatchedGateway,
          unmatchedLedger: stats.totals.unmatchedLedger,
          duplicates: stats.totals.duplicates,
        })
  const truthAgrees = truthChecks?.every((check) => check.agrees) ?? false

  return (
    <div className="space-y-10 py-10 pb-28">
      <div className="flex flex-wrap items-baseline justify-between gap-6">
        <div>
          <Eyebrow>
            {run.runId} · {source} · {formatDuration(totalMs)}
          </Eyebrow>
          <h1 className="mt-3 font-display text-4xl font-light tracking-tight text-cream sm:text-5xl">
            Reconciliation
          </h1>
        </div>
        <Link
          to="/reconcile"
          className="rounded-md border border-line-2 px-6 py-2.5 text-sm font-medium tracking-wide text-cream transition-colors duration-300 ease-refined hover:border-gold/70 hover:text-gold"
        >
          New run
        </Link>
      </div>

      <Panel className="grid grid-cols-1 divide-y divide-line sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-4">
        <div className="sm:border-b sm:border-line lg:border-b-0">
          <MetricTile
            label="Matched"
            value={formatCount(stats.matched)}
            secondary={`${formatPercent(stats.matchRate)} of ${formatCount(stats.active)} active`}
          />
        </div>
        <div className="sm:border-b sm:border-l sm:border-line lg:border-b-0">
          <MetricTile
            label="Unmatched"
            value={formatCount(stats.totals.unmatchedGateway + stats.totals.unmatchedLedger)}
            secondary={`${formatCount(stats.totals.unmatchedGateway)} gateway · ${formatCount(stats.totals.unmatchedLedger)} ledger`}
          />
        </div>
        <div className="lg:border-l lg:border-line">
          <MetricTile
            label="Duplicates"
            value={formatCount(stats.totals.duplicates)}
          />
        </div>
        <div className="sm:border-l sm:border-line">
          <MetricTile
            label="Processing"
            value={formatDuration(stats.elapsedMs)}
            secondary={formatThroughput(
              stats.gatewayRows + stats.ledgerRows,
              stats.elapsedMs,
            )}
          />
        </div>
      </Panel>

      <Panel>
        <PanelHead title="Resolution by layer" />
        <LayerCascade totals={stats.totals} />
      </Panel>

      {truthChecks !== null && (
        <Panel>
          <PanelHead
            title="Injected vs detected"
            aside={
              <span
                className={`text-[10px] font-medium uppercase tracking-[0.22em] ${truthAgrees ? 'text-sage' : 'text-rose'}`}
              >
                {truthAgrees ? 'All counts agree' : 'Discrepancy found'}
              </span>
            }
          />
          <PanelBody>
            <div className="grid grid-cols-[1fr_3.5rem_3.5rem_4.5rem] gap-3 border-b border-line pb-3 sm:grid-cols-[1fr_5rem_5rem_6rem] sm:gap-4">
              <Eyebrow>Classification</Eyebrow>
              <Eyebrow className="text-right">Injected</Eyebrow>
              <Eyebrow className="text-right">Detected</Eyebrow>
              <Eyebrow className="text-right">Verdict</Eyebrow>
            </div>

            {truthChecks.map((check) => (
              <div
                key={check.label}
                className="grid grid-cols-[1fr_3.5rem_3.5rem_4.5rem] items-baseline gap-3 border-b border-line py-4 last:border-b-0 sm:grid-cols-[1fr_5rem_5rem_6rem] sm:gap-4"
              >
                <div className="text-sm text-cream">{check.label}</div>
                <div className="text-right text-sm font-light text-ash">
                  {formatCount(check.injected)}
                </div>
                <div className="text-right text-sm font-light text-ash">
                  {formatCount(check.detected)}
                </div>
                <div
                  className={`text-right text-xs font-light ${check.agrees ? 'text-sage' : 'text-rose'}`}
                >
                  {check.agrees ? 'Match' : `Off by ${check.detected - check.injected}`}
                </div>
              </div>
            ))}
          </PanelBody>
        </Panel>
      )}

      <Panel>
        <div className="flex flex-wrap items-center justify-between gap-5 border-b border-line px-6 py-5 sm:px-8">
          <h2 className="text-xl font-normal tracking-tight text-cream">Transactions</h2>
          <div className="flex flex-wrap gap-2">
            {FILTERS.map((option) => {
              const active = filter === option.key
              return (
                <button
                  key={option.key}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setFilter(option.key)}
                  className={`rounded-full border px-3.5 py-1 text-xs font-light transition-colors duration-300 ease-refined ${
                    active
                      ? 'border-gold/60 text-gold'
                      : 'border-line text-ash hover:border-line-2 hover:text-cream'
                  }`}
                >
                  {option.label}
                  <span className={`ml-2 ${active ? 'text-gold/60' : 'text-slate'}`}>
                    {formatCount(views[option.key].length)}
                  </span>
                </button>
              )
            })}
          </div>
        </div>
        <TransactionTable
          rows={views[filter]}
          resetKey={`${run.runId}-${filter}`}
          emptyMessage={
            filter === 'open'
              ? 'Nothing open. Every exception in this run is resolved.'
              : 'No rows in this view.'
          }
          resolutions={resolutions}
          onResolutionChange={onResolutionChange}
        />
      </Panel>
    </div>
  )
}
