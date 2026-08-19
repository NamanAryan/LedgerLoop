/**
 * Routing and run state.
 *
 * One route per stage of the job: the pitch on `/`, the choice of source on
 * `/reconcile`, the setup on `/reconcile/test` or `/reconcile/upload`, and the
 * result on `/results`. The
 * run itself lives here rather than in a route, because /results is a view of
 * something /reconcile produced — a reload has nothing to show, so it sends you
 * back rather than inventing an empty dashboard.
 */

import { useCallback, useState } from 'react'
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useNavigate,
} from 'react-router-dom'
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
import { RunProgress } from './components/RunProgress'
import { Landing } from './screens/Landing'
import { ChooseSource } from './screens/ChooseSource'
import { Reconcile, type Mode } from './screens/Reconcile'
import type { UploadedFile } from './screens/UploadMapping'
import { Dashboard } from './screens/Dashboard'
import { formatCount } from './format'

interface CompletedRun {
  result: ReconResult
  truth: GroundTruth | null
  totalMs: number
  source: string
}

/** Files this large take long enough to read that the UI must say something. */
const LARGE_FILE_BYTES = 4 * 1024 * 1024

/**
 * The engine returns in milliseconds, which is too fast to read. Runs are held
 * open this long so the layer walk in RunProgress is legible. It delays the
 * screen, never the work — the elapsed time on the dashboard stays the measured
 * one.
 */
const MIN_RUN_MS = 1800

function heldOpen<T>(work: Promise<T>, ms: number): Promise<T> {
  const wait = new Promise((resolve) => setTimeout(resolve, ms))
  return Promise.all([work, wait]).then(([value]) => value)
}

export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  )
}

function Shell() {
  const navigate = useNavigate()

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

  const startRun = useCallback(() => {
    setRunning(true)
    setRunError(null)
    setResolutions({})

    heldOpen(runGenerated(generator, DEFAULT_MATCH_CONFIG), MIN_RUN_MS)
      .then((outcome) => {
        setRun({
          result: outcome.result,
          truth: outcome.truth,
          totalMs: outcome.totalMs,
          source: `Synthetic · seed ${generator.seed}`,
        })
        navigate('/results')
      })
      .catch((error: Error) => setRunError(error.message))
      .finally(() => setRunning(false))
  }, [generator, navigate])

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
        'No row in either file survived parsing. Check the amount and timestamp mappings.',
      )
      return
    }

    const rejected = gateway.rejectedCount + ledger.rejectedCount
    const runId = `UP-${Date.now().toString(36).toUpperCase().slice(-5)}`

    heldOpen(runUploaded(gateway.rows, ledger.rows, DEFAULT_MATCH_CONFIG, runId), MIN_RUN_MS)
      .then((outcome) => {
        const rejectNote =
          rejected > 0 ? ` · ${formatCount(rejected)} rows rejected at parse` : ''
        setRun({
          result: outcome.result,
          truth: null,
          totalMs: outcome.totalMs,
          source: `${gatewayFile.name} + ${ledgerFile.name}${rejectNote}`,
        })
        navigate('/results')
      })
      .catch((error: Error) => setRunError(error.message))
      .finally(() => setRunning(false))
  }, [files, maps, fallbackCurrency, navigate])

  return (
    <div className="flex min-h-svh flex-col">
      <header className="border-b border-line">
        <div className="mx-auto flex h-16 w-full max-w-[1600px] items-center px-6 sm:px-8 lg:px-12">
          <Link
            to="/"
            className="font-display text-xl tracking-tight text-cream transition-opacity duration-300 ease-refined hover:opacity-80"
          >
            Ledger<span className="italic text-gold">Loop</span>
          </Link>
        </div>
      </header>

      <main className="mx-auto flex w-full max-w-[1600px] grow flex-col px-6 sm:px-8 lg:px-12">
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/reconcile" element={<ChooseSource />} />
          {(['test', 'upload'] as Mode[]).map((mode) => (
            <Route
              key={mode}
              path={`/reconcile/${mode}`}
              element={
                <Reconcile
                  mode={mode}
                  generator={generator}
                  onGeneratorChange={setGenerator}
                  onRunGenerated={startRun}
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
                  onMapChange={(side, map) =>
                    setMaps((current) => ({ ...current, [side]: map }))
                  }
                  onFallbackCurrency={setFallbackCurrency}
                  onRunUploaded={startUploadRun}
                />
              }
            />
          ))}
          <Route
            path="/results"
            element={
              run === null ? (
                <Navigate to="/reconcile" replace />
              ) : (
                <Dashboard
                  run={run.result}
                  truth={run.truth}
                  totalMs={run.totalMs}
                  source={run.source}
                  resolutions={resolutions}
                  onResolutionChange={(id, next) =>
                    setResolutions((current) => ({ ...current, [id]: next }))
                  }
                />
              )
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>

      {running && <RunProgress durationMs={MIN_RUN_MS} />}
    </div>
  )
}
