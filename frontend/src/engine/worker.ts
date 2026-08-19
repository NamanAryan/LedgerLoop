/**
 * The engine, off the main thread.
 *
 * 50,000 pairs reconcile in well under a second, but "well under a second" is still
 * long enough to drop frames and freeze a scroll if it happens on the UI thread.
 * Running it here keeps the interface answerable while the run is in flight, which
 * is the difference between a demo and a tool.
 */

import { generate, type GeneratorConfig } from './generate'
import { reconcile } from './reconcile'
import type { GroundTruth, MatchConfig, ReconResult, TxnFacts } from './types'

export type WorkerRequest =
  | { id: number; type: 'generate'; generator: GeneratorConfig; match: MatchConfig }
  | {
      id: number
      type: 'reconcile'
      gateway: TxnFacts[]
      ledger: TxnFacts[]
      match: MatchConfig
      runId: string
    }

export type WorkerResponse =
  | {
      id: number
      ok: true
      result: ReconResult
      truth: GroundTruth | null
      /** Wall time including generation and transfer, not just the match loop. */
      totalMs: number
    }
  | { id: number; ok: false; error: string }

self.onmessage = (event: MessageEvent<WorkerRequest>) => {
  const request = event.data
  const startedAt = performance.now()

  try {
    if (request.type === 'generate') {
      const run = generate(request.generator, request.match)
      const result = reconcile({
        gateway: run.gateway,
        ledger: run.ledger,
        config: request.match,
        runId: run.runId,
      })
      const response: WorkerResponse = {
        id: request.id,
        ok: true,
        result,
        truth: run.truth,
        totalMs: performance.now() - startedAt,
      }
      self.postMessage(response)
      return
    }

    const result = reconcile({
      gateway: request.gateway,
      ledger: request.ledger,
      config: request.match,
      runId: request.runId,
    })
    const response: WorkerResponse = {
      id: request.id,
      ok: true,
      result,
      truth: null,
      totalMs: performance.now() - startedAt,
    }
    self.postMessage(response)
  } catch (error) {
    const response: WorkerResponse = {
      id: request.id,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    }
    self.postMessage(response)
  }
}
