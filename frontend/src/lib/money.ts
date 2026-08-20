/**
 * Money, in integer minor units. Never a float.
 *
 * This file used to carry the tolerance arithmetic for layer 3. It does not any more
 * — the engine is the backend's, and comparing amounts is its job. What is left is
 * the edge: parsing a CSV cell into an exact integer, serialising that integer into
 * the decimal string the API expects, and rendering a value for a human.
 *
 * The discipline survives the move because the reason for it did. `parseMinor` is
 * string-based rather than `Math.round(parseFloat(s) * 100)`, which is correct for
 * almost every input and silently wrong for a few (`8.115` -> 811, not 812), and
 * "almost every" is not a standard a ledger can be held to. An amount is parsed once,
 * held as an integer, and serialised once. It is never arithmetic in between.
 */

/** An amount in minor units. 2100.00 INR is 210_000. */
export type Minor = number

/**
 * Parse a decimal string ("2100.50", "-3.4", "1,204.00") into minor units.
 *
 * Returns null when the input is not a number this function is willing to guess at —
 * the caller counts that as a rejected row rather than substituting a zero.
 */
export function parseMinor(raw: string, scale = 2): Minor | null {
  const text = raw.trim().replace(/[,\s]/g, '')
  if (text === '') return null

  const match = /^([+-]?)(\d*)(?:\.(\d*))?$/.exec(text)
  if (!match) return null

  const sign = match[1] ?? ''
  const whole = match[2] ?? ''
  const frac = match[3] ?? ''
  if (whole === '' && frac === '') return null

  // Pad or truncate the fraction to the currency's scale. Truncating past the scale is
  // a deliberate choice over rounding: an export carrying more precision than the
  // currency has is a data problem, and rounding it would manufacture a match out of a
  // real discrepancy.
  const padded = (frac + '0'.repeat(scale)).slice(0, scale)
  const magnitude = Number(whole || '0') * 10 ** scale + Number(padded || '0')
  if (!Number.isSafeInteger(magnitude)) return null

  return sign === '-' ? -magnitude : magnitude
}

/**
 * Minor units -> the decimal string the API takes: `210000` -> `"2100.00"`.
 *
 * The wire format is a string on purpose. The backend column is `numeric(18,2)`, and
 * a JSON number would be parsed into a float on the way in — reintroducing the binary
 * error the schema exists to prevent, in the one place nobody would look for it.
 * Always exactly `scale` decimal places, because the API validates `decimal_places`
 * and answers a third one with a 422 rather than rounding someone's money silently.
 */
export function toDecimalString(value: Minor, scale = 2): string {
  const negative = value < 0
  const digits = Math.abs(value).toString().padStart(scale + 1, '0')
  const whole = digits.slice(0, digits.length - scale)
  const frac = digits.slice(digits.length - scale)
  return `${negative ? '-' : ''}${whole}.${frac}`
}

/** Render minor units as a grouped decimal string. No currency mark. */
export function formatMinor(value: Minor, scale = 2): string {
  const negative = value < 0
  const digits = Math.abs(value).toString().padStart(scale + 1, '0')
  const whole = digits.slice(0, digits.length - scale)
  const frac = digits.slice(digits.length - scale)
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${negative ? '-' : ''}${grouped}.${frac}`
}

const SYMBOLS: Record<string, string> = {
  INR: '₹',
  USD: '$',
  EUR: '€',
  GBP: '£',
  JPY: '¥',
}

/** Display form with a currency mark. Falls back to a trailing ISO code. */
export function formatMoney(value: Minor, currency: string, scale = 2): string {
  const symbol = SYMBOLS[currency.toUpperCase()]
  return symbol
    ? `${symbol}${formatMinor(value, scale)}`
    : `${formatMinor(value, scale)} ${currency}`
}

/**
 * Format an amount that arrived from the API as a decimal string.
 *
 * Goes through `parseMinor` rather than `Number()` so the rendered figure is the one
 * the database holds, digit for digit. An unparseable value is shown verbatim instead
 * of as `NaN` — if the backend ever sends something unexpected, the operator should
 * see what it actually said.
 */
export function formatAmount(
  decimal: string | null | undefined,
  currency: string | null | undefined,
): string {
  if (decimal === null || decimal === undefined || decimal === '') return '—'
  const minor = parseMinor(decimal)
  if (minor === null) return decimal
  return formatMoney(minor, currency ?? '')
}

/**
 * Signed difference between two API decimal strings, for the drift column.
 * Null when either side is absent, which is what a one-sided break is.
 */
export function amountDelta(
  gateway: string | null | undefined,
  ledger: string | null | undefined,
): Minor | null {
  if (!gateway || !ledger) return null
  const a = parseMinor(gateway)
  const b = parseMinor(ledger)
  if (a === null || b === null) return null
  return a - b
}
