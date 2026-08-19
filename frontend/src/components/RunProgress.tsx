/**
 * The run overlay.
 *
 * The engine finishes a 50,000-row book in a few milliseconds, so there is no
 * real progress to report — a truthful bar would be a single flash. This walks
 * the five layers in the order they actually run, held open by the minimum
 * duration in App, so the operator sees what the engine does instead of a
 * spinner that says nothing. The elapsed time on the dashboard is the measured
 * one; this is pacing, not measurement.
 */

import { useEffect, useState } from 'react'

const LAYERS = [
  'Exact',
  'Time drift',
  'Amount drift',
  'Duplicate',
  'Sweep',
] as const

export function RunProgress({ durationMs }: { durationMs: number }) {
  const [stage, setStage] = useState(0)

  useEffect(() => {
    const step = durationMs / LAYERS.length
    const timers = LAYERS.map((_, index) =>
      window.setTimeout(() => setStage(index + 1), step * (index + 1)),
    )
    return () => timers.forEach(window.clearTimeout)
  }, [durationMs])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink px-6"
      role="status"
      aria-live="polite"
    >
      <div className="w-full max-w-xs">
        <h2 className="text-center font-display text-3xl font-light italic text-cream">
          Reconciling
        </h2>

        <ol className="mt-10 space-y-3">
          {LAYERS.map((layer, index) => {
            const done = stage > index
            const active = stage === index
            return (
              <li
                key={layer}
                className={`flex items-center gap-3 text-sm transition-colors duration-500 ease-refined ${
                  done ? 'text-ash' : active ? 'text-cream' : 'text-slate/50'
                }`}
              >
                <span
                  className={`size-1.5 shrink-0 rounded-full transition-colors duration-500 ease-refined ${
                    done ? 'bg-gold' : active ? 'bg-gold animate-pulse' : 'bg-line-2'
                  }`}
                />
                <span className="font-light">{layer}</span>
              </li>
            )
          })}
        </ol>

        <div className="mt-10 h-px w-full overflow-hidden bg-line">
          <span
            className="block h-full bg-gold transition-[width] duration-500 ease-refined"
            style={{ width: `${(stage / LAYERS.length) * 100}%` }}
          />
        </div>
      </div>
    </div>
  )
}
