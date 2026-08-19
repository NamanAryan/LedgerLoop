/**
 * CSV ingestion: parse, preview, map, coerce.
 *
 * Written by hand rather than pulled from a library because the interesting part is
 * not splitting on commas — it is what happens to the rows that do not parse. A
 * reconciliation tool that silently drops eleven malformed rows has manufactured
 * eleven breaks. Every rejection here is counted and surfaced.
 */

import { parseMinor } from './money'
import type { Side, TxnFacts } from './types'

export interface ParsedCsv {
  headers: string[]
  /** Data rows, header excluded. Ragged rows are padded, never dropped. */
  rows: string[][]
  delimiter: string
  /** Rows whose field count disagreed with the header. Padded and flagged, not lost. */
  raggedRows: number
}

const DELIMITERS = [',', ';', '\t', '|'] as const

/**
 * Guess the delimiter from the header line by counting candidates outside quotes.
 * Comma wins ties, because a comma-delimited file is the overwhelming prior and a
 * wrong guess is visible immediately in the preview.
 */
function sniffDelimiter(text: string): string {
  const firstLine = text.slice(0, text.indexOf('\n') === -1 ? text.length : text.indexOf('\n'))
  let best = ','
  let bestCount = 0
  for (const candidate of DELIMITERS) {
    let count = 0
    let inQuotes = false
    for (const char of firstLine) {
      if (char === '"') inQuotes = !inQuotes
      else if (char === candidate && !inQuotes) count += 1
    }
    if (count > bestCount) {
      best = candidate
      bestCount = count
    }
  }
  return best
}

/**
 * RFC 4180 with the concessions reality demands: CRLF or LF, optional trailing
 * newline, `""` as an escaped quote inside a quoted field, and a BOM stripped if
 * Excel left one behind.
 */
export function parseCsv(input: string): ParsedCsv {
  const text = input.charCodeAt(0) === 0xfeff ? input.slice(1) : input
  const delimiter = sniffDelimiter(text)

  const records: string[][] = []
  let field = ''
  let record: string[] = []
  let inQuotes = false

  const endField = () => {
    record.push(field)
    field = ''
  }
  const endRecord = () => {
    endField()
    // A blank trailing line is an artefact of the file ending in a newline, not a row.
    if (!(record.length === 1 && record[0] === '')) records.push(record)
    record = []
  }

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i]

    if (inQuotes) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          field += '"'
          i += 1
        } else {
          inQuotes = false
        }
      } else {
        field += char
      }
      continue
    }

    if (char === '"' && field === '') inQuotes = true
    else if (char === delimiter) endField()
    else if (char === '\n') endRecord()
    else if (char === '\r') {
      /* swallowed; the \n that follows ends the record */
    } else field += char
  }
  if (field !== '' || record.length > 0) endRecord()

  const headerRow = records.shift() ?? []
  const headers = headerRow.map((h, index) => {
    const trimmed = h.trim()
    return trimmed === '' ? `column_${index + 1}` : trimmed
  })

  let raggedRows = 0
  const rows = records.map((row) => {
    if (row.length !== headers.length) raggedRows += 1
    const padded = row.slice(0, headers.length)
    while (padded.length < headers.length) padded.push('')
    return padded
  })

  return { headers, rows, delimiter, raggedRows }
}

/** The five fields the engine needs, in the order the mapping UI presents them. */
export const REQUIRED_FIELDS = [
  'txn_id',
  'amount',
  'currency',
  'timestamp',
  'idempotency_key',
] as const

export type FieldName = (typeof REQUIRED_FIELDS)[number]

/**
 * Fields the engine cannot run without.
 *
 * `currency` is absent from this list only because the mapping UI offers a fixed
 * fallback value for it — a gateway export with a single-currency book routinely
 * omits the column. `idempotency_key` is optional too, and its absence has a stated
 * consequence: layer 4 cannot detect duplicates without it.
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
 * and a reconciliation engine that picks one silently will match the wrong day's
 * settlement. Those rows are rejected and counted instead.
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

export interface CoerceResult {
  rows: TxnFacts[]
  /** Rows the engine refused to accept, with the reason, capped for display. */
  rejects: { line: number; reason: string }[]
  rejectedCount: number
}

export interface CoerceOptions {
  side: Side
  map: ColumnMap
  /** Used when the currency column is unmapped. */
  fallbackCurrency: string
  /** Offsets row ids so the two sides never collide. */
  idOffset: number
}

const MAX_REPORTED_REJECTS = 50

/** Turn mapped CSV rows into the facts the engine consumes, counting every refusal. */
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

  const rows: TxnFacts[] = []
  const rejects: { line: number; reason: string }[] = []
  let rejectedCount = 0
  let rowId = options.idOffset

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

    const occurredAt = parseTimestamp(cells[timestampAt] ?? '')
    if (occurredAt === null) {
      return reject(`timestamp "${cells[timestampAt] ?? ''}" is not ISO 8601 or an epoch value`)
    }

    const currency =
      currencyAt === -1
        ? options.fallbackCurrency
        : ((cells[currencyAt] ?? '').trim().toUpperCase() || options.fallbackCurrency)

    rows.push({
      side: options.side,
      rowId: rowId++,
      txnId,
      amount,
      currency,
      occurredAt,
      idempotencyKey: keyAt === -1 ? '' : (cells[keyAt] ?? '').trim(),
    })
  })

  return { rows, rejects, rejectedCount }
}
