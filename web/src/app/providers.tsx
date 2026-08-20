import { useEffect, useState, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { UsherProblem } from '@/api/client'
import { LayerStackProvider, ToastProvider } from '@/patterns'
import { RuntimeConfigContext } from './runtime-config-context'
import { loadRuntimeConfig, UNCONFIGURED, type RuntimeConfig } from './runtime-config'

/**
 * `retry` is a policy about *what a response means*, not a network setting.
 *
 * A 4xx problem document is an answer. Retrying a `not_found` or a
 * `validation_failed` three times delays the message, triples the request
 * journal and puts three identical rows in the operator's log. 5xx and
 * transport failures still get one retry, because those are the two cases where
 * the same request can legitimately succeed.
 *
 * `invalid_cursor` (400) is the one 4xx a screen must never render — patterns.md
 * §3 says the list silently restarts from the top — and that is handled where
 * the cursor lives, not here.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => {
          if (error instanceof UsherProblem && error.status >= 400 && error.status < 500) return false
          return failureCount < 1
        },
        refetchOnWindowFocus: false,
        staleTime: 10_000,
      },
    },
  })
}

/**
 * Order matters, outermost first:
 *
 * · `QueryClientProvider` — everything below may fetch.
 * · `RuntimeConfigContext` — the Grafana and Tempo links, which `Problem`
 *   renders and which therefore have to be readable from an error boundary.
 * · `LayerStackProvider` — the single `Esc` listener, above every layer.
 * · `ToastProvider` — the receipt queue outlives any screen that opened it,
 *   because a 202's key is the only record of a queued job.
 */
export function Providers({ children, client }: { children: ReactNode; client: QueryClient }) {
  const [config, setConfig] = useState<RuntimeConfig>(UNCONFIGURED)

  useEffect(() => {
    let live = true
    void loadRuntimeConfig().then((loaded) => {
      if (live) setConfig(loaded)
    })
    return () => {
      live = false
    }
  }, [])

  return (
    <QueryClientProvider client={client}>
      <RuntimeConfigContext.Provider value={config}>
        <LayerStackProvider>
          <ToastProvider>{children}</ToastProvider>
        </LayerStackProvider>
      </RuntimeConfigContext.Provider>
    </QueryClientProvider>
  )
}
