/**
 * The transaction feed.
 *
 * Rows are rendered in pages against a stable sort rather than all at once. A 50,000
 * row result is roughly 350,000 DOM nodes if rendered whole, which is enough to make
 * the tab unresponsive for seconds — and no operator reads row 12,000 anyway. The
 * cursor is a position in the sorted result, and because the sort is total (time,
 * then txn_id) advancing it can never skip or repeat a row.
 */

import { Fragment, useEffect, useState } from 'react'
import { formatMoney } from '../engine/money'
import type { ReconRow } from '../engine/types'
import { formatClock, formatCount, formatSkew } from '../format'
import { ExceptionDetail, type Resolution } from './ExceptionDetail'
import { Button, Eyebrow, StatusPill } from './primitives'

const PAGE_SIZE = 100

const LAYER_SHORT: Record<string, string> = {
  exact: 'Exact',
  time_drift: 'Time drift',
  amount_drift: 'Amount drift',
  duplicate: 'Duplicate',
  unmatched_sweep: 'Sweep',
}

/** One grid definition, shared by the header and every row, so they cannot drift. */
const COLUMNS =
  'grid grid-cols-[7rem_minmax(11rem,1.3fr)_8rem_8rem_minmax(7rem,1fr)_minmax(9rem,1.1fr)_6.5rem] items-center gap-5 px-6 sm:px-8'

export function TransactionTable({
  rows,
  emptyMessage,
  resolutions,
  onResolutionChange,
  resetKey,
}: {
  rows: ReconRow[]
  emptyMessage: string
  resolutions: Record<number, Resolution>
  onResolutionChange: (id: number, next: Resolution) => void
  /** Changing this rewinds the cursor — a new filter starts at the top. */
  resetKey: string
}) {
  const [cursor, setCursor] = useState(PAGE_SIZE)
  const [expanded, setExpanded] = useState<number | null>(null)

  useEffect(() => {
    setCursor(PAGE_SIZE)
    setExpanded(null)
  }, [resetKey])

  if (rows.length === 0) {
    return (
      <p className="px-8 py-16 text-center text-sm font-light leading-relaxed text-slate">
        {emptyMessage}
      </p>
    )
  }

  const visible = rows.slice(0, cursor)

  return (
    <>
      <div className="overflow-x-auto">
        <div className="min-w-[62rem]">
          <div className={`${COLUMNS} border-b border-line py-3.5`} role="row">
            <Eyebrow>Status</Eyebrow>
            <Eyebrow>Transaction ID</Eyebrow>
            <Eyebrow>Gateway UTC</Eyebrow>
            <Eyebrow>Ledger UTC</Eyebrow>
            <Eyebrow className="block text-right">Amount</Eyebrow>
            <Eyebrow>Difference</Eyebrow>
            <Eyebrow>Layer</Eyebrow>
          </div>

          {visible.map((row) => {
            const isOpen = expanded === row.id
            const resolution = resolutions[row.id] ?? {
              reason: 'Unreviewed',
              note: '',
              resolvedAt: null,
            }
            const amount = row.gateway?.amount ?? row.ledger?.amount ?? 0

            return (
              <Fragment key={row.id}>
                <button
                  type="button"
                  aria-expanded={isOpen}
                  onClick={() => setExpanded(isOpen ? null : row.id)}
                  className={`${COLUMNS} w-full border-b border-line py-3.5 text-left text-sm font-light transition-colors duration-200 ease-refined hover:bg-ink-2 ${isOpen ? 'bg-ink-2' : ''}`}
                >
                  <span>
                    <StatusPill status={row.status} />
                  </span>
                  <span className="truncate text-cream">{row.txnId}</span>
                  <span className="text-slate">
                    {row.gateway ? formatClock(row.gateway.occurredAt) : '—'}
                  </span>
                  <span className="text-slate">
                    {row.ledger ? formatClock(row.ledger.occurredAt) : '—'}
                  </span>
                  <span className="text-right text-ash">
                    {formatMoney(amount, row.currency)}
                  </span>
                  <span
                    className={`truncate ${row.amountDelta ? 'text-rose' : 'text-slate'}`}
                  >
                    {renderDifference(row)}
                  </span>
                  <span className="text-slate">{LAYER_SHORT[row.layer] ?? row.layer}</span>
                </button>

                {isOpen && (
                  <ExceptionDetail
                    row={row}
                    resolution={resolution}
                    onChange={(next) => onResolutionChange(row.id, next)}
                  />
                )}
              </Fragment>
            )
          })}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-4 px-6 py-5 sm:px-8">
        <span className="text-xs font-light text-slate">
          Showing {formatCount(visible.length)} of {formatCount(rows.length)} rows
        </span>
        {cursor < rows.length && (
          <Button size="sm" onClick={() => setCursor((current) => current + PAGE_SIZE)}>
            Load {formatCount(Math.min(PAGE_SIZE, rows.length - cursor))} more
          </Button>
        )}
      </div>
    </>
  )
}

/**
 * The difference column carries the actual mismatch, not a restatement of the status.
 * A drift row shows both amounts so the size of the break is legible without opening
 * anything; a matched row that needed layer 2 shows the clock offset that made it so.
 */
function renderDifference(row: ReconRow) {
  if (row.status === 'amount_drift' && row.gateway && row.ledger) {
    return (
      <>
        {formatMoney(row.gateway.amount, row.currency)} ≠{' '}
        {formatMoney(row.ledger.amount, row.currency)}
      </>
    )
  }
  if (row.status === 'duplicate') return 'Repeat submission'
  if (row.status === 'unmatched_gateway_only') return 'No ledger entry'
  if (row.status === 'unmatched_ledger_only') return 'No gateway entry'
  if (row.layer === 'time_drift' && row.skewMs !== null) return formatSkew(row.skewMs)
  return row.skewMs !== null ? formatSkew(row.skewMs) : '—'
}
