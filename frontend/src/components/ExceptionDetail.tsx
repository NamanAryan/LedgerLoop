/**
 * The inline exception pane.
 *
 * Expands in place rather than opening a modal: an operator working a queue needs to
 * keep the surrounding rows visible, because the row above is very often the context
 * that explains the row in front of them. A modal throws that away every time.
 */

import { formatMoney } from '../engine/money'
import type { ReconRow, TxnFacts } from '../engine/types'
import { formatSkew, formatTimestamp } from '../format'
import { Button, Eyebrow, Select } from './primitives'

export interface Resolution {
  reason: string
  note: string
  resolvedAt: number | null
}

const REASONS = [
  'Unreviewed',
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

function SideRecord({
  title,
  facts,
  counterpart,
  currency,
}: {
  title: string
  facts: TxnFacts | null
  counterpart: TxnFacts | null
  currency: string
}) {
  if (facts === null) {
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

  const differs = (key: 'amount' | 'currency' | 'occurredAt') =>
    counterpart !== null && facts[key] !== counterpart[key]

  /* A differing field is marked on the value itself. Highlighting the whole row
     would make the operator hunt for the token that actually changed. */
  const value = (changed: boolean) =>
    changed
      ? 'rounded-sm bg-rose/12 px-1.5 py-0.5 -mx-1.5 text-rose'
      : 'text-cream'

  return (
    <div className="rounded-lg border border-line p-6">
      <div className="mb-4 flex items-center justify-between border-b border-line pb-3">
        <Eyebrow>{title}</Eyebrow>
        <span className="text-xs font-light text-slate">Row #{facts.rowId}</span>
      </div>
      <dl className="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-3 text-sm font-light">
        <dt className="text-slate">Transaction</dt>
        <dd className="m-0 break-words text-cream">{facts.txnId}</dd>

        <dt className="text-slate">Amount</dt>
        <dd className="m-0">
          <span className={value(differs('amount'))}>
            {formatMoney(facts.amount, currency)}
          </span>
        </dd>

        <dt className="text-slate">Currency</dt>
        <dd className="m-0">
          <span className={value(differs('currency'))}>{facts.currency}</span>
        </dd>

        <dt className="text-slate">Timestamp</dt>
        <dd className="m-0">
          <span className={value(differs('occurredAt'))}>
            {formatTimestamp(facts.occurredAt)}
          </span>
        </dd>

        <dt className="text-slate">Idempotency</dt>
        <dd className="m-0 break-words text-cream">
          {facts.idempotencyKey === '' ? '—' : facts.idempotencyKey}
        </dd>
      </dl>
    </div>
  )
}

export function ExceptionDetail({
  row,
  resolution,
  onChange,
}: {
  row: ReconRow
  resolution: Resolution
  onChange: (next: Resolution) => void
}) {
  const resolved = resolution.resolvedAt !== null

  return (
    <div className="space-y-8 border-b border-line bg-ink-2 px-6 py-8 sm:px-8">
      <div className="border-l-2 border-gold/70 pl-5">
        <Eyebrow>Why it was flagged</Eyebrow>
        <p className="mt-2 text-sm font-light leading-relaxed text-ash">
          <span className="text-cream">{LAYER_COPY[row.layer] ?? row.layer}</span>
          {row.notes !== null && <> — {row.notes}</>}
        </p>
        {row.skewMs !== null && (
          <p className="mt-1 text-sm font-light leading-relaxed text-ash">
            Ledger timestamp is {formatSkew(row.skewMs)} against the gateway.
          </p>
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <SideRecord
          title="Gateway"
          facts={row.gateway}
          counterpart={row.ledger}
          currency={row.currency}
        />
        <SideRecord
          title="Ledger"
          facts={row.ledger}
          counterpart={row.gateway}
          currency={row.currency}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div>
          <label htmlFor={`reason-${row.id}`}>
            <Eyebrow>Resolution</Eyebrow>
          </label>
          <Select
            id={`reason-${row.id}`}
            className="mt-2.5"
            value={resolution.reason}
            onChange={(reason) => onChange({ ...resolution, reason })}
          >
            {REASONS.map((reason) => (
              <option key={reason} value={reason}>
                {reason}
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
            value={resolution.note}
            onChange={(event) => onChange({ ...resolution, note: event.target.value })}
            className="mt-2.5 min-h-24 w-full resize-y rounded-md border border-line-2 bg-ink px-3.5 py-2.5 text-sm font-light leading-relaxed text-cream transition-colors duration-300 ease-refined placeholder:text-slate focus:border-gold/70 focus:outline-none"
          />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-4">
        <Button
          size="sm"
          variant={resolved ? 'outline' : 'primary'}
          onClick={() => onChange({ ...resolution, resolvedAt: resolved ? null : Date.now() })}
        >
          {resolved ? 'Reopen' : 'Mark resolved'}
        </Button>
        {resolved && resolution.resolvedAt !== null && (
          <span className="text-xs font-light text-sage">
            Resolved {formatTimestamp(resolution.resolvedAt)} UTC
          </span>
        )}
      </div>
    </div>
  )
}
