import { useCallback, useEffect, useState } from 'react'
import {
  EMPTY_MAP,
  coerceRows,
  guessMapping,
  parseCsv,
  type ColumnMap,
} from './engine/csv'
import { runGenerated, runUploaded } from './engine/client'
import { DEFAULT_GENERATOR_CONFIG, type GeneratorConfig } from './engine/generate'
import { DEFAULT_MATCH_CONFIG, type GroundTruth, type ReconResult, type Side } from './engine/types'
import type { Resolution } from './components/ExceptionDetail'
import { Landing } from './screens/Landing'
import { TestDataControls } from './screens/TestDataControls'
import { UploadMapping, type UploadedFile } from './screens/UploadMapping'
import { Dashboard } from './screens/Dashboard'
import { formatCount } from './format'

type Screen = 'landing' | 'test' | 'upload' | 'dashboard'
type Theme = 'dark' | 'light'

interface CompletedRun {
  result: ReconResult
  truth: GroundTruth | null
  totalMs: number
  source: string
}

/** Files this large take long enough to read that the UI must say something. */
const LARGE_FILE_BYTES = 4 * 1024 * 1024

export default function App() {
  const [screen, setScreen] = useState<Screen>('landing')
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem('ledgerloop-theme')
    return stored === 'light' ? 'light' : 'dark'
  })

  const [generator, setGenerator] = useState<GeneratorConfig>(DEFAULT_GENERATOR_CONFIG)
  const [running, setRunning] = useState(false)
  const [run, setRun] = useState<CompletedRun | null>(null)
  const [resolutions, setResolutions] = useState<Record<number, Resolution>>({})
  const [runError, setRunError] = useState<string | null>(null)

  const [files, setFiles] = useState<Record<Side, UploadedFile | null>>({
    gateway: null,
    ledger: null,
  })
  const [maps, setMaps] = useState<Record<Side, ColumnMap>>({
    gateway: EMPTY_MAP,
    ledger: EMPTY_MAP,
  })
  const [fileError, setFileError] = useState<Record<Side, string | null>>({
    gateway: null,
    ledger: null,
  })
  const [fallbackCurrency, setFallbackCurrency] = useState('INR')

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('ledgerloop-theme', theme)
  }, [theme])

  const startRun = useCallback(() => {
    setRunning(true)
    setRunError(null)
    setResolutions({})

    runGenerated(generator, DEFAULT_MATCH_CONFIG)
      .then((outcome) => {
        setRun({
          result: outcome.result,
          truth: outcome.truth,
          totalMs: outcome.totalMs,
          source: `synthetic · seed ${generator.seed}`,
        })
        setScreen('dashboard')
      })
      .catch((error: Error) => setRunError(error.message))
      .finally(() => setRunning(false))
  }, [generator])

  const handleFile = useCallback(async (side: Side, file: File) => {
    setFileError((current) => ({ ...current, [side]: null }))
    try {
      const text = await file.text()
      const parsed = parseCsv(text)
      if (parsed.headers.length === 0 || parsed.rows.length === 0) {
        setFileError((current) => ({
          ...current,
          [side]: 'That file has a header but no data rows.',
        }))
        return
      }
      setFiles((current) => ({
        ...current,
        [side]: { name: file.name, size: file.size, parsed },
      }))
      setMaps((current) => ({ ...current, [side]: guessMapping(parsed.headers) }))
    } catch {
      setFileError((current) => ({
        ...current,
        [side]: 'Could not read that file. It needs to be plain-text CSV.',
      }))
    }
  }, [])

  const startUploadRun = useCallback(() => {
    const gatewayFile = files.gateway
    const ledgerFile = files.ledger
    if (gatewayFile === null || ledgerFile === null) return

    setRunning(true)
    setRunError(null)
    setResolutions({})

    const gateway = coerceRows(gatewayFile.parsed, {
      side: 'gateway',
      map: maps.gateway,
      fallbackCurrency,
      idOffset: 1,
    })
    // Ledger ids start past the gateway's range so a row id is unique across both
    // sides — the engine reports them, and two rows numbered 7 would be ambiguous.
    const ledger = coerceRows(ledgerFile.parsed, {
      side: 'ledger',
      map: maps.ledger,
      fallbackCurrency,
      idOffset: gateway.rows.length + 1_000_001,
    })

    if (gateway.rows.length === 0 && ledger.rows.length === 0) {
      setRunning(false)
      setRunError(
        'No row in either file survived parsing. Check the column mapping — the amount and timestamp fields are the usual cause.',
      )
      return
    }

    const rejected = gateway.rejectedCount + ledger.rejectedCount
    const runId = `UP-${Date.now().toString(36).toUpperCase().slice(-5)}`

    runUploaded(gateway.rows, ledger.rows, DEFAULT_MATCH_CONFIG, runId)
      .then((outcome) => {
        const rejectNote =
          rejected > 0
            ? ` · ${formatCount(rejected)} rows rejected at parse (${describeRejects([
                ...gateway.rejects,
                ...ledger.rejects,
              ])})`
            : ''
        setRun({
          result: outcome.result,
          truth: null,
          totalMs: outcome.totalMs,
          source: `${gatewayFile.name} + ${ledgerFile.name}${rejectNote}`,
        })
        setScreen('dashboard')
      })
      .catch((error: Error) => setRunError(error.message))
      .finally(() => setRunning(false))
  }, [files, maps, fallbackCurrency])

  const statusLabel = running ? 'running' : run !== null ? 'static run' : 'idle'

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          LedgerLoop
        </span>
        <span className="eyebrow" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className={running || run !== null ? 'status-dot' : 'status-dot idle'} />
          {statusLabel}
        </span>
        <span className="topbar-spacer" />
        <span className="eyebrow">in-browser engine · no server</span>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
        >
          {theme === 'dark' ? 'Light' : 'Dark'}
        </button>
      </header>

      <main className="main">
        {screen === 'landing' && (
          <Landing
            onPickTestData={() => setScreen('test')}
            onPickUpload={() => setScreen('upload')}
          />
        )}

        {screen === 'test' && (
          <>
            {runError !== null && (
              <div className="alert" style={{ marginBottom: 'calc(var(--u) * 5)' }}>
                {runError}
              </div>
            )}
            <TestDataControls
              config={generator}
              onChange={setGenerator}
              match={DEFAULT_MATCH_CONFIG}
              onRun={startRun}
              running={running}
              onBack={() => setScreen('landing')}
            />
          </>
        )}

        {screen === 'upload' && (
          <UploadMapping
            gateway={files.gateway}
            ledger={files.ledger}
            gatewayMap={maps.gateway}
            ledgerMap={maps.ledger}
            fallbackCurrency={fallbackCurrency}
            fileError={fileError}
            runError={runError}
            running={running}
            onFile={(side, file) => {
              if (file.size > LARGE_FILE_BYTES) {
                setFileError((current) => ({ ...current, [side]: 'Reading…' }))
              }
              void handleFile(side, file)
            }}
            onMapChange={(side, map) => setMaps((current) => ({ ...current, [side]: map }))}
            onFallbackCurrency={setFallbackCurrency}
            onRun={startUploadRun}
            onBack={() => setScreen('landing')}
          />
        )}

        {screen === 'dashboard' && run !== null && (
          <Dashboard
            run={run.result}
            truth={run.truth}
            totalMs={run.totalMs}
            source={run.source}
            resolutions={resolutions}
            onResolutionChange={(id, next) =>
              setResolutions((current) => ({ ...current, [id]: next }))
            }
            onNewRun={() => setScreen('landing')}
          />
        )}
      </main>
    </div>
  )
}

/** Summarise parse rejections without listing hundreds of them. */
function describeRejects(rejects: { line: number; reason: string }[]): string {
  if (rejects.length === 0) return 'no detail captured'
  const first = rejects[0]
  return first === undefined ? 'no detail captured' : `first at line ${first.line}: ${first.reason}`
}
