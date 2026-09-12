/**
 * Route `/reconcile` — which of the two sources.
 *
 * The choice gets its own screen because it is the only thing being asked. The
 * cards fill the space rather than sitting in a strip at the top of a form: at
 * this point there is nothing else on the page to compete with them.
 *
 * Three cards now, and the third behaves differently: the two static ones are always
 * available, while the live stream is gated on an account key and renders its own
 * state. It is a component rather than another entry in SOURCES because "locked",
 * "awaiting", "live", "idle" and "rejecting" are not a link.
 */

import { Link } from 'react-router-dom'
import { WebhookCard } from '../components/WebhookCard'
import { useTenantContext } from '../tenant/TenantContext'

const SOURCES = [
  {
    to: '/reconcile/test',
    title: 'Test data',
    body: 'Generate transaction pairs in your browser with defects injected at rates you set.',
    meta: 'Up to 50,000 rows · reproducible by seed',
    cta: 'Configure a run',
    /** Synthetic rows written to a real account cannot be removed — there is no delete
     *  endpoint — so this is unavailable once a key is active. */
    sandboxOnly: true,
  },
  {
    to: '/reconcile/upload',
    title: 'Upload',
    body: 'Bring a gateway CSV and a ledger CSV, then map your columns to the five fields the engine needs.',
    meta: 'CSV · read in this tab, never uploaded',
    cta: 'Choose files',
    sandboxOnly: false,
  },
]

export function ChooseSource({ onOpenKeyEntry }: { onOpenKeyEntry: () => void }) {
  const { mode } = useTenantContext()

  return (
    <div className="flex flex-1 flex-col justify-center gap-10 py-10 short:gap-6 short:py-6">
      <h1 className="font-display text-4xl font-light tracking-tight text-cream sm:text-5xl">
        Reconcile
      </h1>

      <div className="grid flex-1 gap-6 grid-cols-1 md:grid-cols-3">
        {SOURCES.map((source) => {
          const locked = source.sandboxOnly && mode === 'account'
          if (locked) {
            return (
              <div
                key={source.to}
                aria-disabled="true"
                className="flex min-h-48 flex-col justify-between rounded-lg border border-line p-6 opacity-60 sm:p-10"
              >
                <div>
                  <h2 className="font-display text-2xl font-normal tracking-tight text-cream sm:text-3xl">
                    {source.title}
                  </h2>
                  {/* Hard-disabled rather than warned about. There is no delete
                      endpoint, so synthetic rows written into a real account's stats
                      are permanent — an undoable mistake behind a confirm dialog is
                      still an undoable mistake. */}
                  <p className="mt-3 max-w-sm text-sm font-light leading-[1.7] text-ash sm:mt-4 sm:leading-[1.8]">
                    Unavailable while an account key is active. Synthetic transactions written
                    to a real account cannot be removed.
                  </p>
                </div>
                <div className="mt-6 border-t border-line pt-4 sm:mt-8 sm:pt-5">
                  <p className="text-[11px] font-light text-slate">
                    Sign out to generate test data
                  </p>
                </div>
              </div>
            )
          }

          return (
          <Link
            key={source.to}
            to={source.to}
            className="group flex min-h-48 flex-col justify-between rounded-lg border border-line p-6 transition-colors duration-300 ease-refined hover:border-gold/50 sm:p-10"
          >
            <div>
              <h2 className="font-display text-2xl font-normal tracking-tight text-cream sm:text-3xl">
                {source.title}
              </h2>
              <p className="mt-3 max-w-sm text-sm font-light leading-[1.7] text-ash sm:mt-4 sm:leading-[1.8]">
                {source.body}
              </p>
            </div>
            <div className="mt-6 border-t border-line pt-4 sm:mt-8 sm:pt-5">
              <p className="text-[11px] font-light text-slate">{source.meta}</p>
              <span className="mt-3 inline-flex items-center gap-2 text-xs font-medium uppercase tracking-[0.18em] text-gold">
                {source.cta}
                <span
                  aria-hidden="true"
                  className="transition-transform duration-300 ease-refined group-hover:translate-x-1.5"
                >
                  &rarr;
                </span>
              </span>
            </div>
          </Link>
          )
        })}

        <WebhookCard onOpenKeyEntry={onOpenKeyEntry} />
      </div>
    </div>
  )
}
