import { CONSOLE_BASE } from '@/api/paths'

/**
 * Configuration the bundle cannot know at build time, served by
 * `usher.api.console` at `/console/config.json`.
 *
 * Three values, and two of them are deliberately nullable. `grafanaUrl` and
 * `tempoUrl` are deployment facts — the Insights screen's "Open in Grafana" is
 * a marked escape hatch and `Problem`'s "Open trace" is a link into Tempo — and
 * an unconfigured one must render as **absent** rather than as a dead link.
 * That is the same distinction this product makes everywhere between never
 * computed and computed and empty, applied to its own configuration.
 */
export interface RuntimeConfig {
  /** The server's version, for the About screen. */
  version: string
  grafanaUrl: string | null
  tempoUrl: string | null
}

/**
 * What the app runs on before the fetch lands, and what it keeps if the fetch
 * fails. Both nulls, so a console served by something that is not Usher — a
 * `vite preview`, a static host, the Playwright suite — degrades to "these
 * links are not configured" instead of to a crash.
 */
export const UNCONFIGURED: RuntimeConfig = {
  version: '',
  grafanaUrl: null,
  tempoUrl: null,
}

function isRuntimeConfig(value: unknown): value is RuntimeConfig {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Record<string, unknown>
  return (
    typeof candidate['version'] === 'string' &&
    (candidate['grafanaUrl'] === null || typeof candidate['grafanaUrl'] === 'string') &&
    (candidate['tempoUrl'] === null || typeof candidate['tempoUrl'] === 'string')
  )
}

/**
 * Deliberately a bare `fetch` rather than the API client.
 *
 * `config.json` is not one of the 35 operations: it is not in `paths`, it has
 * no generated type, and it is served outside the OpenAPI document on purpose.
 * Putting it through `request()` would journal an entry with a `null` template,
 * which the coverage ledger would then count as an operation it can never
 * green — the measurement measuring itself. `usher-web` learned that one about
 * `/openapi.json` and the reasoning transfers exactly.
 */
export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  try {
    const response = await fetch(`${CONSOLE_BASE}/config.json`, {
      headers: { accept: 'application/json' },
    })
    if (!response.ok) return UNCONFIGURED
    const body: unknown = await response.json()
    return isRuntimeConfig(body) ? body : UNCONFIGURED
  } catch {
    return UNCONFIGURED
  }
}
