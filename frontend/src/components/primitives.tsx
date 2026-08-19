/** Small shared pieces: status pills, metric tiles, sliders. */

import type { ReactNode } from 'react'
import type { ReconStatus } from '../engine/types'

/* --- status pill ---------------------------------------------------------- */

const PILL_COPY: Record<ReconStatus, { label: string; className: string }> = {
  matched: { label: 'Matched', className: 'pill-matched' },
  amount_drift: { label: 'Drift', className: 'pill-drift' },
  duplicate: { label: 'Duplicate', className: 'pill-duplicate' },
  unmatched_gateway_only: { label: 'Gateway only', className: 'pill-unmatched' },
  unmatched_ledger_only: { label: 'Ledger only', className: 'pill-unmatched' },
}

export function StatusPill({ status }: { status: ReconStatus }) {
  const { label, className } = PILL_COPY[status]
  return <span className={`pill ${className}`}>{label}</span>
}

/* --- metric tile ---------------------------------------------------------- */

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
    <div className="tile">
      <div className="tile-value">{value}</div>
      <div className="eyebrow">{label}</div>
      {secondary !== undefined && <div className="tile-secondary">{secondary}</div>}
    </div>
  )
}

/* --- slider field --------------------------------------------------------- */

export function SliderField({
  label,
  hint,
  value,
  display,
  min,
  max,
  step,
  onChange,
  disabled,
}: {
  label: string
  hint: string
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
    <div className="field">
      <div className="field-head">
        <label className="eyebrow" htmlFor={id}>
          {label}
        </label>
        <span className="field-value">{display}</span>
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
      />
      <p className="field-hint">{hint}</p>
    </div>
  )
}
