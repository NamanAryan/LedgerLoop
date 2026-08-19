/**
 * Route `/` — the hero and the way in.
 *
 * Sized to one screen and nothing more. The shell is `min-h-svh` and this fills
 * whatever the header leaves, so there is no height arithmetic to round wrong:
 * the hero takes the slack and the three steps sit on the floor of it. `svh`
 * rather than `vh` so a mobile browser's collapsing address bar cannot push the
 * page into a scroll it was designed not to have.
 */

import { Link } from 'react-router-dom'
import { Numeral } from '../components/primitives'

/**
 * These three are a real sequence: nothing can be matched before it is
 * normalised, and nothing can be reviewed before it is matched. That is the only
 * reason they carry numbers.
 */
const PROCESS = [
  { title: 'Stream ingestion', body: 'Two sources normalised into one shape.' },
  {
    title: 'Multi-layer matching',
    body: 'Exact IDs, time windows, amount tolerance, idempotency keys.',
  },
  { title: 'Exception review', body: 'Every break, with both sides side by side.' },
]

export function Landing() {
  return (
    <div className="flex flex-1 flex-col justify-center gap-14 py-8 sm:gap-20 sm:py-10 short:gap-10 short:py-6">
      <header className="flex flex-col items-center text-center">
        <h1 className="font-display text-[2.4rem] font-light leading-[1.12] tracking-tight text-cream sm:text-5xl lg:text-6xl 2xl:text-7xl short:text-5xl!">
          <span className="block animate-rise" style={{ animationDelay: '80ms' }}>
            Financial clarity,
          </span>
          <span className="block animate-rise italic" style={{ animationDelay: '220ms' }}>
            effortlessly reconciled
          </span>
        </h1>

        <p
          className="animate-rise mt-5 max-w-md text-base font-light leading-[1.7] text-ash sm:mt-6 sm:max-w-lg sm:leading-[1.8] 2xl:max-w-xl 2xl:text-lg short:mt-4"
          style={{ animationDelay: '380ms' }}
        >
          Complete visibility across payment flows. Reconcile at scale, instantly
          detect exceptions.
        </p>

        <Link
          to="/reconcile"
          className="animate-rise mt-8 inline-flex rounded-md bg-gold px-8 py-3 short:mt-6 sm:mt-10 text-sm font-medium tracking-wide text-ink transition-colors duration-300 ease-refined hover:bg-[#e2c257]"
          style={{ animationDelay: '520ms' }}
        >
          Start reconciling
        </Link>
      </header>

      {/* On a phone the three steps collapse to their names on one line each:
          the descriptions are the first thing to go when the page has to stay
          inside one screen, and the sequence still reads. */}
      <section
        className="animate-rise grid gap-4 border-t border-line pt-8 sm:grid-cols-3 sm:gap-px sm:border-t-0 sm:pt-0"
        style={{ animationDelay: '660ms' }}
        aria-label="How reconciliation runs"
      >
        {PROCESS.map((step, index) => (
          <article
            key={step.title}
            className="flex items-baseline gap-3 sm:block sm:border-l sm:border-line sm:px-8 sm:first:border-l-0 sm:first:pl-0 sm:last:pr-0 lg:px-12"
          >
            <Numeral n={index + 1} />
            <div>
              <h2 className="text-base font-normal tracking-tight text-cream sm:mt-3 2xl:text-lg">
                {step.title}
              </h2>
              <p className="hidden text-sm font-light leading-relaxed text-ash sm:mt-1.5 sm:block">
                {step.body}
              </p>
            </div>
          </article>
        ))}
      </section>
    </div>
  )
}
