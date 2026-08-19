/**
 * Promise-shaped access to the engine worker.
 *
 * The worker is created lazily and kept alive between runs — spinning one up costs a
 * few milliseconds and the user will usually reconcile more than once. If the
 * environment has no worker support the call falls back to running in-thread, so the
 * app degrades to "briefly janky" rather than "broken".
 */

import { generate, type GeneratorConfig } from './generate'
import { reconcile } from './reconcile'
import type { GroundTruth, MatchConfig, ReconResult, TxnFacts } from './types'
import type { WorkerRequest, WorkerResponse } from './worker'

/**
 * `Omit` collapses a union into its common keys, which would leave every request
 * body typed as `{ type }` alone. Distributing over the union keeps each variant's
 * own fields checkable.
 */
type RequestBody = WorkerRequest extends infer T
  ? T extends WorkerRequest
    ? Omit<T, 'id'>
    : never
  : never

export interface RunOutcome {
  result: ReconResult
  truth: GroundTruth | null
  totalMs: number
}

let worker: Worker | null = null
let nextId = 1
const pending = new Map<number, { resolve: (v: RunOutcome) => void; reject: (e: Error) => void }>()

function ensureWorker(): Worker | null {
  if (worker !== null) return worker
  if (typeof Worker === 'undefined') return null

  try {
    worker = new Worker(new URL('./worker.ts', import.meta.url), { type: 'module' })
  } catch {
    return null
  }

  worker.onmessage = (event: MessageEvent<WorkerResponse>) => {
    const response = event.data
    const entry = pending.get(response.id)
    if (entry === undefined) return
    pending.delete(response.id)
    if (response.ok) {
      entry.resolve({ result: response.result, truth: response.truth, totalMs: response.totalMs })
    } else {
      entry.reject(new Error(response.error))
    }
  }

  worker.onerror = (event) => {
    // A worker-level failure invalidates every request in flight; failing them all
    // is better than leaving the UI on a spinner that will never resolve.
    const error = new Error(event.message || 'reconciliation worker failed')
    for (const entry of pending.values()) entry.reject(error)
    pending.clear()
    worker?.terminate()
    worker = null
  }

  return worker
}

function post(request: RequestBody): Promise<RunOutcome> | null {
  const instance = ensureWorker()
  if (instance === null) return null

  const id = nextId++
  return new Promise<RunOutcome>((resolve, reject) => {
    pending.set(id, { resolve, reject })
    instance.postMessage({ ...request, id } as WorkerRequest)
  })
}

export function runGenerated(
  generator: GeneratorConfig,
  match: MatchConfig,
): Promise<RunOutcome> {
  const viaWorker = post({ type: 'generate', generator, match })
  if (viaWorker !== null) return viaWorker

  const startedAt = performance.now()
  const run = generate(generator, match)
  const result = reconcile({
    gateway: run.gateway,
    ledger: run.ledger,
    config: match,
    runId: run.runId,
  })
  return Promise.resolve({ result, truth: run.truth, totalMs: performance.now() - startedAt })
}

export function runUploaded(
  gateway: TxnFacts[],
  ledger: TxnFacts[],
  match: MatchConfig,
  runId: string,
): Promise<RunOutcome> {
  const viaWorker = post({ type: 'reconcile', gateway, ledger, match, runId })
  if (viaWorker !== null) return viaWorker

  const startedAt = performance.now()
  const result = reconcile({ gateway, ledger, config: match, runId })
  return Promise.resolve({ result, truth: null, totalMs: performance.now() - startedAt })
}
