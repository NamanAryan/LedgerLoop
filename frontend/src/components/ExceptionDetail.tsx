/**
 * The inline exception pane.
 *
 * Expands in place rather than opening a modal: an operator working a queue needs to
 * keep the surrounding rows visible, because the row above is very often the context
 * that explains the row in front of them. A modal throws that away every time.
 *
 * Resolution posts to `POST /v1/exceptions/{id}/resolve` and is the only non-idempotent
 * write in the API. The backend closes it with a compare-and-set on `closed_at IS NULL`,
 * so two operators resolving the same break cannot both win — the loser gets a 409 and
 * is told, rather than silently overwriting the first person's note. That 409 is
 * surfaced here as a real message, because "someone else already resolved this" is
 * information, not an error to swallow.
 *
 * Note what is *not* here any more: a local resolved/reopen toggle. Resolution state
 * lives in the backend now. There is no reopen, because the API offers none.
 */

import { useState } from 'react'
import { ApiError, resolveException } from '../api/client'
import type { ExceptionOut, ReconciliationResultOut } from '../api/types'
import { formatAmount } from '../lib/money'
import { formatSkew, formatTimestamp } from '../format'
import { Button, Eyebrow, Select } from './primitives'

const REASONS = [
  'FX or rounding difference — accept',
  'Ledger post pending — recheck next cycle',
  'Gateway retry — suppress',
  'Chargeback or reversal',
  'Escalate to payments engineering',
] as const

const LAYER_COPY: Record<string, string> = {
  exact: 'Layer 1 · Exact',
  time_drift: 'Layer 2 · Time drift',
  amount_drift: 'Layer 3 · Amount drift',
  duplicate: 'Layer 4 · Duplicate',
  unmatched_sweep: 'Layer 5 · Unmatched sweep',
}

/** Everything the pane needs, from either the feed or the exception queue. */
export interface DetailRow {
  id: number
  status: string
  match_layer: string
  notes: string | null
  txn_id: string | null
  currency: string | null
  gateway_txn_id: number | null
  ledger_entry_id: number | null
  gateway_amount: string | null
  ledger_amount: string | null
  gateway_occurred_at: string | null
  ledger_occurred_at: string | null
}

export function toDetailRow(row: ReconciliationResultOut | ExceptionOut): DetailRow {
  return {
    id: row.id,
    status: row.status,
    match_layer: row.match_layer,
    notes: row.notes,
    txn_id: row.txn_id,
    currency: row.currency,
    gateway_txn_id: row.gateway_txn_id,
    ledger_entry_id: row.ledger_entry_id,
    gateway_amount: row.gateway_amount,
    ledger_amount: row.ledger_amount,
    gateway_occurred_at: row.gateway_occurred_at,
    ledger_occurred_at: row.ledger_occurred_at,
  }
}

/** Signed skew, ledger minus gateway, in ms. Null unless both sides exist. */
export function skewMs(row: DetailRow): number | null {
  if (!row.gateway_occurred_at || !row.ledger_occurred_at) return null
  return Date.parse(row.ledger_occurred_at) - Date.parse(row.gateway_occurred_at)
}

function SideRecord({
  title,
  rowId,
  amount,
  occurredAt,
  currency,
  amountDiffers,
  timeDiffers,
}: {
  title: string
  rowId: number | null
  amount: string | null
  occurredAt: string | null
  currency: string | null
  amountDiffers: boolean
  timeDiffers: boolean
}) {
  if (rowId === null) {
    return (
      <div className="rounded-lg border border-line p-6">
        <div className="mb-4 flex items-center justify-between border-b border-line pb-3">
          <Eyebrow>{title}</Eyebrow>
          <span className="inline-flex rounded-full border border-rose/30 bg-rose/10 px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em] text-rose">
            Absent
          </span>
        </div>
        <p className="text-sm font-light text-slate">No record on this side.</p>
      </div>
    )
  }

  /* A differing field is marked on the value itself. Highlighting the whole row would
     make the operator hunt for the token that actually changed. */
  const mark = (changed: boolean) =>
    changed ? 'rounded-sm bg-rose/12 px-1.5 py-0.5 -mx-1.5 text-rose' : 'text-cream'

  return (
    <div className="rounded-lg border border-line p-6">
      <div className="mb-4 flex items-center justify-between border-b border-line pb-3">
        <Eyebrow>{title}</Eyebrow>
        <span className="text-xs font-light text-slate">Row #{rowId}</span>
      </div>
      <dl className="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-3 text-sm font-light">
        <dt className="text-slate">Amount</dt>
        <dd className="m-0">
          <span className={mark(amountDiffers)}>{formatAmount(amount, currency)}</span>
        </dd>

        <dt className="text-slate">Currency</dt>
        <dd className="m-0 text-cream">{currency ?? '—'}</dd>

        <dt className="text-slate">Timestamp</dt>
        <dd className="m-0">
          <span className={mark(timeDiffers)}>
            {occurredAt === null ? '—' : formatTimestamp(Date.parse(occurredAt))}
          </span>
        </dd>
      </dl>
    </div>
  )
}

export function ExceptionDetail({
  row,
  exceptionId,
  closedAt,
  resolutionNotes,
  onResolved,
}: {
  row: DetailRow
  /** The exceptions-queue id, when this row has one. Null means nothing to resolve. */
  exceptionId: number | null
  closedAt: string | null
  resolutionNotes: string | null
  onResolved: () => void
}) {
  const [reason, setReason] = useState<string>(REASONS[0])
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const skew = skewMs(row)
  const resolved = closedAt !== null
  const amountsDiffer =
    row.gateway_amount !== null &&
    row.ledger_amount !== null &&
    row.gateway_amount !== row.ledger_amount
  const timesDiffer = skew !== null && skew !== 0

  const submit = async () => {
    if (exceptionId === null) return
    setSaving(true)
    setError(null)
    try {
      // The reason is the structured half and the note is the free half; the API takes
      // one string, so they are joined rather than one being dropped.
      const body = note.trim() === '' ? reason : `${reason} — ${note.trim()}`
      await resolveException(exceptionId, body)
      onResolved()
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 409
          ? 'Someone else resolved this exception first. Refreshing will show their note.'
          : caught instanceof Error
            ? caught.message
            : 'Could not resolve this exception.',
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-8 border-b border-line bg-ink-2 px-6 py-8 sm:px-8">
      <div className="border-l-2 border-gold/70 pl-5">
        <Eyebrow>Why it was flagged</Eyebrow>
        <p className="mt-2 text-sm font-light leading-relaxed text-ash">
          <span className="text-cream">{LAYER_COPY[row.match_layer] ?? row.match_layer}</span>
          {row.notes !== null && <> — {row.notes}</>}
        </p>
        {skew !== null && (
          <p className="mt-1 text-sm font-light leading-relaxed text-ash">
            Ledger timestamp is {formatSkew(skew)} against the gateway.
          </p>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <SideRecord
          title="Gateway"
          rowId={row.gateway_txn_id}
          amount={row.gateway_amount}
          occurredAt={row.gateway_occurred_at}
          currency={row.currency}
          amountDiffers={amountsDiffer}
          timeDiffers={timesDiffer}
        />
        <SideRecord
          title="Ledger"
          rowId={row.ledger_entry_id}
          amount={row.ledger_amount}
          occurredAt={row.ledger_occurred_at}
          currency={row.currency}
          amountDiffers={amountsDiffer}
          timeDiffers={timesDiffer}
        />
      </div>

      {exceptionId === null ? (
        <p className="text-sm font-light text-slate">
          This row reconciled without needing review, so there is no exception to resolve.
        </p>
      ) : resolved ? (
        <div className="border-l-2 border-sage/60 pl-5">
          <Eyebrow>Resolved</Eyebrow>
          <p className="mt-2 text-sm font-light leading-relaxed text-ash">
            {resolutionNotes ?? 'No note recorded.'}
          </p>
          <p className="mt-1 text-xs font-light text-slate">
            Closed {formatTimestamp(Date.parse(closedAt))} UTC
          </p>
        </div>
      ) : (
        <>
          <div className="grid gap-6 lg:grid-cols-2">
            <div>
              <label htmlFor={`reason-${row.id}`}>
                <Eyebrow>Resolution</Eyebrow>
              </label>
              <Select
                id={`reason-${row.id}`}
                className="mt-2.5"
                value={reason}
                disabled={saving}
                onChange={setReason}
              >
                {REASONS.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </Select>
            </div>

            <div>
              <label htmlFor={`note-${row.id}`}>
                <Eyebrow>Notes</Eyebrow>
              </label>
              <textarea
                id={`note-${row.id}`}
                placeholder="What you checked, what you found, what happens next."
                value={note}
                disabled={saving}
                onChange={(event) => setNote(event.target.value)}
                className="mt-2.5 min-h-24 w-full resize-y rounded-md border border-line-2 bg-ink px-3.5 py-2.5 text-sm font-light leading-relaxed text-cream transition-colors duration-300 ease-refined placeholder:text-slate focus:border-gold/70 focus:outline-none"
              />
            </div>
          </div>

          {error !== null && <p className="text-sm font-light text-rose">{error}</p>}

          <Button size="sm" variant="primary" onClick={() => void submit()} disabled={saving}>
            {saving ? 'Resolving…' : 'Mark resolved'}
          </Button>
        </>
      )}
    </div>
  )
}
