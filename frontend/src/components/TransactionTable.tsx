/**
 * The transaction feed.
 *
 * Pagination is the API's, not this component's. The backend pages by keyset — `WHERE
 * id < :cursor ORDER BY id DESC LIMIT :n` — so page 1000 costs exactly what page 1
 * costs, and "load more" appends the next page rather than re-fetching a larger slice.
 * The cursor is opaque here: it is handed back exactly as received and never parsed,
 * because the moment a client starts doing arithmetic on a cursor the server can no
 * longer change what one means.
 *
 * The old version sliced a locally-computed array. There is no local array now — rows
 * exist only as far as they have been fetched, which is also why the footer says how
 * many are loaded rather than how many exist. Claiming a total the server never sent
 * would be a guess.
 */

import { Fragment, useState } from 'react'
import type { ExceptionOut, ReconciliationResultOut } from '../api/types'
import { formatAmount } from '../lib/money'
import { formatClock, formatCount, formatSkew } from '../format'
import { ExceptionDetail, skewMs, toDetailRow } from './ExceptionDetail'
import { Button, Eyebrow, StatusPill } from './primitives'

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
  exceptionsByResultId,
  emptyMessage,
  hasMore,
  loadingMore,
  onLoadMore,
  onResolved,
}: {
  rows: ReconciliationResultOut[]
  /** Open/closed exception state, keyed by reconciliation_result_id. */
  exceptionsByResultId: Map<number, ExceptionOut>
  emptyMessage: string
  hasMore: boolean
  loadingMore: boolean
  onLoadMore: () => void
  onResolved: () => void
}) {
  const [expanded, setExpanded] = useState<number | null>(null)

  if (rows.length === 0) {
    return (
      <p className="px-8 py-16 text-center text-sm font-light leading-relaxed text-slate">
        {emptyMessage}
      </p>
    )
  }

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

          {rows.map((row) => {
            const isOpen = expanded === row.id
            const detail = toDetailRow(row)
            const exception = exceptionsByResultId.get(row.id) ?? null
            // Either side's amount, for the headline column. They agree except on a
            // drift row, which is exactly what the Difference column is for.
            const amount = row.gateway_amount ?? row.ledger_amount

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
                  <span className="truncate text-cream">{row.txn_id ?? '—'}</span>
                  <span className="text-slate">
                    {row.gateway_occurred_at
                      ? formatClock(Date.parse(row.gateway_occurred_at))
                      : '—'}
                  </span>
                  <span className="text-slate">
                    {row.ledger_occurred_at ? formatClock(Date.parse(row.ledger_occurred_at)) : '—'}
                  </span>
                  <span className="text-right text-ash">
                    {formatAmount(amount, row.currency)}
                  </span>
                  <span
                    className={`truncate ${row.status === 'amount_drift' ? 'text-rose' : 'text-slate'}`}
                  >
                    {renderDifference(row)}
                  </span>
                  <span className="text-slate">
                    {LAYER_SHORT[row.match_layer] ?? row.match_layer}
                  </span>
                </button>

                {isOpen && (
                  <ExceptionDetail
                    row={detail}
                    exceptionId={exception?.id ?? null}
                    closedAt={exception?.closed_at ?? null}
                    resolutionNotes={exception?.resolution_notes ?? null}
                    onResolved={onResolved}
                  />
                )}
              </Fragment>
            )
          })}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-4 px-6 py-5 sm:px-8">
        <span className="text-xs font-light text-slate">
          {formatCount(rows.length)} rows loaded
          {hasMore ? '' : ' · end of feed'}
        </span>
        {hasMore && (
          <Button size="sm" onClick={onLoadMore} disabled={loadingMore}>
            {loadingMore ? 'Loading…' : 'Load more'}
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
function renderDifference(row: ReconciliationResultOut) {
  if (row.status === 'amount_drift' && row.gateway_amount && row.ledger_amount) {
    return (
      <>
        {formatAmount(row.gateway_amount, row.currency)} ≠{' '}
        {formatAmount(row.ledger_amount, row.currency)}
      </>
    )
  }
  if (row.status === 'duplicate') return 'Repeat submission'
  if (row.status === 'unmatched_gateway_only') return 'No ledger entry'
  if (row.status === 'unmatched_ledger_only') return 'No gateway entry'
  const skew = skewMs(toDetailRow(row))
  return skew !== null ? formatSkew(skew) : '—'
}
