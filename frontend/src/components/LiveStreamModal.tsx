/**
 * "Set Up Live Stream" — the endpoint URL, per-provider instructions, and a test send.
 *
 * There is no signing-secret field, and its absence is a decision rather than an
 * omission. The secret is held server-side per source and never leaves the backend;
 * a field here would say the operator sets it from the dashboard, which is false for
 * Stripe and Razorpay — those issue their own secret, and verification uses their bytes
 * or fails. Where that leaves a dashboard-created source is stated in the copy below
 * rather than papered over.
 */

import { useState, type ReactNode } from 'react'
import { webhookUrl } from '../api/client'
import { CopyButton } from './CopyButton'
import { Modal } from './Modal'
import { Button } from './primitives'
import type { WebhookProvider, WebhookSource, WebhookTestResult } from '../api/types'

const PROVIDERS: { value: WebhookProvider; label: string }[] = [
  { value: 'stripe', label: 'Stripe' },
  { value: 'razorpay', label: 'Razorpay' },
  { value: 'custom', label: 'Custom Backend' },
]

interface Guidance {
  header: string
  signed: string
  event: string
  /** The one thing that will actually go wrong for this provider. */
  caveat: string
}

const GUIDANCE: Record<WebhookProvider, Guidance> = {
  stripe: {
    header: 'Stripe-Signature',
    signed: '{timestamp}.{raw body}, HMAC-SHA256, rejected outside ±5 minutes',
    event: 'payment_intent.succeeded',
    caveat:
      'Stripe issues its own signing secret and it cannot be set from here. Point this source at Stripe with the CLI (scripts/create_webhook_source.py --signing-secret whsec_…) or every delivery will be refused.',
  },
  razorpay: {
    header: 'X-Razorpay-Signature',
    signed: 'raw body, HMAC-SHA256, no timestamp',
    event: 'payment.captured',
    caveat:
      'Razorpay issues its own webhook secret and it cannot be set from here. Register the source with the CLI, passing that secret, before pointing Razorpay at this URL.',
  },
  custom: {
    header: 'X-LedgerLoop-Signature',
    signed: 'raw body, HMAC-SHA256, hex digest',
    event: "LedgerLoop's own payload shape — txn_id, amount, currency, occurred_at, gateway_ref",
    caveat:
      'The secret for this source was generated server-side and is never returned. Retrieve it from the database, or create the source with the CLI so you hold the secret you sign with.',
  },
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="flex gap-4">
      <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border border-line-2 font-mono text-[11px] text-slate">
        {n}
      </span>
      <div className="min-w-0 space-y-1">
        <p className="text-sm font-medium text-cream">{title}</p>
        <div className="text-xs font-light leading-relaxed text-ash">{children}</div>
      </div>
    </li>
  )
}

export function LiveStreamModal({
  open,
  onClose,
  source,
  testing,
  testResult,
  onTest,
}: {
  open: boolean
  onClose: () => void
  source: WebhookSource
  testing: boolean
  testResult: WebhookTestResult | null
  onTest: () => void
}) {
  const [provider, setProvider] = useState<WebhookProvider>(source.provider)
  const guidance = GUIDANCE[provider]
  const url = webhookUrl(source.source_token)

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Set Up Live Stream"
      description="Point a payment provider at this endpoint. Deliveries are signature-verified, mapped to the internal transaction shape, and reconciled against your ledger."
      footer={
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="min-w-0 text-xs leading-relaxed">
            {testResult === null ? (
              <span className="text-slate">
                Signs a synthetic event server-side and runs the real verify → ingest path.
              </span>
            ) : testResult.delivered ? (
              <span className="text-sage">
                {testResult.duplicate
                  ? // Reported plainly rather than as a failure: a repeat event id
                    // collapsing is the idempotency layer doing its job, and calling
                    // it an error teaches distrust of the guarantee.
                    `HTTP ${testResult.http_status} · already seen, deduplicated — idempotency working`
                  : `HTTP ${testResult.http_status} · accepted as ${testResult.txn_id ?? 'a transaction'}`}
              </span>
            ) : (
              <span className="text-rose">
                {testResult.http_status > 0
                  ? `HTTP ${testResult.http_status} · ${testResult.detail ?? 'rejected'}`
                  : `No response · ${testResult.detail ?? 'network, CORS, or a cold start'}`}
              </span>
            )}
          </div>
          <Button variant="primary" onClick={onTest} disabled={testing}>
            {testing ? 'Sending…' : 'Send Test Payload'}
          </Button>
        </div>
      }
    >
      <div className="space-y-7">
        <div className="space-y-2">
          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-slate">
            Endpoint URL
          </p>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 overflow-x-auto rounded-md border border-line bg-ink px-3 py-2.5 font-mono text-[11px] text-cream">
              {url}
            </code>
            <CopyButton value={url} label="Copy Endpoint" />
          </div>
        </div>

        <div className="space-y-4">
          <div
            role="tablist"
            aria-label="Provider"
            className="inline-flex rounded-md border border-line p-1"
          >
            {PROVIDERS.map((option) => (
              <button
                key={option.value}
                type="button"
                role="tab"
                aria-selected={provider === option.value}
                onClick={() => setProvider(option.value)}
                className={`rounded px-4 py-1.5 text-xs font-medium tracking-wide transition-colors duration-300 ease-refined ${
                  provider === option.value
                    ? 'bg-gold/10 text-gold'
                    : 'text-slate hover:text-cream'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>

          {provider !== source.provider && (
            <p className="text-xs leading-relaxed text-gold">
              This source is registered as <strong>{source.provider}</strong>. The provider decides
              both the signature scheme and the payload adapter, so deliveries shaped for{' '}
              {guidance.header} will be refused unless the source is registered for it.
            </p>
          )}

          <ol className="space-y-5">
            <Step n={1} title="Add the endpoint at your provider">
              Paste the URL above into the provider&rsquo;s webhook settings and subscribe to{' '}
              <code className="font-mono text-cream">{guidance.event}</code>.
            </Step>
            <Step n={2} title="Sign every request">
              Header <code className="font-mono text-cream">{guidance.header}</code>, over{' '}
              {guidance.signed}. The signature covers the <em>raw bytes</em> — re-serialising the
              JSON changes the digest and every delivery fails as if the secret were wrong.
            </Step>
            <Step n={3} title="Match the secret">
              {guidance.caveat}
            </Step>
            <Step n={4} title="Expect 202, including on retries">
              A redelivery answers <code className="font-mono text-cream">202</code> with{' '}
              <code className="font-mono text-cream">duplicate: true</code>, never{' '}
              <code className="font-mono text-cream">409</code>. Retry on 5xx and network failures;
              do not retry a 4xx.
            </Step>
            <Step n={5} title="Send your ledger side">
              Reconciliation needs both halves. Post your own records to{' '}
              <code className="font-mono text-cream">POST /v1/ledger/sync</code> (up to 1000 entries
              per request) with the same <code className="font-mono text-cream">txn_id</code> the
              gateway reports, and the matcher pairs them.
            </Step>
          </ol>
        </div>
      </div>
    </Modal>
  )
}
