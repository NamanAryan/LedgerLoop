/**
 * The backend's wire contract, transcribed.
 *
 * These mirror `backend/ledgerloop/api/schemas.py` and the native enums in
 * `backend/ledgerloop/db/enums.py`. There is no matching logic here and there is
 * none anywhere else in this app: the engine lives in the backend, this is a client.
 *
 * Money crosses the wire as a **decimal string**, never a number. The backend stores
 * `numeric(18,2)` and a JSON number would be parsed into a float on the way in,
 * reintroducing exactly the binary error the schema exists to avoid. Amounts are
 * parsed to integer minor units at the edge (`lib/money.ts`) or rendered straight
 * from the string; they are never arithmetic in this codebase.
 */

export type Side = 'gateway' | 'ledger'

export type ReconStatus =
  | 'matched'
  | 'unmatched_gateway_only'
  | 'unmatched_ledger_only'
  | 'duplicate'
  | 'amount_drift'
  | 'time_drift'

export type MatchLayer =
  | 'exact'
  | 'time_drift'
  | 'amount_drift'
  | 'duplicate'
  | 'unmatched_sweep'

/** Statuses the backend opens an `exceptions` row for. Mirrors EXCEPTION_STATUSES. */
export const EXCEPTION_STATUSES: ReadonlySet<ReconStatus> = new Set<ReconStatus>([
  'amount_drift',
  'unmatched_gateway_only',
  'unmatched_ledger_only',
])

export type StatsWindow = '1h' | '24h' | '7d'

// --------------------------------------------------------------------------- //
// Ingestion                                                                     //
// --------------------------------------------------------------------------- //

/** `POST /v1/gateway/webhook`. The Idempotency-Key travels as a header, not a field. */
export interface GatewayWebhookIn {
  txn_id: string
  amount: string
  currency: string
  occurred_at: string
  gateway_ref: string
}

/** One entry in `POST /v1/ledger/sync`. Here the key *is* a field. */
export interface LedgerEntryIn {
  entry_id: string
  txn_id: string
  amount: string
  currency: string
  occurred_at: string
  idempotency_key: string
}

export interface IngestAck {
  row_id: number
  txn_id: string
  /** True when the backend already held this idempotency key. Not an error. */
  duplicate: boolean
  submissions: number
}

export interface GatewayWebhookAccepted {
  accepted: true
  result: IngestAck
}

export interface LedgerSyncAccepted {
  accepted: number
  duplicates: number
  results: IngestAck[]
}

/** The endpoint's own cap, enforced by the request model. Batches are chunked to it. */
export const LEDGER_BATCH_MAX = 1000

// --------------------------------------------------------------------------- //
// Read path                                                                     //
// --------------------------------------------------------------------------- //

export interface LatencyPercentiles {
  p50: number | null
  p95: number | null
  p99: number | null
}

export interface StatsOut {
  window: StatsWindow
  window_seconds: number
  matched: number
  /** Matched, but only via layer 2. Keyed on match_layer, not status. */
  matched_via_time_drift: number
  unmatched: number
  unmatched_gateway_only: number
  unmatched_ledger_only: number
  duplicates: number
  drift: number
  /** matched + unmatched + drift. Duplicates are excluded from the denominator. */
  total: number
  match_rate: number
  latency_ms: LatencyPercentiles
  throughput_tx_per_sec: number
  /**
   * The server's unmatched window, in seconds.
   *
   * Reported because a client cannot otherwise tell "not matched yet" from "declared a
   * break". A row whose counterparty never arrives stays pending until the sweeper has
   * waited this long, so any unmatched count read sooner than this after ingestion is
   * incomplete by design — and calling that a discrepancy would be a false alarm.
   */
  unmatched_after_s: number
  /** Not window-scoped: a break opened last week is still open work. */
  open_exceptions: number
}

export interface ReconciliationResultOut {
  id: number
  gateway_txn_id: number | null
  ledger_entry_id: number | null
  status: ReconStatus
  match_layer: MatchLayer
  resolved_at: string
  match_latency_ms: number | null
  notes: string | null
  /** Null only if neither side survived, which the schema forbids. */
  txn_id: string | null
  currency: string | null
  /** Decimal strings. Either may be null — that is what a one-sided break *is*. */
  gateway_amount: string | null
  ledger_amount: string | null
  /**
   * ISO 8601. The difference between these two is the clock skew that layer 2 exists
   * to tolerate, so "matched via time drift" is only legible to a reader who has both.
   */
  gateway_occurred_at: string | null
  ledger_occurred_at: string | null
}

export interface TransactionPage {
  items: ReconciliationResultOut[]
  /** Opaque. Feed it back as `cursor`; never construct one. */
  next_cursor: string | null
}

export interface ExceptionOut {
  id: number
  reconciliation_result_id: number
  opened_at: string
  closed_at: string | null
  resolution_notes: string | null
  status: ReconStatus
  match_layer: MatchLayer
  gateway_txn_id: number | null
  ledger_entry_id: number | null
  notes: string | null
  txn_id: string | null
  currency: string | null
  gateway_amount: string | null
  ledger_amount: string | null
  gateway_occurred_at: string | null
  ledger_occurred_at: string | null
}

export interface ExceptionPage {
  items: ExceptionOut[]
  next_cursor: string | null
}

// --------------------------------------------------------------------------- //
// Ops                                                                           //
// --------------------------------------------------------------------------- //

export interface DependencyStatus {
  ok: boolean
  detail?: string | null
}

export interface ReadyOut {
  ready: boolean
  database: DependencyStatus
  redis: DependencyStatus
}

// --------------------------------------------------------------------------- //
// Webhook sources                                                               //
// --------------------------------------------------------------------------- //

/** Mirrors `WebhookProvider` in db/enums.py. The provider selects both the signature
 *  scheme and the payload adapter, server-side. */
export type WebhookProvider = 'stripe' | 'razorpay' | 'custom'

/** Mirrors `WebhookDeliveryStatus`. `null` on a source that has never received one. */
export type WebhookDeliveryStatus = 'ok' | 'invalid_signature' | 'invalid_payload'

/**
 * `GET /v1/gateway/sources` item.
 *
 * There is no `signing_secret` here and there must never be one. The backend cannot
 * hash it — HMAC verification needs the bytes themselves — so the entirety of its
 * protection is that it never leaves the database.
 *
 * `source_token` *is* returned: it sits in the URL the provider posts to, so it is not
 * a secret, and an operator needs it to configure the far end.
 */
export interface WebhookSource {
  id: number
  provider: WebhookProvider
  source_token: string
  label: string
  /** ISO-8601, or null if nothing has ever arrived. Stamped on rejected deliveries too
   *  — "receiving nothing" and "rejecting everything" both leave the transaction
   *  tables empty, and this is the only thing that tells them apart. */
  last_event_at: string | null
  last_delivery_status: WebhookDeliveryStatus | null
  created_at: string
  revoked_at: string | null
}

export interface WebhookSourceList {
  items: WebhookSource[]
}

/** `POST /v1/gateway/sources`. No secret field: the server generates it. */
export interface WebhookSourceCreate {
  provider: WebhookProvider
  label?: string
}

/** `POST /v1/gateway/sources/{id}/test`. */
export interface WebhookTestResult {
  delivered: boolean
  http_status: number
  /** True when a redelivery collapsed against the ingestion index. Success, not
   *  failure — reporting it as an error would teach distrust of idempotency. */
  duplicate: boolean
  txn_id: string | null
  detail: string | null
}
