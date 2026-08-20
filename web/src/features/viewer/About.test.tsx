import type { ReactElement } from 'react'
import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { renderApp, screen, waitFor } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { degradedReadiness } from '@/test/handlers'
import {
  attribution,
  problemHandler,
  problemResponse,
  readinessNotADocument,
  sourceUnavailable,
} from '@/test/fixtures'
import { TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks } from '@/test/trace'
import { RuntimeConfigContext } from '@/app/runtime-config-context'
import type { RuntimeConfig } from '@/app/runtime-config'
import About from './About'

const CONFIGURED: RuntimeConfig = { version: '0.9.4', grafanaUrl: null, tempoUrl: null }

function renderAbout(config: RuntimeConfig = CONFIGURED) {
  const ui: ReactElement = (
    <RuntimeConfigContext.Provider value={config}>
      <About />
    </RuntimeConfigContext.Provider>
  )
  return renderApp(ui, { route: '/about' })
}

describe('About', () => {
  it('renders all four attribution strings verbatim', async () => {
    const { container } = renderAbout()

    // Byte-for-byte. These are licence terms, not copy the client may reword,
    // shorten or translate.
    for (const entry of attribution) {
      expect(await screen.findByText(entry.text)).toBeVisible()
      expect(screen.getByText(entry.source)).toBeVisible()
    }
    expect(attribution).toHaveLength(4)

    expect(screen.getByText(/are never edited, shortened or translated in the client/i)).toBeVisible()

    await expectNoViolations(container)
  })

  it('renders the TMDb logo as a marked empty slot rather than substituting a mark', async () => {
    renderAbout()

    const slot = await screen.findByText('official TMDb logo required')
    expect(slot).toBeVisible()

    // Nothing stands in for it: no image, and the slot sits beside TMDb's own
    // disclaimer, which is what their terms require of it.
    expect(screen.queryByRole('img', { name: /tmdb/i })).toBeNull()
    expect(container(slot)).toHaveTextContent(
      'This product uses the TMDb API but is not endorsed or certified by TMDb.',
    )
  })

  it('shows the version from the runtime config', async () => {
    renderAbout()
    expect(await screen.findByText('0.9.4')).toBeVisible()
  })

  it('states the version is absent rather than inventing one', async () => {
    renderAbout({ version: '', grafanaUrl: null, tempoUrl: null })

    expect(await screen.findByText(/This console was not served by Usher, so/i)).toBeVisible()
    expect(screen.getByText('/console/config.json')).toBeVisible()
  })

  it('renders a ready deployment with a word and a glyph, not a colour', async () => {
    renderAbout()

    expect(await screen.findByText('ready')).toBeVisible()
    expect(screen.getByText(/database ok · migrations ok/)).toBeVisible()
    expect(screen.getByText(/push Living Room Emby · worker running/)).toBeVisible()
  })

  it('treats a 503 readiness as a state that names the failed check, never as a Problem', async () => {
    server.use(degradedReadiness())
    const { container } = renderAbout()

    expect(await screen.findByText('degraded')).toBeVisible()
    expect(screen.getByText(/Running degraded\./)).toBeVisible()
    expect(screen.getByText(/The migrations check is failing\./)).toBeVisible()
    expect(screen.getByText(/No worker lane is running in this process/)).toBeVisible()

    // A 503 here carries the readiness document, so it is information. No
    // error envelope, no code, no retry button.
    expect(screen.queryByText('HTTP 503')).toBeNull()
    expect(screen.queryByText(/code source_unavailable/)).toBeNull()
    expect(screen.getByText(/database ok · migrations failing/)).toBeVisible()

    await expectNoViolations(container)
  })

  it('falls back to the error treatment when a 503 body is not a readiness document', async () => {
    server.use(http.get('/health/ready', () => HttpResponse.json(readinessNotADocument, { status: 503 })))
    renderAbout()

    await waitFor(() => {
      expect(screen.getByText('could not be read')).toBeVisible()
    })
    expect(screen.getByText('HTTP 503')).toBeVisible()
  })

  it('shows a skeleton for the notices while the route is pending', () => {
    renderAbout()
    expect(screen.getByText('Loading the attribution notices …')).toBeInTheDocument()
  })

  it('says an empty notice list is a deployment to look at, not an empty screen', async () => {
    server.use(http.get('/meta/attribution', () => HttpResponse.json([])))
    const { container } = renderAbout()

    expect(await screen.findByText('No notices were returned')).toBeVisible()
    expect(screen.getByText('/meta/attribution: []')).toBeVisible()

    await expectNoViolations(container)
  })

  it('renders a failed attribution fetch at panel scale with the server detail verbatim', async () => {
    server.use(
      http.get('/meta/attribution', () =>
        problemResponse(sourceUnavailable('/meta/attribution'), { 'retry-after': '30' }),
      ),
    )
    renderAbout()

    expect(await screen.findByText('code source_unavailable')).toBeVisible()
    expect(screen.getByText('Living Room Emby did not answer within 5.0 s.')).toBeVisible()
    // Retry honours Retry-After: disabled until the window the server named.
    expect(screen.getByRole('button', { name: /Try again in 30 s/ })).toBeDisabled()
  })

  describe('the trace link (patterns.md §3)', () => {
    /**
     * About reaches `Problem` through `ScreenProblem`, so this is the wrapper's
     * wiring proven end to end on a real screen. `NotFound.test.tsx` carries
     * the full matrix for the wrapper itself.
     */
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/meta/attribution',
          sourceUnavailable('/meta/attribution'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderAbout({ version: '0.9.4', grafanaUrl: null, tempoUrl })
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      const { container: root } = renderFailed(TEMPO_URL, TRACE_ID)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(root)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      const { container: root } = renderFailed(null, TRACE_ID)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(root)).toHaveLength(0)
      expect(deadLinks(root)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      const { container: root } = renderFailed(TEMPO_URL)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(root)).toHaveLength(0)
      expect(deadLinks(root)).toHaveLength(0)
    })
  })
})

/** The notice card a matched node sits in. */
function container(node: HTMLElement): HTMLElement {
  const parent = node.parentElement
  if (parent === null) throw new Error('expected the slot to have a parent')
  return parent
}
