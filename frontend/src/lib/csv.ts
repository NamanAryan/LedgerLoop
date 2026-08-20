/**
 * CSV ingestion: parse, preview, map, coerce.
 *
 * Tokenising is PapaParse's job — it handles RFC 4180, delimiter sniffing, CRLF, BOM
 * and quoted fields, and there is no reason to hand-roll that. What is *not* delegated
 * is what happens to the rows that do not parse. A reconciliation tool that silently
 * drops eleven malformed rows has manufactured eleven breaks, so every refusal here is
 * counted, reasoned, and surfaced with its line number.
 *
 * Nothing in this file decides whether two rows match. It turns a file into rows the
 * API will accept, and that is the whole of its job.
 */

import Papa from 'papaparse'
import { parseMinor, type Minor } from './money'
import type { Side } from '../api/types'

export interface ParsedCsv {
  headers: string[]
  /** Data rows, header excluded. Ragged rows are padded, never dropped. */
  rows: string[][]
  delimiter: string
  /** Rows whose field count disagreed with the header. Padded and flagged, not lost. */
  raggedRows: number
}

/**
 * Parse a CSV file into a header row plus padded data rows.
 *
 * `skipEmptyLines: 'greedy'` drops lines that are blank or only separators, which is
 * what a trailing newline and Excel's habit of exporting empty rows both produce. A
 * genuinely empty data row carries no transaction, so nothing is lost by it.
 */
export function parseCsv(input: string): ParsedCsv {
  const result = Papa.parse<string[]>(input, {
    header: false,
    skipEmptyLines: 'greedy',
    // Everything stays a string. PapaParse's dynamic typing would turn an amount into
    // a float and a txn_id like "0012" into 12 -- both of which this app exists to
    // avoid. Coercion happens once, explicitly, in coerceRows.
    dynamicTyping: false,
  })

  const records = result.data.filter((row) => Array.isArray(row))
  const headerRow = records.shift() ?? []
  const headers = headerRow.map((cell, index) => {
    const trimmed = (cell ?? '').trim()
    return trimmed === '' ? `column_${index + 1}` : trimmed
  })

  let raggedRows = 0
  const rows = records.map((row) => {
    if (row.length !== headers.length) raggedRows += 1
    const padded = row.slice(0, headers.length)
    while (padded.length < headers.length) padded.push('')
    return padded
  })

  return {
    headers,
    rows,
    delimiter: result.meta.delimiter || ',',
    raggedRows,
  }
}

/** The five fields the API needs, in the order the mapping UI presents them. */
export const REQUIRED_FIELDS = [
  'txn_id',
  'amount',
  'currency',
  'timestamp',
  'idempotency_key',
] as const

export type FieldName = (typeof REQUIRED_FIELDS)[number]

/**
 * Fields the ingestion endpoints cannot run without.
 *
 * `currency` is absent only because the mapping UI offers a fixed fallback — a gateway
 * export with a single-currency book routinely omits the column. `idempotency_key` is
 * optional too, and its absence has a stated consequence: one is derived from the row's
 * own content instead, so a re-upload of the same file is recognised as a repeat.
 */
export const HARD_REQUIRED: readonly FieldName[] = ['txn_id', 'amount', 'timestamp']

export type ColumnMap = Record<FieldName, string | null>

export const EMPTY_MAP: ColumnMap = {
  txn_id: null,
  amount: null,
  currency: null,
  timestamp: null,
  idempotency_key: null,
}

/** Header spellings seen in the wild, most specific first. */
const SYNONYMS: Record<FieldName, string[]> = {
  txn_id: [
    'txn_id',
    'txnid',
    'transaction_id',
    'transactionid',
    'payment_id',
    'reference',
    'ref',
    'order_id',
    'id',
  ],
  amount: ['amount', 'amount_minor', 'value', 'gross_amount', 'net_amount', 'total', 'sum'],
  currency: ['currency', 'currency_code', 'ccy', 'iso_currency'],
  timestamp: [
    'timestamp',
    'occurred_at',
    'created_at',
    'captured_at',
    'settled_at',
    'booked_at',
    'transaction_date',
    'value_date',
    'posted_at',
    'datetime',
    'date',
    'time',
  ],
  idempotency_key: [
    'idempotency_key',
    'idempotencykey',
    'entry_ref',
    'entry_id',
    'dedupe_key',
    'request_id',
  ],
}

function normalise(header: string): string {
  return header.toLowerCase().replace(/[\s-]+/g, '_').replace(/[^a-z0-9_]/g, '')
}

/**
 * Best-effort auto-mapping, so the common case is one glance instead of five
 * dropdowns. Every guess stays editable — this fills the form, it does not decide.
 */
export function guessMapping(headers: readonly string[]): ColumnMap {
  const normalised = headers.map(normalise)
  const map: ColumnMap = { ...EMPTY_MAP }
  const taken = new Set<string>()

  for (const field of REQUIRED_FIELDS) {
    for (const synonym of SYNONYMS[field]) {
      const index = normalised.findIndex(
        (header, i) => header === synonym && !taken.has(headers[i] as string),
      )
      if (index !== -1) {
        const header = headers[index] as string
        map[field] = header
        taken.add(header)
        break
      }
    }
  }

  // Second pass for headers that contain rather than equal a synonym
  // ("gateway_txn_id", "amount_in_inr"). Exact hits above always win.
  for (const field of REQUIRED_FIELDS) {
    if (map[field] !== null) continue
    for (const synonym of SYNONYMS[field]) {
      const index = normalised.findIndex(
        (header, i) => header.includes(synonym) && !taken.has(headers[i] as string),
      )
      if (index !== -1) {
        const header = headers[index] as string
        map[field] = header
        taken.add(header)
        break
      }
    }
  }

  return map
}

/** Which required fields are still unmapped, phrased for the operator. */
export function validationErrors(map: ColumnMap): string[] {
  const labels: Record<FieldName, string> = {
    txn_id: 'transaction ID',
    amount: 'amount',
    currency: 'currency',
    timestamp: 'timestamp',
    idempotency_key: 'idempotency key',
  }
  return HARD_REQUIRED.filter((field) => map[field] === null).map(
    (field) => `Map the ${labels[field]} column to continue`,
  )
}

/**
 * Parse a timestamp without guessing between ambiguous regional formats.
 *
 * ISO 8601 and epoch numbers are accepted because they are unambiguous. A bare
 * `03/04/2026` is not — it is March 4th in one hemisphere and April 3rd in another,
 * and an engine that picks one silently will reconcile the wrong day's settlement.
 * Those rows are rejected and counted instead.
 */
export function parseTimestamp(raw: string): number | null {
  const text = raw.trim()
  if (text === '') return null

  if (/^\d{10}$/.test(text)) return Number(text) * 1000 // epoch seconds
  if (/^\d{13}$/.test(text)) return Number(text) // epoch milliseconds

  // ISO 8601, with a space instead of the T tolerated. A timestamp with no zone is
  // read as UTC rather than the viewer's local zone, so the same file reconciles
  // identically in Mumbai and in New York.
  const iso = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)(Z|[+-]\d{2}:?\d{2})?$/
  const match = iso.exec(text)
  if (match) {
    const zone = match[3] ?? 'Z'
    const value = Date.parse(`${match[1]}T${match[2]}${zone === 'Z' ? 'Z' : zone}`)
    return Number.isNaN(value) ? null : value
  }

  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    const value = Date.parse(`${text}T00:00:00Z`)
    return Number.isNaN(value) ? null : value
  }

  return null
}

/**
 * One row on its way to the API.
 *
 * Deliberately not the API payload itself: the amount is still an integer and the
 * instant is still epoch milliseconds, because those are the forms that survive being
 * compared and sorted in the UI. Serialisation to the wire happens once, in
 * `api/ingest.ts`.
 */
export interface SourceRow {
  readonly side: Side
  /** 1-based line in the source file, for reporting a rejection back to the operator. */
  readonly line: number
  readonly txnId: string
  readonly amount: Minor
  readonly currency: string
  /** Epoch milliseconds, always an absolute instant. */
  readonly occurredAt: number
  readonly idempotencyKey: string
}

export interface CoerceResult {
  rows: SourceRow[]
  /** Rows the API would have refused, with the reason, capped for display. */
  rejects: { line: number; reason: string }[]
  rejectedCount: number
}

export interface CoerceOptions {
  side: Side
  map: ColumnMap
  /** Used when the currency column is unmapped. */
  fallbackCurrency: string
  /**
   * Prefixed onto every derived idempotency key.
   *
   * Empty for an uploaded file, which makes the derived key a pure function of the
   * row's own content — so re-uploading the same file is recognised by the backend as
   * a repeat submission rather than counted twice. The synthetic generator passes its
   * run id instead, because two runs are genuinely different transactions that happen
   * to look alike.
   */
  keyPrefix?: string
}

const MAX_REPORTED_REJECTS = 50

/**
 * Turn mapped CSV rows into rows the ingestion endpoints will accept, counting every
 * refusal. The validation mirrors the API's own request models, so a row that survives
 * here is one the backend will not 422 — a rejection is shown against the file, where
 * the operator can fix it, rather than as a failed HTTP call halfway through an upload.
 */
export function coerceRows(parsed: ParsedCsv, options: CoerceOptions): CoerceResult {
  const index = (field: FieldName): number => {
    const header = options.map[field]
    return header === null ? -1 : parsed.headers.indexOf(header)
  }

  const txnIdAt = index('txn_id')
  const amountAt = index('amount')
  const currencyAt = index('currency')
  const timestampAt = index('timestamp')
  const keyAt = index('idempotency_key')
  const prefix = options.keyPrefix ?? ''

  const rows: SourceRow[] = []
  const rejects: { line: number; reason: string }[] = []
  let rejectedCount = 0

  parsed.rows.forEach((cells, i) => {
    const line = i + 2 // +1 for the header, +1 because operators count from one
    const reject = (reason: string) => {
      rejectedCount += 1
      if (rejects.length < MAX_REPORTED_REJECTS) rejects.push({ line, reason })
    }

    const txnId = (cells[txnIdAt] ?? '').trim()
    if (txnId === '') return reject('empty transaction ID')

    const amount = parseMinor(cells[amountAt] ?? '')
    if (amount === null) return reject(`amount "${cells[amountAt] ?? ''}" is not a number`)
    // The API rejects a zero amount: a zero-value transaction has nothing to reconcile.
    // Catching it here costs one comparison and saves a 422 mid-upload.
    if (amount === 0) return reject('amount is zero')

    const occurredAt = parseTimestamp(cells[timestampAt] ?? '')
    if (occurredAt === null) {
      return reject(`timestamp "${cells[timestampAt] ?? ''}" is not ISO 8601 or an epoch value`)
    }

    const currency =
      currencyAt === -1
        ? options.fallbackCurrency
        : ((cells[currencyAt] ?? '').trim().toUpperCase() || options.fallbackCurrency)
    // ISO 4217 is checked by a CHECK constraint in the schema and a pattern in the
    // request model. A file with "Rs" in the currency column would otherwise fail one
    // row at a time, at the network, after the upload had already started.
    if (!/^[A-Z]{3}$/.test(currency)) {
      return reject(`currency "${currency}" is not a 3-letter ISO 4217 code`)
    }

    const mapped = keyAt === -1 ? '' : (cells[keyAt] ?? '').trim()

    rows.push({
      side: options.side,
      line,
      txnId,
      amount,
      currency,
      occurredAt,
      // A mapped key is authoritative: it came from the system of record and is what
      // that system means by "the same submission". Only when the column is absent do
      // we derive one, and then from content, so it is stable across re-uploads.
      idempotencyKey: mapped !== '' ? mapped : deriveKey(options.side, txnId, amount, occurredAt, prefix),
    })
  })

  return { rows, rejects, rejectedCount }
}

/**
 * A content-addressed idempotency key for a file that carries none.
 *
 * Side is part of the key because the two streams are independent: the same txn_id
 * legitimately appears once on each side, and collapsing them would make every
 * transaction look like its own duplicate.
 */
export function deriveKey(
  side: Side,
  txnId: string,
  amount: Minor,
  occurredAt: number,
  prefix = '',
): string {
  const body = `${side}:${txnId}:${amount}:${occurredAt}`
  return prefix === '' ? body : `${prefix}:${body}`
}
