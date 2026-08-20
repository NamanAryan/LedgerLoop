/** The upload-and-map panel, shown on `/reconcile?mode=upload`. */

import { useState } from 'react'
import {
  HARD_REQUIRED,
  REQUIRED_FIELDS,
  validationErrors,
  type ColumnMap,
  type FieldName,
  type ParsedCsv,
} from '../lib/csv'
import type { Side } from '../api/types'
import { formatBytes, formatCount } from '../format'
import {
  Alert,
  Button,
  Eyebrow,
  Panel,
  PanelBody,
  PanelHead,
  Select,
} from '../components/primitives'

export interface UploadedFile {
  name: string
  size: number
  parsed: ParsedCsv
}

const FIELD_LABELS: Record<FieldName, string> = {
  txn_id: 'Transaction ID',
  amount: 'Amount',
  currency: 'Currency',
  timestamp: 'Timestamp',
  idempotency_key: 'Idempotency key',
}

function Dropzone({
  side,
  label,
  file,
  onFile,
  error,
}: {
  side: Side
  label: string
  file: UploadedFile | null
  onFile: (file: File) => void
  error: string | null
}) {
  const [over, setOver] = useState(false)
  const id = `drop-${side}`

  const border = over
    ? 'border-gold border-solid'
    : file !== null
      ? 'border-sage/40 border-solid'
      : 'border-line-2 border-dashed hover:border-gold/50'

  return (
    <label
      htmlFor={id}
      onDragOver={(event) => {
        event.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault()
        setOver(false)
        const dropped = event.dataTransfer.files[0]
        if (dropped) onFile(dropped)
      }}
      className={`block cursor-pointer rounded-lg border px-8 py-10 transition-colors duration-300 ease-refined ${border}`}
    >
      <input
        id={id}
        type="file"
        accept=".csv,text/csv,text/plain"
        onChange={(event) => {
          const chosen = event.target.files?.[0]
          if (chosen) onFile(chosen)
          event.target.value = ''
        }}
        className="sr-only"
      />
      <div className="text-base text-cream">{label}</div>

      {file === null ? (
        <p className="mt-2 text-sm font-light text-slate">Drop a CSV, or click to choose.</p>
      ) : (
        <div className="mt-2 text-sm font-light text-slate">
          <p className="text-ash">{file.name}</p>
          <p>
            {formatCount(file.parsed.rows.length)} rows · {file.parsed.headers.length}{' '}
            columns · {formatBytes(file.size)}
          </p>
        </div>
      )}

      {error !== null && <p className="mt-3 text-sm font-light text-rose">{error}</p>}
    </label>
  )
}

function MappingBlock({
  title,
  file,
  map,
  onMap,
}: {
  title: string
  file: UploadedFile
  map: ColumnMap
  onMap: (next: ColumnMap) => void
}) {
  return (
    <Panel>
      <PanelHead title={title} aside={<Eyebrow>{file.name}</Eyebrow>} />
      <PanelBody>
        <div className="grid gap-x-10 gap-y-7 sm:grid-cols-2 lg:grid-cols-3">
          {REQUIRED_FIELDS.map((field) => (
            <div key={field}>
              <label htmlFor={`${title}-${field}`} className="mb-3 block">
                <Eyebrow>
                  {FIELD_LABELS[field]}
                  {HARD_REQUIRED.includes(field) && <span className="ml-1 text-rose">*</span>}
                </Eyebrow>
              </label>
              <Select
                id={`${title}-${field}`}
                value={map[field] ?? ''}
                onChange={(value) => onMap({ ...map, [field]: value === '' ? null : value })}
              >
                <option value="">— Not mapped —</option>
                {file.parsed.headers.map((header) => (
                  <option key={header} value={header}>
                    {header}
                  </option>
                ))}
              </Select>
            </div>
          ))}
        </div>
      </PanelBody>
    </Panel>
  )
}

export function UploadPanel({
  gateway,
  ledger,
  gatewayMap,
  ledgerMap,
  fallbackCurrency,
  fileError,
  running,
  onFile,
  onMapChange,
  onFallbackCurrency,
  onRun,
}: {
  gateway: UploadedFile | null
  ledger: UploadedFile | null
  gatewayMap: ColumnMap
  ledgerMap: ColumnMap
  fallbackCurrency: string
  fileError: Record<Side, string | null>
  running: boolean
  onFile: (side: Side, file: File) => void
  onMapChange: (side: Side, map: ColumnMap) => void
  onFallbackCurrency: (value: string) => void
  onRun: () => void
}) {
  const bothPresent = gateway !== null && ledger !== null
  const errors = bothPresent
    ? [
        ...validationErrors(gatewayMap).map((message) => `Gateway: ${message}`),
        ...validationErrors(ledgerMap).map((message) => `Ledger: ${message}`),
      ]
    : []
  const canRun = bothPresent && errors.length === 0 && !running

  return (
    <div className="space-y-8">
      <div className="grid gap-6 sm:grid-cols-2">
        <Dropzone
          side="gateway"
          label="Gateway transactions"
          file={gateway}
          onFile={(file) => onFile('gateway', file)}
          error={fileError.gateway}
        />
        <Dropzone
          side="ledger"
          label="Ledger entries"
          file={ledger}
          onFile={(file) => onFile('ledger', file)}
          error={fileError.ledger}
        />
      </div>

      {!bothPresent && (
        <p className="text-sm font-light text-slate">
          No files handy? Download{' '}
          <a
            href="/samples/gateway-sample.csv"
            download
            className="text-gold transition-opacity duration-300 ease-refined hover:opacity-80"
          >
            gateway-sample.csv
          </a>{' '}
          and{' '}
          <a
            href="/samples/ledger-sample.csv"
            download
            className="text-gold transition-opacity duration-300 ease-refined hover:opacity-80"
          >
            ledger-sample.csv
          </a>
          .
        </p>
      )}

      {gateway !== null && (
        <MappingBlock
          title="Gateway columns"
          file={gateway}
          map={gatewayMap}
          onMap={(map) => onMapChange('gateway', map)}
        />
      )}
      {ledger !== null && (
        <MappingBlock
          title="Ledger columns"
          file={ledger}
          map={ledgerMap}
          onMap={(map) => onMapChange('ledger', map)}
        />
      )}

      {bothPresent && (
        <Panel>
          <PanelBody className="flex flex-wrap items-center justify-between gap-6">
            <Eyebrow>Fallback currency</Eyebrow>
            <Select className="w-32" value={fallbackCurrency} onChange={onFallbackCurrency}>
              {['INR', 'USD', 'EUR', 'GBP', 'JPY'].map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </Select>
          </PanelBody>
        </Panel>
      )}

      {errors.length > 0 && (
        <Alert tone="caution">
          <ul className="space-y-1">
            {errors.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        </Alert>
      )}

      <Button variant="primary" onClick={onRun} disabled={!canRun}>
        {running ? 'Reconciling…' : 'Reconcile'}
      </Button>
    </div>
  )
}
