/**
 * Display formatting.
 *
 * Everything with a clock in it renders in UTC. A reconciliation run that shows one
 * set of timestamps in Mumbai and another in New York is a support ticket waiting to
 * happen, and the two sides of a break are frequently read by people in different
 * places. The zone is stated in the UI rather than assumed.
 */

const pad = (value: number, width = 2) => value.toString().padStart(width, '0')

/** `2026-08-19 14:32:07.412` — UTC, sortable, no locale. */
export function formatTimestamp(ms: number): string {
  const d = new Date(ms)
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}.` +
    `${pad(d.getUTCMilliseconds(), 3)}`
  )
}

/** Just the clock portion, for dense table columns. */
export function formatClock(ms: number): string {
  const d = new Date(ms)
  return (
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}.` +
    `${pad(d.getUTCMilliseconds(), 3)}`
  )
}

/** Signed offset, in whatever unit keeps it readable. */
export function formatSkew(ms: number): string {
  const sign = ms >= 0 ? '+' : '-'
  const abs = Math.abs(ms)
  if (abs < 1000) return `${sign}${abs}ms`
  if (abs < 60_000) return `${sign}${(abs / 1000).toFixed(2)}s`
  return `${sign}${(abs / 60_000).toFixed(1)}m`
}

/** Unsigned duration, for windows and elapsed time. */
export function formatDuration(ms: number): string {
  if (ms < 1) return `${ms.toFixed(2)}ms`
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export function formatCount(value: number): string {
  return value.toLocaleString('en-US')
}

export function formatPercent(fraction: number, digits = 2): string {
  return `${(fraction * 100).toFixed(digits)}%`
}

/** Rows per second, rounded to something an operator would actually say aloud. */
export function formatThroughput(rows: number, ms: number): string {
  if (ms <= 0) return '—'
  const perSecond = rows / (ms / 1000)
  if (perSecond >= 1_000_000) return `${(perSecond / 1_000_000).toFixed(1)}M rows/s`
  if (perSecond >= 1_000) return `${Math.round(perSecond / 1000)}k rows/s`
  return `${Math.round(perSecond)} rows/s`
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
