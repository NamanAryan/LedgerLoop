/** Screen 3 — upload, then map columns. */

import { useState } from 'react'
import {
  HARD_REQUIRED,
  REQUIRED_FIELDS,
  validationErrors,
  type ColumnMap,
  type FieldName,
  type ParsedCsv,
} from '../engine/csv'
import type { Side } from '../engine/types'
import { formatBytes, formatCount } from '../format'

export interface UploadedFile {
  name: string
  size: number
  parsed: ParsedCsv
}

const FIELD_LABELS: Record<FieldName, string> = {
  txn_id: 'txn_id',
  amount: 'amount',
  currency: 'currency',
  timestamp: 'timestamp',
  idempotency_key: 'idempotency_key',
}

const FIELD_HINTS: Record<FieldName, string> = {
  txn_id: 'The shared key both sides use for the same payment.',
  amount: 'Decimal major units. Parsed as an exact integer, never a float.',
  currency: 'ISO code. Unmapped falls back to the fixed value below.',
  timestamp: 'ISO 8601 or epoch seconds/ms. Ambiguous date formats are rejected.',
  idempotency_key: 'Optional. Without it, layer 4 cannot detect duplicates.',
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

  return (
    <label
      htmlFor={id}
      className={`drop${file !== null ? ' filled' : ''}${over ? ' over' : ''}`}
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
      />
      <div className="drop-title">{label}</div>
      {file === null ? (
        <div className="drop-hint">Drop a CSV here, or click to choose one.</div>
      ) : (
        <div className="drop-hint">
          <span className="mono" style={{ color: 'var(--text)' }}>
            {file.name}
          </span>
          <br />
          {formatCount(file.parsed.rows.length)} rows · {file.parsed.headers.length} columns ·{' '}
          {formatBytes(file.size)}
          {file.parsed.raggedRows > 0 && (
            <>
              <br />
              <span style={{ color: 'var(--warn)' }}>
                {formatCount(file.parsed.raggedRows)} rows had an unexpected field count — padded,
                not dropped.
              </span>
            </>
          )}
        </div>
      )}
      {error !== null && (
        <div className="drop-hint" style={{ color: 'var(--bad)', marginTop: 8 }}>
          {error}
        </div>
      )}
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
  const mapped = new Set(Object.values(map).filter((v): v is string => v !== null))

  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="panel-title">{title}</h2>
        <span className="eyebrow mono">{file.name}</span>
      </div>
      <div className="panel-body stack">
        <div className="map-grid">
          {REQUIRED_FIELDS.map((field) => (
            <div className="map-field" key={field}>
              <label className="eyebrow" htmlFor={`${title}-${field}`}>
                {FIELD_LABELS[field]}
                {HARD_REQUIRED.includes(field) && <span className="map-required">*</span>}
              </label>
              <select
                id={`${title}-${field}`}
                className="select"
                value={map[field] ?? ''}
                onChange={(event) =>
                  onMap({ ...map, [field]: event.target.value === '' ? null : event.target.value })
                }
              >
                <option value="">— not mapped —</option>
                {file.parsed.headers.map((header) => (
                  <option key={header} value={header}>
                    {header}
                  </option>
                ))}
              </select>
              <p className="field-hint">{FIELD_HINTS[field]}</p>
            </div>
          ))}
        </div>

        <div>
          <span className="eyebrow">First 5 rows</span>
          <div className="preview-scroll" style={{ marginTop: 8 }}>
            <table className="preview">
              <thead>
                <tr>
                  {file.parsed.headers.map((header) => (
                    <th key={header} className={mapped.has(header) ? 'mapped' : undefined}>
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {file.parsed.rows.slice(0, 5).map((row, index) => (
                  <tr key={index}>
                    {row.map((cell, cellIndex) => (
                      <td key={cellIndex}>{cell === '' ? '—' : cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  )
}

export function UploadMapping({
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
  onRun,
  onBack,
}: {
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
  onRun: () => void
  onBack: () => void
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
    <div className="stack">
      <div className="page-head">
        <div>
          <h1 className="page-title">Upload your data</h1>
          <p className="page-sub">
            Two CSVs, one per side. Files are read in this tab and never leave it.
          </p>
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onBack}>
          ← Back
        </button>
      </div>

      <div className="drops">
        <Dropzone
          side="gateway"
          label="Gateway transactions (CSV)"
          file={gateway}
          onFile={(file) => onFile('gateway', file)}
          error={fileError.gateway}
        />
        <Dropzone
          side="ledger"
          label="Ledger entries (CSV)"
          file={ledger}
          onFile={(file) => onFile('ledger', file)}
          error={fileError.ledger}
        />
      </div>

      {!bothPresent && (
        <p className="field-hint">
          Column mapping appears once both files are loaded — real exports rarely agree on
          header names, so each side is mapped separately. No files handy?{' '}
          <a href="/samples/gateway-sample.csv" download className="sample-link">
            gateway-sample.csv
          </a>{' '}
          and{' '}
          <a href="/samples/ledger-sample.csv" download className="sample-link">
            ledger-sample.csv
          </a>{' '}
          are a matched pair with deliberately different headers.
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
        <section className="panel">
          <div className="panel-body row-between">
            <div>
              <span className="eyebrow">Fallback currency</span>
              <p className="field-hint">
                Used for any row whose currency column is unmapped or blank.
              </p>
            </div>
            <select
              className="select"
              style={{ width: 120 }}
              value={fallbackCurrency}
              onChange={(event) => onFallbackCurrency(event.target.value)}
            >
              {['INR', 'USD', 'EUR', 'GBP', 'JPY'].map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          </div>
        </section>
      )}

      {errors.length > 0 && (
        <div className="alert alert-warn">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {errors.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        </div>
      )}

      {runError !== null && <div className="alert">{runError}</div>}

      <div className="actions">
        <button type="button" className="btn btn-primary" onClick={onRun} disabled={!canRun}>
          {running ? 'Reconciling…' : 'Reconcile'}
        </button>
        {!bothPresent && (
          <span className="field-hint" style={{ margin: 0 }}>
            Load both files to continue.
          </span>
        )}
      </div>
    </div>
  )
}
