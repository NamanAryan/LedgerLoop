/** Screen 4 and 5 — the reconciliation dashboard, with exceptions expanding in place. */

import { useMemo, useState } from 'react'
import { compareToTruth } from '../engine/generate'
import { EXCEPTION_STATUSES, type GroundTruth, type ReconResult } from '../engine/types'
import {
  formatCount,
  formatDuration,
  formatPercent,
  formatThroughput,
  formatTimestamp,
} from '../format'
import { LayerCascade } from '../components/LayerCascade'
import { MetricTile } from '../components/primitives'
import { TransactionTable } from '../components/TransactionTable'
import type { Resolution } from '../components/ExceptionDetail'

type FilterKey = 'all' | 'matched' | 'unmatched' | 'duplicates' | 'drift'

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: 'All' },
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
  onNewRun,
}: {
  run: ReconResult
  truth: GroundTruth | null
  totalMs: number
  source: string
  resolutions: Record<number, Resolution>
  onResolutionChange: (id: number, next: Resolution) => void
  onNewRun: () => void
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

  const filtered = {
    all: rows,
    matched: groups.matched,
    unmatched: groups.unmatched,
    duplicates: groups.duplicates,
    drift: groups.drift,
  }[filter]

  const counts: Record<FilterKey, number> = {
    all: rows.length,
    matched: groups.matched.length,
    unmatched: groups.unmatched.length,
    duplicates: groups.duplicates.length,
    drift: groups.drift.length,
  }

  const openExceptions = groups.exceptions.filter(
    (row) => (resolutions[row.id]?.resolvedAt ?? null) === null,
  )

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
    <div className="stack">
      <div className="page-head">
        <div>
          <h1 className="page-title">Reconciliation</h1>
          <p className="page-sub">
            Run <span className="mono">{run.runId}</span> · {source} ·{' '}
            {formatCount(stats.gatewayRows)} gateway and {formatCount(stats.ledgerRows)} ledger
            rows · finished {formatTimestamp(run.startedAt)} UTC ·{' '}
            {formatDuration(totalMs)} end to end
          </p>
        </div>
        <button type="button" className="btn btn-sm" onClick={onNewRun}>
          New run
        </button>
      </div>

      <div className="tiles">
        <MetricTile
          label="Matched"
          value={formatCount(stats.matched)}
          secondary={`${formatPercent(stats.matchRate)} of ${formatCount(stats.active)} active rows`}
        />
        <MetricTile
          label="Unmatched"
          value={formatCount(stats.totals.unmatchedGateway + stats.totals.unmatchedLedger)}
          secondary={`${formatCount(stats.totals.unmatchedGateway)} gateway · ${formatCount(stats.totals.unmatchedLedger)} ledger`}
        />
        <MetricTile
          label="Duplicates"
          value={formatCount(stats.totals.duplicates)}
          secondary="suppressed from active counts"
        />
        <MetricTile
          label="Processing"
          value={formatDuration(stats.elapsedMs)}
          secondary={`${formatThroughput(stats.gatewayRows + stats.ledgerRows, stats.elapsedMs)} · p99 lag ${formatDuration(stats.skewP99Ms)}`}
        />
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Resolution by layer</h2>
          <span className="eyebrow">
            {formatCount(stats.exceptions)} exceptions · p50 lag {formatDuration(stats.skewP50Ms)}
          </span>
        </div>
        <LayerCascade totals={stats.totals} />
      </section>

      {truthChecks !== null && (
        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Injected vs detected</h2>
            <span className={truthAgrees ? 'eyebrow truth-ok' : 'eyebrow truth-bad'}>
              {truthAgrees ? 'all counts agree' : 'discrepancy found'}
            </span>
          </div>
          <div className="panel-body">
            <p className="panel-note" style={{ marginBottom: 'calc(var(--u) * 4)' }}>
              The generator recorded every defect it injected before the engine ran, using its
              own reading of the layer rules. If these columns disagree, the engine is wrong —
              that is the point of showing them.
            </p>
            <div className="truth-row eyebrow">
              <span>Classification</span>
              <span style={{ textAlign: 'right' }}>Injected</span>
              <span style={{ textAlign: 'right' }}>Detected</span>
              <span style={{ textAlign: 'right' }}>Verdict</span>
            </div>
            {truthChecks.map((check) => (
              <div className="truth-row" key={check.label}>
                <span className="truth-label">
                  <b>{check.label}</b>
                  <span>{check.hint}</span>
                </span>
                <span className="num" style={{ textAlign: 'right' }}>
                  {formatCount(check.injected)}
                </span>
                <span className="num" style={{ textAlign: 'right' }}>
                  {formatCount(check.detected)}
                </span>
                <span className={`truth-verdict ${check.agrees ? 'truth-ok' : 'truth-bad'}`}>
                  {check.agrees ? 'match' : `off by ${check.detected - check.injected}`}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Transactions</h2>
          <div className="chips">
            {FILTERS.map((option) => (
              <button
                key={option.key}
                type="button"
                className="chip"
                aria-pressed={filter === option.key}
                onClick={() => setFilter(option.key)}
              >
                {option.label}
                <span className="chip-count">{formatCount(counts[option.key])}</span>
              </button>
            ))}
          </div>
        </div>
        <TransactionTable
          rows={filtered}
          resetKey={`${run.runId}-${filter}`}
          emptyMessage={`No rows in this view. The run produced ${formatCount(rows.length)} rows in total.`}
          resolutions={resolutions}
          onResolutionChange={onResolutionChange}
        />
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Exception queue</h2>
          <span className="eyebrow">
            {formatCount(openExceptions.length)} open of {formatCount(groups.exceptions.length)}
          </span>
        </div>
        <TransactionTable
          rows={openExceptions}
          resetKey={`${run.runId}-exceptions-${groups.exceptions.length - openExceptions.length}`}
          emptyMessage={
            groups.exceptions.length === 0
              ? 'No exceptions. Every row resolved inside the matching layers.'
              : 'Every exception in this run has been marked resolved.'
          }
          resolutions={resolutions}
          onResolutionChange={onResolutionChange}
        />
      </section>
    </div>
  )
}
