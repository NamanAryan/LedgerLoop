/**
 * Which tenant this tab is acting as — the single source of truth for that question.
 *
 * Deliberately a module, not React state. `api/client.ts` and `api/ingest.ts` are plain
 * async functions that predate any of this and are called from callbacks and loops, not
 * from render; threading a context value through all of them would mean either passing
 * a token down every signature or calling hooks where hooks cannot go. So the session
 * lives here, `TenantProvider` mirrors it into React for rendering, and every request
 * reads it from one place.
 *
 * ## The two modes
 *
 * **Sandbox** (no key). Sends `X-Demo-Session` from a persisted UUID. The backend
 * creates an ephemeral demo tenant for it and sweeps it 24h after it goes idle.
 *
 * **Account** (key present). Sends `Authorization: Bearer` and *no* demo header.
 *
 * Never both. The backend prefers the key and ignores the demo header when one is
 * present, so sending both changes nothing about the response — which is exactly why
 * it must not be done: it would let a mistake in this file sit invisible behind a
 * backend that quietly does the right thing anyway.
 *
 * Mode is derived from whether a key is stored, never tracked alongside it. Two fields
 * that must agree eventually disagree.
 *
 * ## Where the key is kept
 *
 * `sessionStorage`, so it dies with the tab. A bearer token in web storage is readable
 * by any script that achieves XSS on this page — there is no way around that, and
 * `localStorage` would only widen the window by persisting it across sessions. This is
 * acceptable for a demo dashboard whose account holds synthetic reconciliation data.
 * It would not be acceptable for a production console, where the answer is a
 * short-lived `httpOnly; Secure; SameSite` cookie session that JavaScript cannot read
 * at all, with the key exchanged for it server-side.
 */

const KEY_STORAGE = 'ledgerloop.apiKey'
const DEMO_STORAGE = 'ledgerloop.demoSession'

/** Characters of the key shown in the header. Enough to tell two keys apart, far too
 *  few to narrow a search of the 256-bit space behind it. */
const PREFIX_LENGTH = 12

export type TenantMode = 'sandbox' | 'account'

/** Cached so a value blocked or cleared mid-session stays stable for this page load. */
let apiKey: string | null = null
let apiKeyLoaded = false
let demoSession: string | null = null

/** Notified when a stored key turns out to be stale, so the UI can drop to sandbox. */
type Listener = () => void
const listeners = new Set<Listener>()

export function subscribe(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function announce(): void {
  for (const listener of listeners) listener()
}

/* --- demo session --------------------------------------------------------- */

/**
 * This browser's sandbox tenant id.
 *
 * `localStorage`, unlike the key: it is not a credential, and losing it on every tab
 * close would strand the transactions someone just uploaded under a tenant nothing can
 * address any more. Validated on read as well as write, because a value corrupted by
 * hand would otherwise be sent forever and rejected with a 400 on every request, with
 * nothing on screen to suggest clearing it.
 */
export function demoSessionId(): string {
  if (demoSession !== null) return demoSession

  try {
    const stored = window.localStorage.getItem(DEMO_STORAGE)
    if (stored !== null && /^[0-9a-f-]{36}$/i.test(stored)) {
      demoSession = stored
      return stored
    }
  } catch {
    // Storage blocked (private window, or site data disabled). Fall through.
  }

  const minted = crypto.randomUUID()
  try {
    window.localStorage.setItem(DEMO_STORAGE, minted)
  } catch {
    // Nothing to do — the module-level cache still keeps it stable for this page load.
  }
  demoSession = minted
  return minted
}

/* --- api key -------------------------------------------------------------- */

export function getApiKey(): string | null {
  if (!apiKeyLoaded) {
    try {
      apiKey = window.sessionStorage.getItem(KEY_STORAGE)
    } catch {
      apiKey = null
    }
    apiKeyLoaded = true
  }
  return apiKey
}

export function setApiKey(key: string): void {
  apiKey = key
  apiKeyLoaded = true
  try {
    window.sessionStorage.setItem(KEY_STORAGE, key)
  } catch {
    // Storage blocked. The key still works for this page load from the cache above.
  }
  announce()
}

export function clearApiKey(): void {
  apiKey = null
  apiKeyLoaded = true
  try {
    window.sessionStorage.removeItem(KEY_STORAGE)
  } catch {
    // Nothing to remove, or storage is blocked. Either way the cache is cleared.
  }
  announce()
}

/** Derived, never stored. */
export function currentMode(): TenantMode {
  return getApiKey() === null ? 'sandbox' : 'account'
}

/** The non-secret fragment shown in the header. Never the whole key. */
export function keyPrefix(key: string): string {
  return key.slice(0, PREFIX_LENGTH)
}

/**
 * The tenant headers for one request.
 *
 * Exactly one of the two, always — see the module docstring for why sending both is
 * worse than sending the wrong one.
 */
export function tenantHeaders(): Record<string, string> {
  const key = getApiKey()
  return key === null
    ? { 'X-Demo-Session': demoSessionId() }
    : { Authorization: `Bearer ${key}` }
}
