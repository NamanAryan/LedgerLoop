/**
 * The account's first webhook source, kept fresh while the tab is visible.
 *
 * One source, not a list: the card shows a single endpoint, and an account with several
 * is a case the dashboard does not yet render. Taking `[0]` is stated here rather than
 * hidden at the call site so the limitation is visible to whoever adds the second one.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { createWebhookSource, getWebhookSources, sendTestPayload } from '../api/client'
import { useTenantContext } from '../tenant/TenantContext'
import type { WebhookProvider, WebhookSource, WebhookTestResult } from '../api/types'

/** Matches the backend's own cadence closely enough to feel live without polling a
 *  free-tier instance into the ground. */
const POLL_MS = 10_000

/**
 * How long a source may go quiet before it reads as idle rather than live.
 *
 * A dead integration must not render green. Fifteen minutes is long enough that a
 * genuinely low-traffic endpoint is not libelled, short enough that a broken one is
 * caught within a coffee break.
 */
export const IDLE_AFTER_MS = 15 * 60 * 1000

export type SourceHealth = 'none' | 'awaiting' | 'live' | 'idle' | 'rejecting'

/**
 * Health is computed here, from the timestamp and the status, rather than read off a
 * field the backend sets.
 *
 * Order matters: a failing delivery status wins over recency. A source whose secret is
 * wrong receives events *and* rejects them, so it has a fresh `last_event_at` and would
 * otherwise render green while nothing at all is reconciling — the single most likely
 * real failure, showing as success.
 */
export function healthOf(source: WebhookSource | null, now: number): SourceHealth {
  if (source === null) return 'none'
  if (source.last_delivery_status !== null && source.last_delivery_status !== 'ok') {
    return 'rejecting'
  }
  if (source.last_event_at === null) return 'awaiting'
  const age = now - new Date(source.last_event_at).getTime()
  return age > IDLE_AFTER_MS ? 'idle' : 'live'
}

export function useWebhookSource() {
  const { mode } = useTenantContext()
  const [source, setSource] = useState<WebhookSource | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<WebhookTestResult | null>(null)

  // Guards against a slow response from a previous mode landing after a sign-out and
  // repainting an account's endpoint over the sandbox card.
  const generation = useRef(0)

  const refresh = useCallback(async () => {
    if (mode !== 'account') return
    const mine = ++generation.current
    setLoading(true)
    try {
      const page = await getWebhookSources()
      if (generation.current !== mine) return
      setSource(page.items[0] ?? null)
      setError(null)
    } catch (caught) {
      if (generation.current !== mine) return
      // A 401 has already cleared the key inside the fetch wrapper and the provider
      // will flip this component to sandbox; there is nothing useful to say here.
      setError(caught instanceof Error ? caught.message : 'Could not read webhook sources.')
    } finally {
      if (generation.current === mine) setLoading(false)
    }
  }, [mode])

  // Sandbox mode holds no sources and must not poll: an unauthenticated caller would
  // just be creating demo-tenant rows on a timer for a card that shows a locked state.
  useEffect(() => {
    if (mode !== 'account') {
      generation.current += 1
      setSource(null)
      setError(null)
      setTestResult(null)
      return
    }

    void refresh()

    let timer: number | undefined
    const start = () => {
      window.clearInterval(timer)
      timer = window.setInterval(() => void refresh(), POLL_MS)
    }
    const stop = () => window.clearInterval(timer)

    // A backgrounded tab polling for hours is the classic way a demo dashboard keeps a
    // free-tier instance awake and burns its quota. Pause on hide, and refetch straight
    // away on return so the first thing seen is current rather than ten seconds stale.
    const onVisibility = () => {
      if (document.hidden) {
        stop()
      } else {
        void refresh()
        start()
      }
    }

    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [mode, refresh])

  const create = useCallback(
    async (provider: WebhookProvider) => {
      setCreating(true)
      setError(null)
      try {
        setSource(await createWebhookSource({ provider }))
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : 'Could not create the endpoint.')
      } finally {
        setCreating(false)
      }
    },
    [],
  )

  const test = useCallback(async () => {
    if (source === null) return
    setTesting(true)
    setTestResult(null)
    try {
      const result = await sendTestPayload(source.id)
      setTestResult(result)
      // Refetch immediately: the delivery just moved last_event_at, and waiting up to
      // ten seconds for the poll would make a working test look like it did nothing.
      await refresh()
    } catch (caught) {
      setTestResult({
        delivered: false,
        http_status: 0,
        duplicate: false,
        txn_id: null,
        // http_status 0 means the request never got an answer — offline, DNS, a cold
        // start timing out, or a CORS rejection the browser will not describe. Say the
        // useful part rather than surfacing "Failed to fetch".
        detail:
          caught instanceof Error
            ? caught.message
            : 'The request never reached the API (network, CORS, or a cold start).',
      })
    } finally {
      setTesting(false)
    }
  }, [source, refresh])

  return { source, loading, error, creating, testing, testResult, refresh, create, test }
}
