/**
 * Routing and run state.
 *
 * One route per stage of the job: the pitch on `/`, the choice of source on
 * `/reconcile`, the setup on `/reconcile/test` or `/reconcile/upload`, and the result
 * on `/results`.
 *
 * `/results` is a live view of the backend rather than a render of something this app
 * computed, so a reload no longer has nothing to show — the dashboard just polls. What
 * a reload *does* lose is the ground truth from a synthetic run, which only exists in
 * this tab because the generator recorded it before sending. The dashboard degrades to
 * "no injected-vs-detected panel" rather than to an empty screen.
 */

import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, Link, Route, Routes, useNavigate } from 'react-router-dom'
import { EMPTY_MAP, coerceRows, guessMapping, parseCsv, type ColumnMap } from './lib/csv'
import { getHealth } from './api/client'
import { ingestRows, type IngestProgress, type IngestSummary } from './api/ingest'
import {
  DEFAULT_GENERATOR_CONFIG,
  generate,
  type GeneratorConfig,
  type GroundTruth,
} from './lib/generate'
import type { Side } from './api/types'
import { RunProgress } from './components/RunProgress'
import { Landing } from './screens/Landing'
import { ChooseSource } from './screens/ChooseSource'
import { Reconcile, type Mode } from './screens/Reconcile'
import type { UploadedFile } from './screens/UploadMapping'
import { Dashboard } from './screens/Dashboard'
import { formatCount } from './format'

/** Files this large take long enough to read that the UI must say something. */
const LARGE_FILE_BYTES = 4 * 1024 * 1024

export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  )
}

function Shell() {
  const navigate = useNavigate()

  /**
   * Wake the API as soon as the page opens.
   *
   * A free Render instance suspends after 15 minutes idle and takes roughly a minute
   * to come back. This app is served from a CDN, so it loads instantly and the backend
   * is not touched until the operator actually starts a run — meaning without this the
   * cold start lands *mid-action*, right after they click Reconcile, which reads as the
   * app hanging.
   *
   * Firing it here spends that minute while they are still reading the landing page.
   * Deliberately fire-and-forget: the result is not needed, a failure is not actionable
   * yet, and nothing should block rendering on it. /health touches no datastore, so
   * this is the cheapest possible way to start the clock.
   */
  useEffect(() => {
    void getHealth().catch(() => {
      /* Cold start, offline, or API not deployed yet. The screens that need it report
         their own errors with context this early ping could not give. */
    })
  }, [])

  const [generator, setGenerator] = useState<GeneratorConfig>(DEFAULT_GENERATOR_CONFIG)
  const [progress, setProgress] = useState<IngestProgress | null>(null)
  const [running, setRunning] = useState(false)
  const [summary, setSummary] = useState<IngestSummary | null>(null)
  const [truth, setTruth] = useState<GroundTruth | null>(null)
  const [source, setSource] = useState('')
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

  /** Ship both sides to the API, then hand over to the dashboard's polling. */
  const send = useCallback(
    async (
      gateway: Parameters<typeof ingestRows>[0],
      ledger: Parameters<typeof ingestRows>[1],
      label: string,
      groundTruth: GroundTruth | null,
    ) => {
      setRunning(true)
      setRunError(null)
      setProgress(null)
      setTruth(groundTruth)
      setSource(label)

      try {
        const result = await ingestRows(gateway, ledger, { onProgress: setProgress })
        setSummary(result)
        // A run where nothing landed is a failed run, not an empty dashboard. Saying so
        // here beats sending the operator to a screen of zeroes to work it out.
        if (result.gateway.failed > 0 && result.gateway.accepted === 0 && gateway.length > 0) {
          setRunError(
            `Nothing was accepted. ${result.errors[0] ?? 'The API rejected every row.'}`,
          )
          return
        }
        navigate('/results')
      } catch (caught) {
        setRunError(
          caught instanceof Error
            ? `Ingestion failed: ${caught.message}`
            : 'Ingestion failed against the API.',
        )
      } finally {
        setRunning(false)
        setProgress(null)
      }
    },
    [navigate],
  )

  const startGenerated = useCallback(() => {
    const run = generate(generator)
    void send(
      run.gateway,
      run.ledger,
      `Synthetic · seed ${generator.seed} · run ${run.runId}`,
      run.truth,
    )
  }, [generator, send])

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
      setFiles((current) => ({ ...current, [side]: { name: file.name, size: file.size, parsed } }))
      setMaps((current) => ({ ...current, [side]: guessMapping(parsed.headers) }))
    } catch {
      setFileError((current) => ({
        ...current,
        [side]: 'Could not read that file. It needs to be plain-text CSV.',
      }))
    }
  }, [])

  const startUpload = useCallback(() => {
    const gatewayFile = files.gateway
    const ledgerFile = files.ledger
    if (gatewayFile === null || ledgerFile === null) return

    // No keyPrefix: the derived idempotency key is a pure function of the row's own
    // content, so re-uploading the same file is recognised by the backend as a repeat
    // submission rather than counted twice.
    const gateway = coerceRows(gatewayFile.parsed, {
      side: 'gateway',
      map: maps.gateway,
      fallbackCurrency,
    })
    const ledger = coerceRows(ledgerFile.parsed, {
      side: 'ledger',
      map: maps.ledger,
      fallbackCurrency,
    })

    if (gateway.rows.length === 0 && ledger.rows.length === 0) {
      setRunError(
        'No row in either file survived parsing. Check the amount and timestamp mappings.',
      )
      return
    }

    const rejected = gateway.rejectedCount + ledger.rejectedCount
    const note = rejected > 0 ? ` · ${formatCount(rejected)} rows rejected at parse` : ''
    void send(gateway.rows, ledger.rows, `${gatewayFile.name} + ${ledgerFile.name}${note}`, null)
  }, [files, maps, fallbackCurrency, send])

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
                  onRunGenerated={startGenerated}
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
                  onRunUploaded={startUpload}
                />
              }
            />
          ))}
          <Route
            path="/results"
            element={<Dashboard source={source} truth={truth} summary={summary} />}
          />
          <Route path="*" element={<Landing />} />
        </Routes>
      </main>

      {running && <RunProgress progress={progress} />}
    </div>
  )
}
