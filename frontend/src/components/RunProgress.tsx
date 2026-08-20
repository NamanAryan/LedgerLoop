/**
 * The ingest overlay.
 *
 * This used to animate a fake walk through the five layers, held open by an artificial
 * minimum duration, because the in-browser engine finished faster than a human could
 * read. Both the fake and the delay are gone: there is real work happening now, it
 * takes real time, and the bar reports it.
 *
 * What it reports is **ingestion**, not reconciliation — the two are genuinely
 * different events and conflating them would be the same lie in a new costume. The
 * ingestion endpoints answer 202: the row is durable and queued, not yet matched.
 * Matching happens in the backend afterwards, on its own clock, and the dashboard
 * shows it converging. So this overlay ends when the last row is accepted, and the
 * dashboard picks up the story from there.
 */

import type { IngestProgress } from '../api/ingest'
import { formatCount } from '../format'

const PHASES = [
  { key: 'ledger' as const, label: 'Ledger entries', detail: 'batched, 1000 per request' },
  { key: 'gateway' as const, label: 'Gateway webhooks', detail: 'one request each' },
]

export function RunProgress({ progress }: { progress: IngestProgress | null }) {
  const activeIndex = progress === null ? 0 : PHASES.findIndex((p) => p.key === progress.phase)
  const fraction =
    progress === null || progress.total === 0 ? 0 : progress.completed / progress.total

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink px-6"
      role="status"
      aria-live="polite"
    >
      <div className="w-full max-w-sm">
        <h2 className="text-center font-display text-3xl font-light italic text-cream">
          Sending to the engine
        </h2>

        <ol className="mt-10 space-y-3">
          {PHASES.map((phase, index) => {
            const done = activeIndex > index
            const active = activeIndex === index
            return (
              <li
                key={phase.key}
                className={`flex items-baseline gap-3 text-sm transition-colors duration-500 ease-refined ${
                  done ? 'text-ash' : active ? 'text-cream' : 'text-slate/50'
                }`}
              >
                <span
                  className={`size-1.5 shrink-0 translate-y-[-2px] rounded-full transition-colors duration-500 ease-refined ${
                    done ? 'bg-gold' : active ? 'bg-gold animate-pulse' : 'bg-line-2'
                  }`}
                />
                <span className="font-light">{phase.label}</span>
                <span className="ml-auto text-xs text-slate">{phase.detail}</span>
              </li>
            )
          })}
        </ol>

        <div className="mt-10 h-px w-full overflow-hidden bg-line">
          <span
            className="block h-full bg-gold transition-[width] duration-300 ease-refined"
            style={{ width: `${Math.min(fraction * 100, 100)}%` }}
          />
        </div>

        {progress !== null && (
          <div className="mt-4 flex items-baseline justify-between text-xs font-light text-slate">
            <span>
              {formatCount(progress.completed)} of {formatCount(progress.total)}
            </span>
            <span>
              {/* Duplicates are called out rather than folded into a failure count:
                  a 202 with duplicate:true is the idempotency layer working. */}
              {progress.duplicates > 0 && `${formatCount(progress.duplicates)} duplicate · `}
              {progress.failed > 0 && (
                <span className="text-rose">{formatCount(progress.failed)} failed</span>
              )}
            </span>
          </div>
        )}

        <p className="mt-8 text-center text-xs font-light leading-relaxed text-slate">
          Every row is accepted with a 202 — stored and queued. Matching runs in the
          backend and the dashboard will converge as it works.
        </p>
      </div>
    </div>
  )
}
