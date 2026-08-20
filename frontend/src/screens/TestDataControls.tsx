/** The synthetic-run panel, shown on `/reconcile?mode=test`. */

import {
  BACKEND_RULES,
  LIMITS,
  clampGeneratorConfig,
  type GeneratorConfig,
} from '../lib/generate'
import { formatCount, formatDuration } from '../format'
import {
  Alert,
  Button,
  Eyebrow,
  Panel,
  PanelBody,
  PanelHead,
  Select,
  SliderField,
  TextInput,
} from '../components/primitives'

const CURRENCIES = ['INR', 'USD', 'EUR', 'GBP'] as const

export function TestDataPanel({
  config,
  onChange,
  onRun,
  running,
}: {
  config: GeneratorConfig
  onChange: (next: GeneratorConfig) => void
  onRun: () => void
  running: boolean
}) {
  const set = <K extends keyof GeneratorConfig>(key: K, value: GeneratorConfig[K]) =>
    onChange(clampGeneratorConfig({ ...config, [key]: value }))

  const pct = (rate: number) => `${(rate * 100).toFixed(1)}%`

  // The projected shape of the run, so the knobs have a consequence before the
  // button is pressed.
  const expectedLedgerRows = Math.round(config.count * (1 - config.dropRate))
  const expectedGatewayRows = config.count + Math.round(config.count * config.duplicateRate)

  // Skew is drawn around the mean with a 30% spread, so the tail crosses the drift
  // window well before the mean does. Saying so beats letting the match rate
  // collapse and look like a bug.
  const skewTail = config.timeSkewMs * 1.9
  const skewWarning =
    skewTail > BACKEND_RULES.driftWindowMs
      ? `Part of this spread falls outside the ${formatDuration(BACKEND_RULES.driftWindowMs)} drift window, so those pairs will come back unmatched.`
      : null

  return (
    <div className="space-y-8">
      <Panel>
        <PanelHead
          title="Stream shape"
          aside={
            <Eyebrow>
              {formatCount(expectedGatewayRows)} gateway ·{' '}
              {formatCount(expectedLedgerRows)} ledger
            </Eyebrow>
          }
        />
        <PanelBody>
          <div className="grid gap-x-12 gap-y-9 sm:grid-cols-2 lg:grid-cols-3">
            <SliderField
              label="Transactions"
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
              value={config.timeSkewMs}
              display={`${formatCount(config.timeSkewMs)} ms`}
              min={LIMITS.timeSkewMs.min}
              max={LIMITS.timeSkewMs.max}
              step={LIMITS.timeSkewMs.step}
              onChange={(value) => set('timeSkewMs', value)}
              disabled={running}
            />

            <div>
              <label htmlFor="seed" className="mb-3 block">
                <Eyebrow>Seed</Eyebrow>
              </label>
              <TextInput
                id="seed"
                type="number"
                value={config.seed}
                disabled={running}
                onChange={(value) => set('seed', Number(value) || 0)}
              />
            </div>

            <div>
              <label htmlFor="currency" className="mb-3 block">
                <Eyebrow>Currency</Eyebrow>
              </label>
              <Select
                id="currency"
                value={config.currency}
                disabled={running}
                onChange={(value) => set('currency', value)}
              >
                {CURRENCIES.map((code) => (
                  <option key={code} value={code}>
                    {code}
                  </option>
                ))}
              </Select>
            </div>
          </div>
        </PanelBody>
      </Panel>

      {skewWarning !== null && <Alert tone="caution">{skewWarning}</Alert>}

      <Button variant="primary" onClick={onRun} disabled={running}>
        {running ? 'Reconciling…' : 'Generate and reconcile'}
      </Button>
    </div>
  )
}
