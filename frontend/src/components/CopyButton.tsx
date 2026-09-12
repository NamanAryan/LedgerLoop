/**
 * Copy to clipboard, with both outcomes visible.
 *
 * The failure state is not decoration. `navigator.clipboard` is unavailable on any
 * page not served over HTTPS or localhost, and is refused outright by some browsers
 * without a user-gesture heuristic being satisfied — so a button that only ever shows
 * a checkmark will confidently claim to have copied a webhook URL that is not on the
 * clipboard. Here a rejection says so and the text stays selectable beside it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

type CopyState = 'idle' | 'copied' | 'failed'

const RESET_MS = 2000

export function CopyButton({
  value,
  label = 'Copy',
  className = '',
}: {
  value: string
  label?: string
  className?: string
}) {
  const [state, setState] = useState<CopyState>('idle')
  const timer = useRef<number | undefined>(undefined)

  // Clearing on unmount matters: the modal that hosts this can close inside the two
  // seconds, and setting state on a gone component is a warning at best.
  useEffect(() => () => window.clearTimeout(timer.current), [])

  const copy = useCallback(async () => {
    window.clearTimeout(timer.current)
    try {
      if (navigator.clipboard === undefined) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(value)
      setState('copied')
    } catch {
      // Insecure origin, a permissions policy, or a browser that wants a closer
      // gesture. Either way the honest answer is "not copied — select it yourself".
      setState('failed')
    }
    timer.current = window.setTimeout(() => setState('idle'), RESET_MS)
  }, [value])

  const tone =
    state === 'copied'
      ? 'border-sage/50 text-sage'
      : state === 'failed'
        ? 'border-rose/50 text-rose'
        : 'border-line-2 text-ash hover:border-gold/70 hover:text-gold'

  return (
    <button
      type="button"
      onClick={() => void copy()}
      // Announced to a screen reader, which otherwise gets no signal at all from a
      // label that changes silently.
      aria-live="polite"
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium tracking-wide transition-colors duration-300 ease-refined ${tone} ${className}`}
    >
      {state === 'copied' && <span aria-hidden="true">&#10003;</span>}
      {state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : label}
    </button>
  )
}
