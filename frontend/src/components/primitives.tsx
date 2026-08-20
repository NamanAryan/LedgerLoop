/**
 * The shared vocabulary: surfaces, actions, labels, inputs.
 *
 * Everything here is Tailwind utilities on a plain element. Repeated treatments
 * live in a component rather than a CSS class, so there is exactly one place to
 * change how a panel or a button looks, and no stylesheet to keep in sync.
 */

import type { ReactNode } from 'react'
import type { ReconStatus } from '../api/types'

/* --- labels --------------------------------------------------------------- */

/** Section eyebrow. Wide-tracked and small; it names things, never says them. */
export function Eyebrow({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <span
      className={`text-[10px] font-medium uppercase tracking-[0.22em] text-slate ${className}`}
    >
      {children}
    </span>
  )
}

/** The gold italic numeral. Used only where the order is real information. */
export function Numeral({ n, className = '' }: { n: number; className?: string }) {
  return (
    <span
      className={`font-display text-lg font-light italic text-gold ${className}`}
      aria-hidden="true"
    >
      {String(n).padStart(2, '0')}
    </span>
  )
}

/* --- surfaces ------------------------------------------------------------- */

export function Panel({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`rounded-lg border border-line ${className}`}>
      {children}
    </section>
  )
}

export function PanelHead({ title, aside }: { title: string; aside?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-4 border-b border-line px-6 py-5 sm:px-8">
      <h2 className="text-xl font-normal tracking-tight text-cream">{title}</h2>
      {aside !== undefined && <div className="text-right">{aside}</div>}
    </div>
  )
}

export function PanelBody({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}) {
  return <div className={`px-6 py-6 sm:px-8 sm:py-7 ${className}`}>{children}</div>
}

/* --- actions -------------------------------------------------------------- */

type Variant = 'primary' | 'outline' | 'ghost'

const VARIANTS: Record<Variant, string> = {
  primary:
    'bg-gold text-ink border border-gold hover:bg-[#e2c257] hover:border-[#e2c257] disabled:bg-line disabled:border-line disabled:text-slate',
  outline:
    'border border-line-2 text-cream hover:border-gold/70 hover:text-gold disabled:border-line disabled:text-slate',
  ghost: 'border border-transparent text-ash hover:text-cream disabled:text-slate',
}

export function Button({
  children,
  onClick,
  variant = 'outline',
  size = 'md',
  disabled,
  className = '',
}: {
  children: ReactNode
  onClick?: () => void
  variant?: Variant
  size?: 'sm' | 'md'
  disabled?: boolean
  className?: string
}) {
  const sizing = size === 'sm' ? 'px-4 py-1.5 text-xs' : 'px-7 py-2.5 text-sm'
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center gap-2 rounded-md font-medium tracking-wide transition-all duration-300 ease-refined disabled:cursor-not-allowed ${sizing} ${VARIANTS[variant]} ${className}`}
    >
      {children}
    </button>
  )
}

/** Two mutually exclusive ways to start a run, shown as one control. */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[]
  value: T
  onChange: (next: T) => void
}) {
  return (
    <div className="inline-flex rounded-md border border-line p-1">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className={`rounded px-4 py-1.5 text-xs font-medium tracking-wide transition-colors duration-300 ease-refined ${
            value === option.value ? 'bg-ink-3 text-cream' : 'text-slate hover:text-ash'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

/* --- status --------------------------------------------------------------- */

const PILL: Record<ReconStatus, { label: string; tone: string }> = {
  matched: { label: 'Matched', tone: 'text-sage border-sage/30 bg-sage/10' },
  amount_drift: { label: 'Drift', tone: 'text-rose border-rose/30 bg-rose/10' },
  duplicate: { label: 'Duplicate', tone: 'text-gold border-gold/30 bg-gold/10' },
  unmatched_gateway_only: {
    label: 'Gateway only',
    tone: 'text-rose border-rose/30 bg-rose/10',
  },
  unmatched_ledger_only: {
    label: 'Ledger only',
    tone: 'text-rose border-rose/30 bg-rose/10',
  },
  // Reserved in the backend's enum but never written today: layer 2 resolves to
  // `matched` with match_layer=time_drift, because a payment that reconciles 40s late
  // is still a reconciled payment. The pill exists so that if the server's policy ever
  // flips, the UI renders it instead of crashing on an unmapped status.
  time_drift: { label: 'Time drift', tone: 'text-gold border-gold/30 bg-gold/10' },
}

const UNKNOWN_PILL = { label: 'Unknown', tone: 'text-slate border-line-2 bg-ink-2' }

export function StatusPill({ status }: { status: ReconStatus }) {
  // Indexed defensively: a status this build has never heard of should render as
  // itself, not throw. The server owns this enum and can add to it before we redeploy.
  const { label, tone } = PILL[status] ?? { ...UNKNOWN_PILL, label: status }
  return (
    <span
      className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em] ${tone}`}
    >
      {label}
    </span>
  )
}

export function Alert({
  children,
  tone = 'error',
  className = '',
}: {
  children: ReactNode
  tone?: 'error' | 'caution'
  className?: string
}) {
  const styles =
    tone === 'error'
      ? 'border-rose/40 bg-rose/10 text-rose'
      : 'border-gold/40 bg-gold/10 text-gold'
  return (
    <div className={`rounded-lg border px-5 py-4 text-sm leading-relaxed ${styles} ${className}`}>
      {children}
    </div>
  )
}

/* --- metric --------------------------------------------------------------- */

export function MetricTile({
  label,
  value,
  secondary,
}: {
  label: string
  value: ReactNode
  secondary?: ReactNode
}) {
  return (
    <div className="px-6 py-7 sm:px-8">
      <div className="font-display text-4xl font-light leading-none tracking-tight text-cream">
        {value}
      </div>
      <div className="mt-4">
        <Eyebrow>{label}</Eyebrow>
      </div>
      {secondary !== undefined && (
        <p className="mt-1.5 text-xs font-light leading-relaxed text-slate">{secondary}</p>
      )}
    </div>
  )
}

/* --- form controls -------------------------------------------------------- */

const FIELD_BASE =
  'w-full rounded-md border border-line-2 bg-ink px-3.5 py-2.5 text-sm font-light text-cream transition-colors duration-300 ease-refined focus:border-gold/70 focus:outline-none disabled:text-slate'

export function Select({
  id,
  value,
  onChange,
  disabled,
  className = '',
  children,
}: {
  id?: string
  value: string
  onChange: (value: string) => void
  disabled?: boolean
  className?: string
  children: ReactNode
}) {
  return (
    <div className={`relative ${className}`}>
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className={`${FIELD_BASE} appearance-none pr-10`}
      >
        {children}
      </select>
      <svg
        aria-hidden="true"
        viewBox="0 0 10 6"
        className="pointer-events-none absolute right-3.5 top-1/2 w-2.5 -translate-y-1/2 fill-none stroke-ash stroke-[1.2]"
      >
        <path d="M1 1l4 4 4-4" />
      </svg>
    </div>
  )
}

export function TextInput({
  id,
  type = 'text',
  value,
  onChange,
  disabled,
  className = '',
}: {
  id?: string
  type?: string
  value: string | number
  onChange: (value: string) => void
  disabled?: boolean
  className?: string
}) {
  return (
    <input
      id={id}
      type={type}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      className={`${FIELD_BASE} ${className}`}
    />
  )
}

/**
 * The slider track is a hairline and the thumb a small gold dot — the same
 * accent the numerals use, so the value being dragged reads as the one live
 * thing on the panel. Vendor pseudo-elements are reached through Tailwind
 * arbitrary variants rather than a stylesheet.
 */
const RANGE = [
  'w-full h-4 cursor-pointer appearance-none bg-transparent disabled:cursor-not-allowed',
  '[&::-webkit-slider-runnable-track]:h-px [&::-webkit-slider-runnable-track]:bg-line-2',
  '[&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:-mt-[5.5px]',
  '[&::-webkit-slider-thumb]:size-3 [&::-webkit-slider-thumb]:rounded-full',
  '[&::-webkit-slider-thumb]:bg-gold [&::-webkit-slider-thumb]:transition-transform',
  '[&::-webkit-slider-thumb]:duration-200 hover:[&::-webkit-slider-thumb]:scale-125',
  '[&::-moz-range-track]:h-px [&::-moz-range-track]:bg-line-2',
  '[&::-moz-range-thumb]:size-3 [&::-moz-range-thumb]:rounded-full',
  '[&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-gold',
  'disabled:[&::-webkit-slider-thumb]:bg-line-2 disabled:[&::-moz-range-thumb]:bg-line-2',
].join(' ')

export function SliderField({
  label,
  value,
  display,
  min,
  max,
  step,
  onChange,
  disabled,
}: {
  label: string
  value: number
  display: string
  min: number
  max: number
  step: number
  onChange: (value: number) => void
  disabled?: boolean
}) {
  const id = `slider-${label.replace(/\W+/g, '-').toLowerCase()}`
  return (
    <div>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <label htmlFor={id}>
          <Eyebrow>{label}</Eyebrow>
        </label>
        <span className="font-display text-lg font-light leading-none text-cream">
          {display}
        </span>
      </div>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(Number(event.target.value))}
        className={RANGE}
      />
    </div>
  )
}

/* --- page furniture ------------------------------------------------------- */

export function PageHead({
  eyebrow,
  title,
  action,
}: {
  eyebrow?: string
  title: string
  action?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-6">
      <div>
        {eyebrow !== undefined && <Eyebrow>{eyebrow}</Eyebrow>}
        <h1 className="mt-3 font-display text-4xl font-light tracking-tight text-cream sm:text-5xl">
          {title}
        </h1>
      </div>
      {action}
    </div>
  )
}
