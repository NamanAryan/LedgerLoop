/**
 * "Live Webhook Stream" — the third card in the Reconcile grid.
 *
 * Five states, and the distinction between the last two is the reason this card is
 * worth building carefully:
 *
 * - **locked** (sandbox) — a live endpoint needs an account key.
 * - **none** — keyed, no endpoint yet.
 * - **awaiting** — endpoint exists, nothing has ever arrived.
 * - **live / idle** — events arriving, or nothing for fifteen minutes.
 * - **rejecting** — events arriving and being refused.
 *
 * `rejecting` is the common real failure: a signing secret that does not match, so
 * every delivery 401s. It leaves the transaction tables as empty as having received
 * nothing at all, which is precisely why the two must not look the same. And because a
 * rejected delivery still updates `last_event_at`, a card keyed on recency alone would
 * paint that failure green.
 */

import { useEffect, useState, type ReactNode } from 'react'
import { webhookUrl } from '../api/client'
import { healthOf, useWebhookSource, type SourceHealth } from '../hooks/useWebhookSource'
import { useTenantContext } from '../tenant/TenantContext'
import { CopyButton } from './CopyButton'
import { LiveStreamModal } from './LiveStreamModal'
import type { WebhookDeliveryStatus } from '../api/types'

const DELIVERY_LABEL: Record<WebhookDeliveryStatus, string> = {
  ok: 'Delivered',
  // Named as the cause, not the symptom. "invalid_signature" is a field value; "the
  // signing secret does not match" is the thing to go and fix.
  invalid_signature: 'Signature rejected — the signing secret does not match',
  invalid_payload: 'Payload rejected — event type or shape not handled',
}

/** Relative time, ticking. Computed client-side from the timestamp so a card left open
 *  keeps counting instead of freezing at whatever the last poll said. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [active])
  return now
}

function ago(iso: string, now: number): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m ago`
  return `${Math.floor(hours / 24)}d ago`
}

const DOT: Record<SourceHealth, string> = {
  none: 'bg-slate',
  awaiting: 'bg-gold',
  live: 'bg-sage',
  idle: 'bg-slate',
  rejecting: 'bg-rose',
}

const TEXT: Record<SourceHealth, string> = {
  none: 'text-slate',
  awaiting: 'text-gold',
  live: 'text-sage',
  idle: 'text-slate',
  rejecting: 'text-rose',
}

function StatusLine({ health, children }: { health: SourceHealth; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.18em] ${TEXT[health]}`}
    >
      <span
        aria-hidden="true"
        className={`size-1.5 rounded-full ${DOT[health]} ${health === 'live' ? 'animate-pulse' : ''}`}
      />
      {children}
    </span>
  )
}

export function WebhookCard({ onOpenKeyEntry }: { onOpenKeyEntry: () => void }) {
  const { mode } = useTenantContext()
  const { source, error, creating, testing, testResult, create, test } = useWebhookSource()
  const [setupOpen, setSetupOpen] = useState(false)

  // Only tick while there is a timestamp on screen to keep current.
  const now = useNow(mode === 'account' && source?.last_event_at != null)
  const health = healthOf(source, now)

  const shell =
    'group flex min-h-48 flex-col justify-between rounded-lg border p-6 transition-colors duration-300 ease-refined sm:p-10'

  /* --- sandbox: locked -------------------------------------------------- */
  if (mode === 'sandbox') {
    return (
      <div className={`${shell} border-line`}>
        <div>
          <div className="flex items-center gap-3">
            <h2 className="font-display text-2xl font-normal tracking-tight text-cream sm:text-3xl">
              Live Webhook Stream
            </h2>
            <span aria-hidden="true" className="text-slate">
              &#128274;
            </span>
          </div>
          <p className="mt-3 max-w-sm text-sm font-light leading-[1.7] text-ash sm:mt-4 sm:leading-[1.8]">
            Receive signed events straight from Stripe, Razorpay, or your own backend and
            reconcile them as they arrive. Requires an account key.
          </p>
        </div>
        <div className="mt-6 border-t border-line pt-4 sm:mt-8 sm:pt-5">
          {/* No demo-tenant endpoint is offered here on purpose: a sandbox tenant is
              deleted 24h after it goes idle, so the URL would be pasted into a
              gateway's dashboard and then start 404ing with nothing to explain it.
              Offering nothing is better than offering something with a hidden expiry. */}
          <p className="text-[11px] font-light text-slate">
            Sandbox tenants expire after 24h · no live endpoint
          </p>
          <button
            type="button"
            onClick={onOpenKeyEntry}
            className="mt-3 inline-flex items-center gap-2 text-xs font-medium uppercase tracking-[0.18em] text-gold transition-opacity duration-300 ease-refined hover:opacity-75"
          >
            Use an account key
            <span aria-hidden="true" className="transition-transform duration-300 ease-refined group-hover:translate-x-1.5">
              &rarr;
            </span>
          </button>
        </div>
      </div>
    )
  }

  /* --- account ---------------------------------------------------------- */
  const border = health === 'rejecting' ? 'border-rose/40' : health === 'live' ? 'border-sage/30' : 'border-line'

  return (
    <>
      <div className={`${shell} ${border}`}>
        <div>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="font-display text-2xl font-normal tracking-tight text-cream sm:text-3xl">
              Live Webhook Stream
            </h2>
            {source !== null && (
              <StatusLine health={health}>
                {health === 'live' && 'Live'}
                {health === 'idle' && 'Idle'}
                {health === 'awaiting' && 'Awaiting first event'}
                {health === 'rejecting' && 'Rejecting events'}
              </StatusLine>
            )}
          </div>

          {source === null ? (
            <p className="mt-3 max-w-sm text-sm font-light leading-[1.7] text-ash sm:mt-4 sm:leading-[1.8]">
              Create an endpoint and point your gateway at it. Deliveries are signature-verified
              and reconciled against your ledger as they arrive.
            </p>
          ) : (
            <div className="mt-4 space-y-3">
              {health === 'rejecting' && source.last_delivery_status !== null && (
                <p className="text-sm font-light leading-relaxed text-rose">
                  {DELIVERY_LABEL[source.last_delivery_status]}
                </p>
              )}
              {health === 'live' && source.last_event_at !== null && (
                <p className="text-sm font-light text-ash">
                  Last event <span className="text-cream">{ago(source.last_event_at, now)}</span>
                </p>
              )}
              {health === 'idle' && source.last_event_at !== null && (
                <p className="text-sm font-light leading-relaxed text-ash">
                  Nothing for <span className="text-cream">{ago(source.last_event_at, now)}</span>.
                  The endpoint is live but quiet.
                </p>
              )}
              {health === 'awaiting' && (
                <p className="text-sm font-light leading-relaxed text-ash">
                  The endpoint is ready. Nothing has arrived yet.
                </p>
              )}

              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded-md border border-line bg-ink px-3 py-2 font-mono text-[11px] text-slate">
                  {webhookUrl(source.source_token)}
                </code>
                <CopyButton value={webhookUrl(source.source_token)} />
              </div>
            </div>
          )}

          {error !== null && (
            <p role="alert" className="mt-3 text-xs leading-relaxed text-rose">
              {error}
            </p>
          )}
          {testResult !== null && (
            <p
              role="status"
              className={`mt-3 text-xs leading-relaxed ${testResult.delivered ? 'text-sage' : 'text-rose'}`}
            >
              {testResult.delivered
                ? testResult.duplicate
                  ? `Delivered · ${testResult.http_status} · already seen, deduplicated`
                  : `Delivered · ${testResult.http_status} · ${testResult.txn_id ?? 'accepted'}`
                : `Failed${testResult.http_status > 0 ? ` · ${testResult.http_status}` : ''} · ${testResult.detail ?? 'unknown error'}`}
            </p>
          )}
        </div>

        <div className="mt-6 border-t border-line pt-4 sm:mt-8 sm:pt-5">
          {source === null ? (
            <button
              type="button"
              onClick={() => void create('custom')}
              disabled={creating}
              className="inline-flex items-center gap-2 text-xs font-medium uppercase tracking-[0.18em] text-gold transition-opacity duration-300 ease-refined hover:opacity-75 disabled:opacity-50"
            >
              {creating ? 'Creating…' : 'Create endpoint'}
              <span aria-hidden="true" className="transition-transform duration-300 ease-refined group-hover:translate-x-1.5">
                &rarr;
              </span>
            </button>
          ) : (
            <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
              <button
                type="button"
                onClick={() => void test()}
                disabled={testing}
                className="inline-flex items-center gap-2 text-xs font-medium uppercase tracking-[0.18em] text-gold transition-opacity duration-300 ease-refined hover:opacity-75 disabled:opacity-50"
              >
                {testing ? 'Sending…' : 'Send test payload'}
                <span aria-hidden="true">&rarr;</span>
              </button>
              <button
                type="button"
                onClick={() => setSetupOpen(true)}
                className="text-xs font-medium uppercase tracking-[0.18em] text-slate transition-colors duration-300 ease-refined hover:text-cream"
              >
                Setup guide
              </button>
            </div>
          )}
        </div>
      </div>

      {source !== null && (
        <LiveStreamModal
          open={setupOpen}
          onClose={() => setSetupOpen(false)}
          source={source}
          testing={testing}
          testResult={testResult}
          onTest={() => void test()}
        />
      )}
    </>
  )
}
