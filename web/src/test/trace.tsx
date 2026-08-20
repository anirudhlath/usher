/**
 * The test kit for the one link patterns.md §3 makes a MUST — *"When the
 * response carried a trace id, `Problem` MUST render 'Open trace' into Tempo.
 * This single link is what separates a console from a settings page."*
 *
 * Every screen has the same three cases, and they are three because two of them
 * look identical from a naive assertion:
 *
 * 1. Tempo configured, response carried a `traceresponse` → **one anchor**,
 *    whose `href` contains the trace id.
 * 2. Tempo **unconfigured** → **zero anchors**. Not an anchor with an empty
 *    `href`: `<a href="">` navigates to the current page, so a test that only
 *    checked the href would pass on the exact bug the rule exists to prevent.
 *    That is what `deadLinks` is for.
 * 3. Tempo configured, **no header** on the response → zero anchors as well.
 *    There is nothing in Tempo to open and a link built from a fabricated id is
 *    worse than none.
 *
 * `withTempo` supplies the deployment fact through the **real**
 * `RuntimeConfigContext`, the same provider `Providers` fills from
 * `/console/config.json`, so `useTraceUrl()` runs for real rather than stubbed.
 */

import type { ReactElement, ReactNode } from 'react'
import { RuntimeConfigContext } from '@/app/runtime-config-context'
import type { RuntimeConfig } from '@/app/runtime-config'

/** A configured deployment. Trailing-slash-free, as `useTraceUrl` expects. */
export const TEMPO_URL = 'https://tempo.lan'

export function tempoConfig(tempoUrl: string | null): RuntimeConfig {
  return { version: '0.9.4', grafanaUrl: null, tempoUrl }
}

/** Wraps a screen in the runtime config it would be mounted under. */
export function withTempo(children: ReactNode, tempoUrl: string | null): ReactElement {
  return (
    <RuntimeConfigContext.Provider value={tempoConfig(tempoUrl)}>{children}</RuntimeConfigContext.Provider>
  )
}

/**
 * The rendered "Open trace" anchors. `Problem` renders at most one per problem
 * document, so a length of 1 is the assertion and a length of 0 is the absence.
 *
 * Queried off `container` rather than `screen` so a test rendering a screen
 * plus its toast stack can ask about one of them.
 */
export function traceLinks(container: HTMLElement): HTMLAnchorElement[] {
  return [...container.querySelectorAll('a')].filter((anchor) => /open trace/i.test(anchor.textContent ?? ''))
}

/**
 * Every anchor that would navigate to the current page: no `href`, an empty
 * one, or a bare `#`. Zero of these is what separates "the link is absent"
 * from "the link is dead".
 */
export function deadLinks(container: HTMLElement): HTMLAnchorElement[] {
  return [...container.querySelectorAll('a')].filter((anchor) => {
    const href = anchor.getAttribute('href')
    return href === null || href.trim() === '' || href.trim() === '#'
  })
}
