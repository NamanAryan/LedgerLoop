/**
 * Route `/results` — the reconciliation dashboard, read entirely from the backend.
 *
 * Nothing here computes a verdict. `GET /v1/stats` is polled every 3s and
 * `GET /v1/transactions` every 5s, and the tiles show whatever the engine currently
 * believes. That is a meaningfully different thing from the old local dashboard, which
 * rendered a finished result: the counts here are *converging*, because ingestion
 * returns 202 and matching happens afterwards.
 *
 * Surfacing that convergence honestly is most of the work in this file:
 *
 * * Unmatched is high immediately after an upload and falls as counterparties arrive.
 *   That is correct behaviour, not a bug, and the banner says so rather than letting
 *   the operator conclude the engine is broken.
 * * A row only becomes "unmatched" for real once the sweeper has given up on it, which
 *   is a deliberate wait measured in the server's unmatched window. Until then the
 *   absence of a result is not evidence of a break.
 * * Polling stops declaring "reconciling" once the totals hold still across several
 *   polls, which is the only convergence signal a client can honestly observe.
 *
 * The feed pauses auto-refresh once "load more" is used. Appending keyset pages and
 * simultaneously re-fetching page one would fight itself — the cursor walks backwards
 * through ids while new results land at the top.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { getStats, listExceptions, listTransactions } from '../api/client'
import type {
  ExceptionOut,
  ReconStatus,
  ReconciliationResultOut,
  StatsOut,
  StatsWindow,
} from '../api/types'
import { compareToTruth, type GroundTruth } from '../lib/generate'
import type { IngestSummary } from '../api/ingest'
import { formatCount, formatDuration, formatPercent } from '../format'
import { LayerCascade } from '../components/LayerCascade'
import {
  Alert,
  Button,
  Eyebrow,
  MetricTile,
  Panel,
  PanelBody,
  PanelHead,
  Segmented,
} from '../components/primitives'
import { TransactionTable } from '../components/TransactionTable'

const STATS_POLL_MS = 3_000
const FEED_POLL_MS = 5_000
const PAGE_SIZE = 50
/** Polls with an unchanged total before we stop calling it "reconciling". */
const SETTLE_POLLS = 3

type FilterKey =
  | 'all'
  | 'matched'
  | 'amount_drift'
  | 'duplicate'
  | 'unmatched_gateway_only'
  | 'unmatched_ledger_only'

/**
 * Filters map one-to-one onto the API's `status` parameter, which takes a single
 * status. A combined "unmatched" tab would have to either make two requests and merge
 * them — breaking keyset pagination, since the two id sequences interleave — or filter
 * client-side over a partial page, which would show the wrong count. Two explicit tabs
 * are honest about what the server can actually answer.
 */
const FILTERS: { value: FilterKey; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'matched', label: 'Matched' },
  { value: 'unmatched_gateway_only', label: 'Gateway only' },
  { value: 'unmatched_ledger_only', label: 'Ledger only' },
  { value: 'amount_drift', label: 'Drift' },
  { value: 'duplicate', label: 'Duplicates' },
]

const WINDOWS: { value: StatsWindow; label: string }[] = [
  { value: '1h', label: '1h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
]

export function Dashboard({
  source,
  truth,
  summary,
}: {
  source: string
  truth: GroundTruth | null
  summary: IngestSummary | null
}) {
  const [window, setWindow] = useState<StatsWindow>('1h')
  const [filter, setFilter] = useState<FilterKey>('all')

  const [stats, setStats] = useState<StatsOut | null>(null)
  const [rows, setRows] = useState<ReconciliationResultOut[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [exceptions, setExceptions] = useState<ExceptionOut[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  /** Auto-refresh of the feed. Paused by "load more"; see the module note. */
  const [live, setLive] = useState(true)

  // Convergence tracking. Refs rather than state: they feed a decision inside the poll
  // and must not themselves schedule a re-render.
  const lastTotal = useRef<number | null>(null)
  const stablePolls = useRef(0)
  const [settled, setSettled] = useState(false)
  /** Advanced by each stats poll, so the sweep deadline elapses without its own timer. */
  const [now, setNow] = useState(() => Date.now())

  // --- stats poll ----------------------------------------------------------
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        const next = await getStats(window, controller.signal)
        setStats(next)
        setError(null)
        setNow(Date.now())

        if (lastTotal.current === next.total) {
          stablePolls.current += 1
          if (stablePolls.current >= SETTLE_POLLS) setSettled(true)
        } else {
          stablePolls.current = 0
          setSettled(false)
        }
        lastTotal.current = next.total
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        setError(caught instanceof Error ? caught.message : 'Could not reach the API.')
      }
      timer = globalThis.setTimeout(() => void tick(), STATS_POLL_MS)
    }

    void tick()
    return () => {
      controller.abort()
      if (timer !== undefined) globalThis.clearTimeout(timer)
    }
  }, [window])

  // --- feed poll -----------------------------------------------------------
  const statusFilter: ReconStatus | null = filter === 'all' ? null : filter

  useEffect(() => {
    // A filter change restarts the page walk: the cursor is a position within one
    // filtered sequence and means nothing in another.
    setRows([])
    setCursor(null)
    setLive(true)
  }, [filter])

  useEffect(() => {
    if (!live) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        const page = await listTransactions(
          { status: statusFilter, limit: PAGE_SIZE },
          controller.signal,
        )
        setRows(page.items)
        setCursor(page.next_cursor)
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        // Stats polling already surfaces connectivity problems; a second banner for
        // the same outage would be noise.
      }
      timer = globalThis.setTimeout(() => void tick(), FEED_POLL_MS)
    }

    void tick()
    return () => {
      controller.abort()
      if (timer !== undefined) globalThis.clearTimeout(timer)
    }
  }, [statusFilter, live])

  // --- exceptions ----------------------------------------------------------
  const refreshExceptions = useCallback(async () => {
    try {
      // Both halves: the table needs to know a row is resolved as well as that it is
      // open, or a just-resolved row would render as though nothing had happened.
      const [open, closed] = await Promise.all([
        listExceptions({ status: 'open', limit: 200 }),
        listExceptions({ status: 'closed', limit: 200 }),
      ])
      setExceptions([...open.items, ...closed.items])
    } catch {
      /* The exception queue is supplementary; the feed still renders without it. */
    }
  }, [])

  useEffect(() => {
    void refreshExceptions()
  }, [refreshExceptions, stats?.open_exceptions])

  const exceptionsByResultId = useMemo(() => {
    const map = new Map<number, ExceptionOut>()
    for (const item of exceptions) map.set(item.reconciliation_result_id, item)
    return map
  }, [exceptions])

  const loadMore = async () => {
    if (cursor === null) return
    setLoadingMore(true)
    setLive(false)
    try {
      const page = await listTransactions({ status: statusFilter, limit: PAGE_SIZE, cursor })
      setRows((current) => [...current, ...page.items])
      setCursor(page.next_cursor)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load more rows.')
    } finally {
      setLoadingMore(false)
    }
  }

  // --- ground truth --------------------------------------------------------
  const truthChecks =
    truth === null || stats === null
      ? null
      : compareToTruth(truth, {
          matchedExact: Math.max(stats.matched - stats.matched_via_time_drift, 0),
          matchedTimeDrift: stats.matched_via_time_drift,
          amountDrift: stats.drift,
          unmatchedGateway: stats.unmatched_gateway_only,
          unmatchedLedger: stats.unmatched_ledger_only,
          duplicates: stats.duplicates,
        })
  const truthAgrees = truthChecks?.every((check) => check.agrees) ?? false

  /** Rows sent that the engine has not yet reached a verdict on. */
  const expected =
    summary === null
      ? null
      : summary.gateway.accepted + summary.gateway.duplicates + summary.ledger.accepted

  /**
   * Convergence is two conditions, not one, and missing the second produces a false
   * alarm that looks exactly like an engine bug.
   *
   * The totals holding still is necessary but not sufficient. A row whose counterparty
   * never arrives contributes nothing to any count until the *sweeper* declares it
   * unmatched, and the sweeper deliberately waits `unmatched_after_s` first. So the
   * moment every match resolves, the totals go quiet — while a batch of genuine breaks
   * is still pending. Judging then reports "detected 0, injected 16" for rows the
   * engine is entirely correct not to have ruled on yet.
   *
   * So we also wait out the window the server advertises, counted from the end of
   * ingestion. Without the server telling us that number this would have to be a
   * guess, which is why /v1/stats reports it.
   */
  const sweepDueAt =
    summary === null || stats === null
      ? null
      : summary.finishedAt + (stats.unmatched_after_s + 5) * 1000
  const sweepPending = sweepDueAt !== null && now < sweepDueAt
  const reconciling = !settled || sweepPending

  return (
    <div className="space-y-10 py-10 pb-28">
      <div className="flex flex-wrap items-baseline justify-between gap-6">
        <div>
          <Eyebrow>
            {source}
            {summary !== null && <> · ingested in {formatDuration(summary.elapsedMs)}</>}
          </Eyebrow>
          <h1 className="mt-3 font-display text-4xl font-light tracking-tight text-cream sm:text-5xl">
            Reconciliation
          </h1>
        </div>
        <div className="flex items-center gap-4">
          <Segmented options={WINDOWS} value={window} onChange={setWindow} />
          <Link
            to="/reconcile"
            className="rounded-md border border-line-2 px-6 py-2.5 text-sm font-medium tracking-wide text-cream transition-colors duration-300 ease-refined hover:border-gold/70 hover:text-gold"
          >
            New run
          </Link>
        </div>
      </div>

      {error !== null && <Alert>{error}</Alert>}

      {reconciling && (
        <Alert tone="caution">
          <span className="inline-flex items-center gap-2">
            <span className="size-1.5 animate-pulse rounded-full bg-gold" />
            {sweepPending && settled ? (
              <>
                Matching is done. Waiting out the {stats?.unmatched_after_s}s unmatched
                window before any row with no counterparty is declared a break — the
                sweeper checks each one against live data first, so a late counterparty
                still matches.
              </>
            ) : (
              <>
                Reconciling. The engine matches asynchronously, so these counts are still
                moving
                {expected !== null && stats !== null && expected > stats.total && (
                  <> — {formatCount(expected - stats.total)} rows still to resolve</>
                )}
                . Unmatched stays low until the sweeper runs.
              </>
            )}
          </span>
        </Alert>
      )}

      <Panel className="grid grid-cols-1 divide-y divide-line sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-4">
        <div className="sm:border-b sm:border-line lg:border-b-0">
          <MetricTile
            label="Matched"
            value={stats === null ? '—' : formatCount(stats.matched)}
            secondary={
              stats === null
                ? 'loading'
                : `${formatPercent(stats.match_rate)} of ${formatCount(stats.total)} active`
            }
          />
        </div>
        <div className="sm:border-b sm:border-l sm:border-line lg:border-b-0">
          <MetricTile
            label="Unmatched"
            value={stats === null ? '—' : formatCount(stats.unmatched)}
            secondary={
              stats === null
                ? 'loading'
                : `${formatCount(stats.unmatched_gateway_only)} gateway · ${formatCount(stats.unmatched_ledger_only)} ledger`
            }
          />
        </div>
        <div className="lg:border-l lg:border-line">
          <MetricTile
            label="Open exceptions"
            value={stats === null ? '—' : formatCount(stats.open_exceptions)}
            secondary={stats === null ? 'loading' : `${formatCount(stats.drift)} drift in window`}
          />
        </div>
        <div className="sm:border-l sm:border-line">
          <MetricTile
            label="Match latency"
            value={
              stats?.latency_ms.p50 === null || stats === null
                ? '—'
                : formatDuration(stats.latency_ms.p50)
            }
            secondary={
              stats?.latency_ms.p99 == null
                ? 'server-side p50'
                : `p99 ${formatDuration(stats.latency_ms.p99)}`
            }
          />
        </div>
      </Panel>

      {stats !== null && (
        <Panel>
          <PanelHead title="Resolution by layer" />
          <LayerCascade stats={stats} />
        </Panel>
      )}

      {truthChecks !== null && (
        <Panel>
          <PanelHead
            title="Injected vs detected"
            aside={
              <span
                className={`text-[10px] font-medium uppercase tracking-[0.22em] ${
                  truthAgrees ? 'text-sage' : reconciling ? 'text-gold' : 'text-rose'
                }`}
              >
                {truthAgrees
                  ? 'All counts agree'
                  : reconciling
                    ? 'Still converging'
                    : 'Discrepancy found'}
              </span>
            }
          />
          <PanelBody>
            <p className="mb-5 text-xs font-light leading-relaxed text-slate">
              The generator recorded these counts before anything was sent, deriving them
              from the layer rules rather than from the engine — so a bug in the matcher
              cannot hide by agreeing with itself. Rows are compared against the real
              backend.
            </p>
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
                <div className="text-sm text-cream">
                  {check.label}
                  <span className="ml-2 text-xs font-light text-slate">{check.hint}</span>
                </div>
                <div className="text-right text-sm font-light text-ash">
                  {formatCount(check.injected)}
                </div>
                <div className="text-right text-sm font-light text-ash">
                  {formatCount(check.detected)}
                </div>
                <div
                  className={`text-right text-xs font-light ${
                    check.agrees ? 'text-sage' : reconciling ? 'text-slate' : 'text-rose'
                  }`}
                >
                  {check.agrees
                    ? 'Match'
                    : reconciling
                      ? 'pending'
                      : `Off by ${check.detected - check.injected}`}
                </div>
              </div>
            ))}
          </PanelBody>
        </Panel>
      )}

      <Panel>
        <div className="flex flex-wrap items-center justify-between gap-5 border-b border-line px-6 py-5 sm:px-8">
          <h2 className="text-xl font-normal tracking-tight text-cream">Transactions</h2>
          <div className="flex flex-wrap items-center gap-3">
            {!live && (
              <Button size="sm" variant="outline" onClick={() => setFilter((f) => f)}>
                Auto-refresh paused
              </Button>
            )}
            <Segmented options={FILTERS} value={filter} onChange={setFilter} />
          </div>
        </div>
        <TransactionTable
          rows={rows}
          exceptionsByResultId={exceptionsByResultId}
          hasMore={cursor !== null}
          loadingMore={loadingMore}
          onLoadMore={() => void loadMore()}
          onResolved={() => void refreshExceptions()}
          emptyMessage={
            reconciling
              ? 'No results yet. The engine is still working through what was sent.'
              : 'No rows in this view.'
          }
        />
      </Panel>
    </div>
  )
}
