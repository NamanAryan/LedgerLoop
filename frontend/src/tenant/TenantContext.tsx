/**
 * Which tenant the dashboard is acting as, for rendering.
 *
 * The authority is `api/session.ts`; this mirrors it into React so components can show
 * the mode and re-render when it changes. The mirror is one-way and the provider
 * subscribes to the module rather than owning the value, which matters because the key
 * can be cleared from outside React entirely — `client.ts` drops it on a 401 from any
 * request, including one fired from a callback or an interval.
 *
 * ## Why mode is not per-endpoint
 *
 * The obvious design is to send the key only on webhook calls and leave the rest of the
 * dashboard alone. It does not work. `/v1/stats`, `/v1/transactions` and
 * `/v1/exceptions` would keep resolving to the demo tenant, so live gateway events
 * would land in the real account while the feed underneath rendered sandbox data. You
 * would connect Stripe successfully and never see a single transaction from it.
 *
 * So the key switches the whole dashboard. From the operator's point of view auth is
 * still "the thing you do to use webhooks", because that is the only feature that asks
 * for a key — it just cannot be scoped to individual requests.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  clearApiKey,
  currentMode,
  getApiKey,
  keyPrefix,
  setApiKey,
  subscribe,
  type TenantMode,
} from '../api/session'

interface TenantContextValue {
  mode: TenantMode
  /** The non-secret fragment, for display. Null in sandbox mode. */
  prefix: string | null
  /** True when the last key was cleared by a 401 rather than by signing out. Drives the
   *  banner, because a session that ends by itself needs explaining and one the
   *  operator ended does not. */
  expired: boolean
  signIn: (key: string) => void
  signOut: () => void
  dismissExpiry: () => void
}

const TenantContext = createContext<TenantContextValue | null>(null)

export function TenantProvider({ children }: { children: ReactNode }) {
  const [key, setKey] = useState<string | null>(() => getApiKey())
  const [expired, setExpired] = useState(false)

  /**
   * Set while this component is itself changing the key.
   *
   * The subscriber below cannot otherwise tell "the operator signed out" from "the
   * backend rejected the key", because both arrive as the same notification with the
   * same resulting value. Only the second deserves the banner — explaining a session
   * ending to the person who just ended it is noise.
   */
  const selfInitiated = useRef(false)

  // The module is the source of truth, so this listens rather than leads: a key cleared
  // by a 401 inside some unrelated poll still moves the header and the webhook card.
  useEffect(() => {
    return subscribe(() => {
      const next = getApiKey()
      // Read rather than derived inside a state updater. An updater must stay pure —
      // StrictMode invokes it twice in development, so a setState in there fires twice
      // and anything less idempotent than this would corrupt.
      setKey((previous) => {
        if (previous !== null && next === null && !selfInitiated.current) {
          // Queued, not called inline, for the same purity reason.
          queueMicrotask(() => setExpired(true))
        }
        return next
      })
    })
  }, [])

  const signIn = useCallback((next: string) => {
    selfInitiated.current = true
    setExpired(false)
    setApiKey(next)
    setKey(next)
    selfInitiated.current = false
  }, [])

  const signOut = useCallback(() => {
    selfInitiated.current = true
    clearApiKey()
    setKey(null)
    setExpired(false)
    selfInitiated.current = false
  }, [])

  const value = useMemo<TenantContextValue>(
    () => ({
      // Derived from the key on every render rather than stored beside it. Two fields
      // that must agree are two fields that will eventually disagree.
      mode: key === null ? 'sandbox' : 'account',
      prefix: key === null ? null : keyPrefix(key),
      expired,
      signIn,
      signOut,
      dismissExpiry: () => setExpired(false),
    }),
    [key, expired, signIn, signOut],
  )

  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>
}

export function useTenantContext(): TenantContextValue {
  const value = useContext(TenantContext)
  if (value === null) {
    throw new Error('useTenantContext must be used inside <TenantProvider>')
  }
  return value
}

/** Kept exported so a non-rendering caller can ask without pulling in the context. */
export { currentMode }
