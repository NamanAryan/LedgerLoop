/**
 * Routes `/reconcile/test` and `/reconcile/upload` — set the source up and run it.
 *
 * Each source is its own page and its own URL, so the two setups never share a
 * screen and a link can point at either. The switch in the head flips between
 * them by navigating, so the URL stays the only state.
 */

import { Link, useNavigate } from 'react-router-dom'
import { useTenantContext } from '../tenant/TenantContext'
import type { ColumnMap } from '../lib/csv'
import type { GeneratorConfig } from '../lib/generate'
import type { Side } from '../api/types'
import { Alert, PageHead, Segmented } from '../components/primitives'
import { TestDataPanel } from './TestDataControls'
import { UploadPanel, type UploadedFile } from './UploadMapping'

export type Mode = 'test' | 'upload'

const MODES: { value: Mode; label: string; path: string }[] = [
  { value: 'test', label: 'Test data', path: '/reconcile/test' },
  { value: 'upload', label: 'Upload', path: '/reconcile/upload' },
]

export function Reconcile({
  mode,
  generator,
  onGeneratorChange,
  onRunGenerated,
  gateway,
  ledger,
  gatewayMap,
  ledgerMap,
  fallbackCurrency,
  fileError,
  runError,
  running,
  onFile,
  onMapChange,
  onFallbackCurrency,
  onRunUploaded,
}: {
  mode: Mode
  generator: GeneratorConfig
  onGeneratorChange: (next: GeneratorConfig) => void
  onRunGenerated: () => void
  gateway: UploadedFile | null
  ledger: UploadedFile | null
  gatewayMap: ColumnMap
  ledgerMap: ColumnMap
  fallbackCurrency: string
  fileError: Record<Side, string | null>
  runError: string | null
  running: boolean
  onFile: (side: Side, file: File) => void
  onMapChange: (side: Side, map: ColumnMap) => void
  onFallbackCurrency: (value: string) => void
  onRunUploaded: () => void
}) {
  const { mode: tenantMode } = useTenantContext()
  // Hiding the card on /reconcile is not enough — this route has a URL, and a bookmark
  // or a back button reaches it directly. The generator is blocked here, where the
  // writing actually happens, rather than only where it is offered.
  const generatorBlocked = mode === 'test' && tenantMode === 'account'

  const navigate = useNavigate()
  const go = (next: Mode) => {
    const target = MODES.find((option) => option.value === next)
    if (target !== undefined) navigate(target.path)
  }

  return (
    <div className="space-y-10 py-10 pb-28">
      <div className="space-y-6">
        <Link
          to="/reconcile"
          className="inline-flex text-xs font-light text-slate transition-colors duration-300 ease-refined hover:text-cream"
        >
          &larr; Sources
        </Link>
        <PageHead
          title={mode === 'test' ? 'Test data' : 'Upload'}
          action={<Segmented options={MODES} value={mode} onChange={go} />}
        />
      </div>

      {runError !== null && <Alert>{runError}</Alert>}

      {generatorBlocked ? (
        <div className="rounded-lg border border-line p-6 sm:p-10">
          <h2 className="font-display text-xl font-normal tracking-tight text-cream">
            Not available on an account
          </h2>
          <p className="mt-3 max-w-xl text-sm font-light leading-[1.8] text-ash">
            The generator writes synthetic transactions straight into reconciliation stats, and
            there is no delete endpoint — once they are in a real account&rsquo;s figures they
            stay there. Sign out to run it against a sandbox tenant, or use{' '}
            <Link to="/reconcile/upload" className="text-gold hover:opacity-75">
              Upload
            </Link>{' '}
            to bring your own data.
          </p>
        </div>
      ) : mode === 'test' ? (
        <TestDataPanel
          config={generator}
          onChange={onGeneratorChange}
          onRun={onRunGenerated}
          running={running}
        />
      ) : (
        <UploadPanel
          gateway={gateway}
          ledger={ledger}
          gatewayMap={gatewayMap}
          ledgerMap={ledgerMap}
          fallbackCurrency={fallbackCurrency}
          fileError={fileError}
          running={running}
          onFile={onFile}
          onMapChange={onMapChange}
          onFallbackCurrency={onFallbackCurrency}
          onRun={onRunUploaded}
        />
      )}
    </div>
  )
}
