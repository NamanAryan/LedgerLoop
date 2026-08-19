/**
 * Money, in integer minor units. Never a float.
 *
 * The backend uses `Decimal`, and its comment on why is worth repeating here:
 * `Decimal(0.01)` inherits the binary error of the float literal, which is exactly
 * the class of bug a reconciliation engine exists to catch. JavaScript has no
 * Decimal, so the equivalent discipline is to hold every amount as an integer
 * count of minor units (paise, cents) and never let a float touch it after parsing.
 *
 * The one place a percentage is unavoidable is layer 3's tolerance. Rather than
 * multiply by 0.01 and hope, the percentage is carried as integer basis points and
 * the comparison is scaled up so both sides stay integral. See `withinTolerance`.
 */

/** An amount in minor units. 2100.00 INR is 210_000. */
export type Minor = number

/** 1% == 100 basis points. */
export const BPS_DIVISOR = 10_000

/**
 * Parse a decimal string ("2100.50", "-3.4", "1,204.00") into minor units.
 *
 * Deliberately string-based: `Math.round(parseFloat(s) * 100)` is correct for
 * almost every input and silently wrong for a few (8.115 -> 811, not 812), and
 * "almost every" is not a standard a ledger can be held to.
 *
 * Returns null when the input is not a number this function is willing to guess at.
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

  // Pad or truncate the fraction to the currency's scale. Truncating past the scale
  // is a deliberate choice over rounding: an export carrying more precision than the
  // currency has is a data problem, and rounding it would manufacture a match out of
  // a real discrepancy.
  const padded = (frac + '0'.repeat(scale)).slice(0, scale)
  const magnitude = Number(whole || '0') * 10 ** scale + Number(padded || '0')
  if (!Number.isSafeInteger(magnitude)) return null

  return sign === '-' ? -magnitude : magnitude
}

/** Render minor units back to a grouped decimal string. No currency mark. */
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
 * Layer 3's allowance: `max(pct * |reference|, floor)`, per the engine spec.
 *
 * The percentage covers FX and rounding on large tickets; the flat floor covers
 * small ones, where 1% of 50 is 0.50 and would flag every trivial fee difference.
 *
 * Both arms are integers scaled by BPS_DIVISOR so the comparison stays exact. The
 * caller compares against a likewise-scaled difference, which is why this returns a
 * scaled value rather than a real amount.
 */
export function toleranceScaled(reference: Minor, pctBps: number, floor: Minor): number {
  return Math.max(Math.abs(reference) * pctBps, floor * BPS_DIVISOR)
}

/** `|gateway - ledger| <= tolerance`, evaluated entirely in integers. */
export function withinTolerance(
  gateway: Minor,
  ledger: Minor,
  pctBps: number,
  floor: Minor,
): boolean {
  return Math.abs(gateway - ledger) * BPS_DIVISOR <= toleranceScaled(gateway, pctBps, floor)
}

/** The tolerance as a real amount, for display only. Rounds down; never decides. */
export function toleranceDisplay(reference: Minor, pctBps: number, floor: Minor): Minor {
  return Math.floor(toleranceScaled(reference, pctBps, floor) / BPS_DIVISOR)
}
