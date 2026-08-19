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
import { StatusPill } from './primitives'

const PAGE_SIZE = 100

const LAYER_SHORT: Record<string, string> = {
  exact: 'L1 exact',
  time_drift: 'L2 time',
  amount_drift: 'L3 amount',
  duplicate: 'L4 dup',
  unmatched_sweep: 'L5 sweep',
}

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
    return <div className="empty">{emptyMessage}</div>
  }

  const visible = rows.slice(0, cursor)

  return (
    <>
      <div className="table-scroll">
        <div className="table">
          <div className="trow thead eyebrow" role="row">
            <span>Status</span>
            <span>Transaction ID</span>
            <span>Gateway (UTC)</span>
            <span>Ledger (UTC)</span>
            <span style={{ textAlign: 'right' }}>Amount</span>
            <span>Difference</span>
            <span>Layer</span>
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
                  className="trow trow-btn"
                  aria-expanded={isOpen}
                  onClick={() => setExpanded(isOpen ? null : row.id)}
                >
                  <span>
                    <StatusPill status={row.status} />
                  </span>
                  <span className="tcell-id">{row.txnId}</span>
                  <span className="tcell-dim">
                    {row.gateway ? formatClock(row.gateway.occurredAt) : '—'}
                  </span>
                  <span className="tcell-dim">
                    {row.ledger ? formatClock(row.ledger.occurredAt) : '—'}
                  </span>
                  <span className="tcell-num">{formatMoney(amount, row.currency)}</span>
                  <span className={row.amountDelta ? 'tcell-delta' : 'tcell-dim'}>
                    {renderDifference(row)}
                  </span>
                  <span className="tcell-dim">{LAYER_SHORT[row.layer] ?? row.layer}</span>
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

      <div className="load-more">
        <span className="field-hint" style={{ margin: 0 }}>
          Showing {formatCount(visible.length)} of {formatCount(rows.length)} rows
        </span>
        {cursor < rows.length && (
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setCursor((current) => current + PAGE_SIZE)}
          >
            Load {formatCount(Math.min(PAGE_SIZE, rows.length - cursor))} more
          </button>
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
  if (row.status === 'duplicate') return 'repeat submission'
  if (row.status === 'unmatched_gateway_only') return 'no ledger entry'
  if (row.status === 'unmatched_ledger_only') return 'no gateway entry'
  if (row.layer === 'time_drift' && row.skewMs !== null) return formatSkew(row.skewMs)
  return row.skewMs !== null ? formatSkew(row.skewMs) : '—'
}
