/**
 * The HTTP client. Every call to the backend goes through here.
 *
 * The base URL comes from `VITE_API_BASE_URL` and is never hardcoded: the same build
 * artefact points at localhost in development and at Render in production, which is
 * what makes the static deploy a single `vite build` with no per-environment branch.
 *
 * Two behaviours worth stating, because they encode the API's contract rather than
 * generic HTTP habits:
 *
 * **A 202 with `duplicate: true` is success.** The ingestion endpoints answer a
 * retried submission with 202, never 409, because clients retry on 5xx and network
 * failures and answering a successful retry with an error makes them retry the retry.
 * Nothing in this file treats `duplicate` as a failure.
 *
 * **Only 5xx and network errors are retried.** A 4xx means the request was wrong and
 * resending it unchanged will be wrong again; a 422 in particular is a validation
 * failure that needs a human to fix the mapping, not a backoff.
 *
 * **Every request carries the current tenant.** The backend is multi-tenant and this
 * client has two modes, held in `api/session.ts`: sandbox sends `X-Demo-Session`,
 * account sends `Authorization: Bearer`. Never both. Which one applies is decided in
 * one place — here — so a screen added later cannot forget it and silently read or
 * write into the wrong tenant.
 *
 * **A 401 while a key is stored means the key is stale.** Phase 1 deliberately does
 * *not* fall back to the demo tenant on a bad key, so without handling this centrally
 * a revoked key would make every screen in the dashboard fail at once with no way out.
 * `request()` clears the key and notifies, dropping the app to sandbox mode.
 */

import { clearApiKey, getApiKey, tenantHeaders } from './session'
import type {
  ExceptionPage,
  GatewayWebhookAccepted,
  GatewayWebhookIn,
  LedgerEntryIn,
  LedgerSyncAccepted,
  ReadyOut,
  ReconStatus,
  StatsOut,
  StatsWindow,
  TransactionPage,
  WebhookSource,
  WebhookSourceCreate,
  WebhookSourceList,
  WebhookTestResult,
} from './types'

/**
 * Trailing slash stripped so `${base}/v1/stats` never becomes a double slash, which
 * some proxies answer with a redirect that drops the CORS headers.
 */
export const API_BASE: string = (
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000'
).replace(/\/+$/, '')

/** A non-2xx response, carrying enough to show the operator what the backend said. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly path: string,
  ) {
    super(`${status} ${path}: ${detail}`)
    this.name = 'ApiError'
  }

  /** 4xx: the request itself is wrong. Resending it unchanged cannot help. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500
  }
}

/** FastAPI answers a validation failure with a list of per-field errors; flatten it. */
function describe(status: number, body: unknown): string {
  if (typeof body === 'string' && body !== '') return body
  if (body !== null && typeof body === 'object') {
    const detail = (body as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          const entry = item as { loc?: unknown[]; msg?: string }
          const where = Array.isArray(entry.loc) ? entry.loc.join('.') : ''
          return where ? `${where}: ${entry.msg ?? 'invalid'}` : (entry.msg ?? 'invalid')
        })
        .slice(0, 3)
        .join('; ')
    }
  }
  return `HTTP ${status}`
}

interface RequestOptions {
  method?: 'GET' | 'POST'
  body?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
  /** Attempts for 5xx and network failures. 1 means no retry. */
  attempts?: number
  /**
   * Leave a stored key alone on a 401.
   *
   * Only the key-entry modal sets this: it is *testing* a candidate key, so a 401 is
   * the expected answer to a typo and must not sign the operator out of the session
   * they already have.
   */
  skipAuthReset?: boolean
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const {
    method = 'GET',
    body,
    headers = {},
    signal,
    attempts = 3,
    skipAuthReset = false,
  } = options
  let lastError: unknown

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetch(`${API_BASE}${path}`, {
        method,
        signal,
        headers: {
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          // Applied here rather than at each call site, so a screen added later cannot
          // forget it and silently read or write into the wrong tenant.
          ...tenantHeaders(),
          ...headers,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      })

      if (!response.ok) {
        const raw: unknown = await response.json().catch(() => null)
        const error = new ApiError(response.status, describe(response.status, raw), path)
        // A 401 with a key stored means that key is stale or revoked. The backend
        // never falls back to the demo tenant on a bad key, so leaving it in place
        // would break every screen at once with nothing on offer but a hard reload.
        // Clearing it here drops the whole app to sandbox in one step.
        //
        // `skipAuthReset` exempts the validation call in the key-entry modal, where a
        // 401 is the expected answer to a typo and must not disturb a working session.
        if (response.status === 401 && !skipAuthReset && getApiKey() !== null) {
          clearApiKey()
        }
        // A 4xx will fail identically on the next attempt. Surface it now.
        if (error.isClientError) throw error
        lastError = error
      } else {
        return (await response.json()) as T
      }
    } catch (error) {
      // An aborted request is the caller changing its mind, not a failure to retry.
      if (error instanceof DOMException && error.name === 'AbortError') throw error
      if (error instanceof ApiError && error.isClientError) throw error
      lastError = error
    }

    if (attempt < attempts) {
      // Exponential backoff. The ingestion endpoints are idempotent, so a retry after
      // a 5xx that actually committed is a no-op rather than a double count.
      await new Promise((resolve) => setTimeout(resolve, 200 * 2 ** (attempt - 1)))
    }
  }

  throw lastError instanceof Error
    ? lastError
    : new Error(`request to ${path} failed after ${attempts} attempts`)
}

// --------------------------------------------------------------------------- //
// Ingestion                                                                     //
// --------------------------------------------------------------------------- //

/**
 * One gateway transaction. The idempotency key is a required header — the backend
 * rejects the request without it, deliberately, because without a client-supplied key
 * it cannot tell a retry from a genuine second payment of the same amount in the same
 * second.
 */
export function postGatewayWebhook(
  payload: GatewayWebhookIn,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<GatewayWebhookAccepted> {
  return request<GatewayWebhookAccepted>('/v1/gateway/webhook', {
    method: 'POST',
    body: payload,
    headers: { 'Idempotency-Key': idempotencyKey },
    signal,
  })
}

/** A batch of ledger entries, up to LEDGER_BATCH_MAX. Chunking is the caller's job. */
export function postLedgerSync(
  entries: LedgerEntryIn[],
  signal?: AbortSignal,
): Promise<LedgerSyncAccepted> {
  return request<LedgerSyncAccepted>('/v1/ledger/sync', {
    method: 'POST',
    body: { entries },
    signal,
  })
}

// --------------------------------------------------------------------------- //
// Read path                                                                     //
// --------------------------------------------------------------------------- //

export function getStats(window: StatsWindow, signal?: AbortSignal): Promise<StatsOut> {
  return request<StatsOut>(`/v1/stats?window=${window}`, { signal })
}

export function listTransactions(
  options: { status?: ReconStatus | null; limit?: number; cursor?: string | null } = {},
  signal?: AbortSignal,
): Promise<TransactionPage> {
  const params = new URLSearchParams()
  if (options.status) params.set('status', options.status)
  params.set('limit', String(options.limit ?? 50))
  // The cursor is opaque: it is echoed back exactly as received, never parsed or
  // incremented. The backend answers a malformed one with 400 rather than silently
  // restarting at page 1.
  if (options.cursor) params.set('cursor', options.cursor)
  return request<TransactionPage>(`/v1/transactions?${params}`, { signal })
}

export function listExceptions(
  options: { status?: 'open' | 'closed' | null; limit?: number; cursor?: string | null } = {},
  signal?: AbortSignal,
): Promise<ExceptionPage> {
  const params = new URLSearchParams()
  if (options.status) params.set('status', options.status)
  params.set('limit', String(options.limit ?? 50))
  if (options.cursor) params.set('cursor', options.cursor)
  return request<ExceptionPage>(`/v1/exceptions?${params}`, { signal })
}

/**
 * Close an exception. Answers 409 if someone else already closed it — a
 * compare-and-set on `closed_at IS NULL`, so two operators cannot both win and the
 * loser is told rather than silently overwriting the first resolution note.
 * Not retried: this is the one non-idempotent write in the API.
 */
export function resolveException(
  id: number,
  resolutionNotes: string,
  signal?: AbortSignal,
): Promise<import('./types').ExceptionOut> {
  return request(`/v1/exceptions/${id}/resolve`, {
    method: 'POST',
    body: { resolution_notes: resolutionNotes },
    signal,
    attempts: 1,
  })
}

// --------------------------------------------------------------------------- //
// Ops                                                                           //
// --------------------------------------------------------------------------- //

/** Liveness. Also the keep-alive target for a free-tier instance that spins down. */
export function getHealth(signal?: AbortSignal): Promise<{ status: string; service: string }> {
  return request('/health', { signal, attempts: 1 })
}

/** Readiness: reports Postgres and Redis individually. 503 when either is down. */
export function getReady(signal?: AbortSignal): Promise<ReadyOut> {
  return request<ReadyOut>('/ready', { signal, attempts: 1 })
}

// --------------------------------------------------------------------------- //
// Webhook sources                                                               //
// --------------------------------------------------------------------------- //

/**
 * The caller's configured endpoints.
 *
 * Doubles as the key-validation call: it is the cheapest authenticated `GET` on the
 * API, so the key-entry modal uses it to tell a good key from a bad one before storing
 * anything. `skipAuthReset` is set there so a rejected candidate does not sign out the
 * session that is already working.
 */
export function getWebhookSources(options: {
  signal?: AbortSignal
  key?: string
  skipAuthReset?: boolean
} = {}): Promise<WebhookSourceList> {
  const { signal, key, skipAuthReset = false } = options
  return request<WebhookSourceList>('/v1/gateway/sources', {
    signal,
    // A candidate key overrides the stored one for this call only — validating a key
    // must not require storing it first.
    headers: key === undefined ? {} : { Authorization: `Bearer ${key}` },
    skipAuthReset,
    // One attempt when validating: a retry would just repeat a 401 and make the modal
    // feel broken rather than decisive.
    attempts: key === undefined ? 3 : 1,
  })
}

/** Register an endpoint. 403 for demo tenants — see the backend route for why. */
export function createWebhookSource(payload: WebhookSourceCreate): Promise<WebhookSource> {
  return request<WebhookSource>('/v1/gateway/sources', {
    method: 'POST',
    body: payload,
    // Not retried. A 5xx here might still have created the source, and a blind retry
    // would leave the account with two endpoints and no way to tell which is which.
    attempts: 1,
  })
}

/**
 * Ask the server to sign a synthetic event with this source's secret and run it through
 * the real verify → adapt → ingest path.
 *
 * Server-side because the secret never leaves the backend; the dashboard holds none and
 * could only ever produce a 401. What it does not cover is the network hop from the
 * provider — DNS, TLS, the proxy, a cold start — so a pass means "this source is wired
 * up correctly", not "Stripe can reach you".
 */
export function sendTestPayload(sourceId: number): Promise<WebhookTestResult> {
  return request<WebhookTestResult>(`/v1/gateway/sources/${sourceId}/test`, {
    method: 'POST',
    attempts: 1,
  })
}

/** The URL a provider posts to. Built from the same base as every other call, so a
 *  misconfigured `VITE_API_BASE_URL` is wrong in one obvious place rather than two. */
export function webhookUrl(sourceToken: string): string {
  return `${API_BASE}/v1/gateway/webhook/${sourceToken}`
}
