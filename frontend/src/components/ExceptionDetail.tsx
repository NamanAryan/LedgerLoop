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
  exact: 'Layer 1 · exact',
  time_drift: 'Layer 2 · time drift',
  amount_drift: 'Layer 3 · amount drift',
  duplicate: 'Layer 4 · duplicate',
  unmatched_sweep: 'Layer 5 · unmatched sweep',
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
      <div className="detail-side">
        <div className="detail-side-head">
          <span className="eyebrow">{title}</span>
          <span className="pill pill-unmatched">Absent</span>
        </div>
        <p className="detail-missing">
          No record on this side. The break is the absence itself — there is nothing to
          compare against.
        </p>
      </div>
    )
  }

  const differs = (key: 'amount' | 'currency' | 'occurredAt') =>
    counterpart !== null && facts[key] !== counterpart[key]

  return (
    <div className="detail-side">
      <div className="detail-side-head">
        <span className="eyebrow">{title}</span>
        <span className="tcell-dim" style={{ fontSize: 11 }}>
          row #{facts.rowId}
        </span>
      </div>
      <dl className="kv">
        <dt>txn_id</dt>
        <dd>{facts.txnId}</dd>

        <dt>amount</dt>
        <dd className={differs('amount') ? 'diff' : undefined}>
          {formatMoney(facts.amount, currency)}
        </dd>

        <dt>currency</dt>
        <dd className={differs('currency') ? 'diff' : undefined}>{facts.currency}</dd>

        <dt>timestamp</dt>
        <dd className={differs('occurredAt') ? 'diff' : undefined}>
          {formatTimestamp(facts.occurredAt)}
        </dd>

        <dt>idem_key</dt>
        <dd>{facts.idempotencyKey === '' ? '—' : facts.idempotencyKey}</dd>
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
    <div className="detail">
      <div className="detail-reason">
        <span className="eyebrow">Why it was flagged</span>
        <p>
          <strong className="mono">{LAYER_COPY[row.layer] ?? row.layer}</strong>
          {row.notes !== null && <> — {row.notes}</>}
        </p>
        {row.skewMs !== null && (
          <p>
            Ledger timestamp is {formatSkew(row.skewMs)} against the gateway.
          </p>
        )}
      </div>

      <div className="detail-sides">
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

      <div className="resolve">
        <div>
          <label className="eyebrow" htmlFor={`reason-${row.id}`}>
            Resolution
          </label>
          <select
            id={`reason-${row.id}`}
            className="select"
            style={{ marginTop: 6 }}
            value={resolution.reason}
            onChange={(event) => onChange({ ...resolution, reason: event.target.value })}
          >
            {REASONS.map((reason) => (
              <option key={reason} value={reason}>
                {reason}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="eyebrow" htmlFor={`note-${row.id}`}>
            Notes
          </label>
          <textarea
            id={`note-${row.id}`}
            style={{ marginTop: 6 }}
            placeholder="What you checked, what you found, what happens next."
            value={resolution.note}
            onChange={(event) => onChange({ ...resolution, note: event.target.value })}
          />
        </div>

        <div className="resolve-row">
          <button
            type="button"
            className={resolved ? 'btn btn-sm' : 'btn btn-sm btn-primary'}
            onClick={() =>
              onChange({ ...resolution, resolvedAt: resolved ? null : Date.now() })
            }
          >
            {resolved ? 'Reopen' : 'Mark resolved'}
          </button>
          {resolved && resolution.resolvedAt !== null && (
            <span className="resolved-flag">
              Resolved {formatTimestamp(resolution.resolvedAt)} UTC
            </span>
          )}
          {/* Said plainly rather than implied: this is a browser session, and
              nothing here is written anywhere durable. */}
          <span className="field-hint" style={{ margin: 0 }}>
            Notes live in this browser session only — nothing is uploaded.
          </span>
        </div>
      </div>
    </div>
  )
}
