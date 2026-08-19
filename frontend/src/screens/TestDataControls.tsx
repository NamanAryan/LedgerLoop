/** Screen 2 — synthetic run controls. */

import {
  LIMITS,
  clampGeneratorConfig,
  type GeneratorConfig,
} from '../engine/generate'
import type { MatchConfig } from '../engine/types'
import { formatCount, formatDuration } from '../format'
import { SliderField } from '../components/primitives'

const CURRENCIES = ['INR', 'USD', 'EUR', 'GBP'] as const

export function TestDataControls({
  config,
  onChange,
  match,
  onRun,
  running,
  onBack,
}: {
  config: GeneratorConfig
  onChange: (next: GeneratorConfig) => void
  match: MatchConfig
  onRun: () => void
  running: boolean
  onBack: () => void
}) {
  const set = <K extends keyof GeneratorConfig>(key: K, value: GeneratorConfig[K]) =>
    onChange(clampGeneratorConfig({ ...config, [key]: value }))

  const pct = (rate: number) => `${(rate * 100).toFixed(1)}%`

  // Projected shape of the run, so the knobs have a consequence before the button is
  // pressed. These are expectations, not results — the dashboard reports what the
  // engine actually found.
  const expectedLedgerRows = Math.round(config.count * (1 - config.dropRate))
  const expectedGatewayRows = config.count + Math.round(config.count * config.duplicateRate)

  // Skew is drawn around the mean with a 30% spread, so the tail crosses the drift
  // window well before the mean does. Saying so beats letting the match rate collapse
  // and look like a bug.
  const skewTail = config.timeSkewMs * 1.9
  const skewWarning =
    skewTail > match.driftWindowMs
      ? `Part of this spread lands beyond the ${formatDuration(match.driftWindowMs)} drift window, so those pairs will be reported unmatched on both sides. That is the engine refusing to guess, not a failure.`
      : null

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1 className="page-title">Test data</h1>
          <p className="page-sub">
            Inject defects at known rates, then check the engine's counts against them.
            The seed makes every run reproducible.
          </p>
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onBack}>
          ← Back
        </button>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Stream shape</h2>
          <span className="eyebrow">
            {formatCount(expectedGatewayRows)} gateway · {formatCount(expectedLedgerRows)} ledger
          </span>
        </div>
        <div className="panel-body">
          <div className="controls">
            <SliderField
              label="Transactions"
              hint="Pairs planned before duplicates are added."
              value={config.count}
              display={formatCount(config.count)}
              min={LIMITS.count.min}
              max={LIMITS.count.max}
              step={LIMITS.count.step}
              onChange={(value) => set('count', value)}
              disabled={running}
            />
            <SliderField
              label="Drop rate"
              hint="Gateway records whose ledger entry never arrives."
              value={config.dropRate}
              display={pct(config.dropRate)}
              min={LIMITS.rate.min}
              max={LIMITS.rate.max}
              step={LIMITS.rate.step}
              onChange={(value) => set('dropRate', value)}
              disabled={running}
            />
            <SliderField
              label="Duplicate rate"
              hint="Gateway retries carrying an idempotency key already seen."
              value={config.duplicateRate}
              display={pct(config.duplicateRate)}
              min={LIMITS.rate.min}
              max={LIMITS.rate.max}
              step={LIMITS.rate.step}
              onChange={(value) => set('duplicateRate', value)}
              disabled={running}
            />
            <SliderField
              label="Amount drift rate"
              hint="Ledger amounts off by 0.2–0.8%, inside the 1% tolerance band."
              value={config.driftRate}
              display={pct(config.driftRate)}
              min={LIMITS.rate.min}
              max={LIMITS.rate.max}
              step={LIMITS.rate.step}
              onChange={(value) => set('driftRate', value)}
              disabled={running}
            />
            <SliderField
              label="Time skew"
              hint="Mean clock offset between the two sides, drawn with a 30% spread."
              value={config.timeSkewMs}
              display={`${formatCount(config.timeSkewMs)} ms`}
              min={LIMITS.timeSkewMs.min}
              max={LIMITS.timeSkewMs.max}
              step={LIMITS.timeSkewMs.step}
              onChange={(value) => set('timeSkewMs', value)}
              disabled={running}
            />

            <div className="field">
              <div className="field-head">
                <label className="eyebrow" htmlFor="seed">
                  Seed
                </label>
              </div>
              <input
                id="seed"
                className="text-input"
                type="number"
                value={config.seed}
                disabled={running}
                onChange={(event) => set('seed', Number(event.target.value) || 0)}
              />
              <p className="field-hint">
                Same seed and knobs reproduce the same defects and the same verdicts.
                Timestamps are anchored to the current time, so only they shift between runs.
              </p>
            </div>

            <div className="field">
              <div className="field-head">
                <label className="eyebrow" htmlFor="currency">
                  Currency
                </label>
              </div>
              <select
                id="currency"
                className="select"
                value={config.currency}
                disabled={running}
                onChange={(event) => set('currency', event.target.value)}
              >
                {CURRENCIES.map((code) => (
                  <option key={code} value={code}>
                    {code}
                  </option>
                ))}
              </select>
              <p className="field-hint">Both sides use one currency in generated runs.</p>
            </div>
          </div>

          {skewWarning !== null && (
            <div className="alert alert-warn" style={{ marginTop: 'calc(var(--u) * 5)' }}>
              {skewWarning}
            </div>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Engine settings</h2>
          <span className="eyebrow">fixed for this build</span>
        </div>
        <div className="panel-body">
          <div className="controls">
            <Readout label="Exact window" value={formatDuration(match.exactWindowMs)} hint="Layer 1" />
            <Readout label="Drift window" value={formatDuration(match.driftWindowMs)} hint="Layers 2 and 3" />
            <Readout
              label="Amount tolerance"
              value={`${(match.amountDriftBps / 100).toFixed(2)}%`}
              hint={`or ${(match.amountDriftFloor / 100).toFixed(2)} ${config.currency}, whichever is larger`}
            />
          </div>
        </div>
      </section>

      <div className="actions">
        <button type="button" className="btn btn-primary" onClick={onRun} disabled={running}>
          {running ? 'Reconciling…' : 'Generate & reconcile'}
        </button>
        <span className="field-hint" style={{ margin: 0 }}>
          Runs off the main thread, so the page stays responsive at 50,000 rows.
        </span>
      </div>
    </div>
  )
}

function Readout({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="field">
      <div className="field-head">
        <span className="eyebrow">{label}</span>
        <span className="field-value">{value}</span>
      </div>
      <p className="field-hint">{hint}</p>
    </div>
  )
}
