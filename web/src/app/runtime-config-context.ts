import { createContext, useContext } from 'react'
import { UNCONFIGURED, type RuntimeConfig } from './runtime-config'

/**
 * Separated from `providers.tsx` so the context object is not re-exported from
 * a module that also exports components — oxlint's `react/only-export-components`
 * is on, and more usefully a context in its own module cannot be accidentally
 * recreated by a fast-refresh boundary.
 */
export const RuntimeConfigContext = createContext<RuntimeConfig>(UNCONFIGURED)

export function useRuntimeConfig(): RuntimeConfig {
  return useContext(RuntimeConfigContext)
}

/**
 * `null` when Tempo is not configured, which callers must render as the link
 * being **absent** rather than as a dead one.
 */
export function useTraceUrl(): (traceId: string) => string | null {
  const { tempoUrl } = useRuntimeConfig()
  return (traceId: string) => {
    if (!tempoUrl || !traceId) return null
    return `${tempoUrl.replace(/\/$/, '')}/explore?traceId=${encodeURIComponent(traceId)}`
  }
}
