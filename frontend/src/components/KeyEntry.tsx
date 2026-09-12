/**
 * Key entry, and the persistent mode indicator that sits beside it in the header.
 *
 * The key is validated before it is stored, by making the cheapest authenticated call
 * on the API with it. Storing first and discovering the problem later would put the
 * dashboard into account mode against a key that does not work, and every screen would
 * fail at once with nothing pointing back at the key as the cause.
 */

import { useCallback, useState } from 'react'
import { getWebhookSources } from '../api/client'
import { ApiError } from '../api/client'
import { useTenantContext } from '../tenant/TenantContext'
import { Button } from './primitives'
import { Modal } from './Modal'

/**
 * Which tenant is on screen, stated permanently rather than on hover.
 *
 * Sandbox and account render the same charts over completely different data, so
 * "whose numbers am I looking at" must never require a click to answer.
 */
export function TenantBadge({ onOpenKeyEntry }: { onOpenKeyEntry: () => void }) {
  const { mode, prefix, signOut } = useTenantContext()

  if (mode === 'sandbox') {
    return (
      <div className="flex items-center gap-3">
        <span className="inline-flex items-center gap-2 rounded-full border border-line-2 px-3 py-1 text-[11px] font-medium uppercase tracking-[0.18em] text-slate">
          <span aria-hidden="true" className="size-1.5 rounded-full bg-slate" />
          Sandbox
        </span>
        <button
          type="button"
          onClick={onOpenKeyEntry}
          className="text-xs font-medium uppercase tracking-[0.18em] text-gold transition-opacity duration-300 ease-refined hover:opacity-75"
        >
          Use key
        </button>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-3">
      <span className="inline-flex items-center gap-2 rounded-full border border-gold/40 bg-gold/10 px-3 py-1 text-[11px] font-medium uppercase tracking-[0.18em] text-gold">
        <span aria-hidden="true" className="size-1.5 rounded-full bg-gold" />
        {/* The prefix only. The full key is never rendered once stored — a dashboard
            left open on a second screen should not be a credential on display. */}
        <span className="font-mono normal-case tracking-normal">{prefix}…</span>
      </span>
      <button
        type="button"
        onClick={signOut}
        className="text-xs font-medium uppercase tracking-[0.18em] text-slate transition-colors duration-300 ease-refined hover:text-cream"
      >
        Sign out
      </button>
    </div>
  )
}

export function KeyEntryModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { signIn } = useTenantContext()
  const [key, setKey] = useState('')
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = useCallback(async () => {
    const candidate = key.trim()
    if (candidate === '') {
      setError('Enter a key.')
      return
    }

    setChecking(true)
    setError(null)
    try {
      // Validated with the candidate key passed explicitly, not by storing it first.
      // `skipAuthReset` keeps a 401 here from signing out a session that already works
      // — the whole point of this call is that a rejection is an expected answer.
      await getWebhookSources({ key: candidate, skipAuthReset: true })
      signIn(candidate)
      setKey('')
      onClose()
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        setError('That key was rejected. It may be mistyped, revoked, or from another environment.')
      } else if (caught instanceof ApiError) {
        setError(`The API answered ${caught.status}. ${caught.detail}`)
      } else {
        setError('Could not reach the API. It may be waking from a cold start — try again.')
      }
    } finally {
      setChecking(false)
    }
  }, [key, signIn, onClose])

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Use an account key"
      description="Switches the whole dashboard — stats, transactions and exceptions — to that account. Without a key everything runs in a sandbox tenant."
      footer={
        <div className="flex items-center justify-end gap-3">
          <Button variant="ghost" onClick={onClose} disabled={checking}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => void submit()} disabled={checking}>
            {checking ? 'Checking…' : 'Continue'}
          </Button>
        </div>
      }
    >
      <div className="space-y-5">
        <div className="space-y-2">
          <label
            htmlFor="api-key"
            className="block text-[11px] font-medium uppercase tracking-[0.18em] text-slate"
          >
            API key
          </label>
          <input
            id="api-key"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={key}
            placeholder="llk_…"
            onChange={(event) => setKey(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !checking) void submit()
            }}
            aria-invalid={error !== null}
            aria-describedby={error === null ? undefined : 'api-key-error'}
            className="w-full rounded-md border border-line-2 bg-ink px-3.5 py-2.5 font-mono text-sm text-cream transition-colors duration-300 ease-refined focus:border-gold/70 focus:outline-none"
          />
          {error !== null && (
            <p id="api-key-error" role="alert" className="text-xs leading-relaxed text-rose">
              {error}
            </p>
          )}
        </div>

        <p className="text-xs font-light leading-relaxed text-slate">
          Keys are issued from the server with{' '}
          <code className="font-mono text-ash">python -m scripts.create_api_key</code>. This one is
          held in <code className="font-mono text-ash">sessionStorage</code> and clears when the tab
          closes; only its first characters are ever shown again.
        </p>
      </div>
    </Modal>
  )
}

/**
 * Shown when a stored key stops working mid-session.
 *
 * Necessary because the backend deliberately does not fall back to the demo tenant on a
 * bad key: without this the dashboard would simply start failing everywhere, with the
 * revocation invisible. Dismissible, because by the time it is read the app has already
 * recovered into sandbox mode.
 */
export function ExpiredKeyBanner() {
  const { expired, dismissExpiry } = useTenantContext()
  if (!expired) return null

  return (
    <div
      role="status"
      className="mx-auto mt-6 flex w-full max-w-[1600px] items-start justify-between gap-6 rounded-lg border border-gold/40 bg-gold/10 px-5 py-4 text-sm leading-relaxed text-gold"
    >
      <p>
        That account key was rejected — it has been revoked, or it belongs to another
        environment. The dashboard has returned to sandbox data.
      </p>
      <button
        type="button"
        onClick={dismissExpiry}
        aria-label="Dismiss"
        className="-mt-1 shrink-0 px-2 text-lg leading-none transition-opacity duration-300 ease-refined hover:opacity-70"
      >
        &times;
      </button>
    </div>
  )
}
