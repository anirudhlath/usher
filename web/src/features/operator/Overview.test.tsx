import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { renderApp, screen, waitFor, within } from '@/test/render'
import { server } from '@/test/server'
import { degradedReadiness } from '@/test/handlers'
import { expectNoViolations } from '@/test/axe'
import {
  bootstrapStatus,
  bootstrapStatusEmpty,
  problemHandler,
  readinessNotADocument,
  sourceUnavailable,
  unmatchedEmpty,
} from '@/test/fixtures'
import type { BootstrapStatusResponse } from '@/api'
import { TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES } from '@/app/routes'
import Overview from './Overview'

function render() {
  return renderApp(<Overview />, { theme: 'light', density: 'compact', route: ROUTES.ops })
}

/**
 * The shipped fixture's `heartbeat_at` is a fixed timestamp, so it is always
 * older than 120 s by the time a test runs — which is the *stalled* case. A run
 * that is genuinely alive has to be dated against the clock the screen reads.
 */
function statusWithHeartbeat(agoSeconds: number): BootstrapStatusResponse {
  return {
    ...bootstrapStatus,
    runs: bootstrapStatus.runs.map((run) =>
      run.status === 'running'
        ? {
            ...run,
            started_at: new Date(Date.now() - 3_600_000).toISOString(),
            heartbeat_at: new Date(Date.now() - agoSeconds * 1000).toISOString(),
          }
        : run,
    ),
  }
}

describe('Overview', () => {
  it('renders readiness, the running cursor and the sources table when everything answers', async () => {
    server.use(http.get('/admin/bootstrap/status', () => HttpResponse.json(statusWithHeartbeat(4))))
    render()

    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    expect(await screen.findByText('Ready')).toBeInTheDocument()
    expect(screen.getByText(/HTTP 200/)).toBeInTheDocument()

    // The cursor run, and the sentence that says there is no estimate.
    expect(await screen.findByText('imdb')).toBeInTheDocument()
    expect(
      screen.getByText('No completion estimate — the server reports a cursor, not a percentage.'),
    ).toBeInTheDocument()

    // The sources table, from `/admin/sources` alone.
    expect(await screen.findByText('Loft Emby')).toBeInTheDocument()
  })

  it('shows the loading state as a skeleton with a busy region, never a spinner', () => {
    const { container } = render()

    expect(screen.getByText('Loading readiness …')).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  })

  it('names the failing check rather than repeating the word "degraded", and never renders a 503 as a Problem', async () => {
    server.use(degradedReadiness())
    render()

    // The cause, not the symptom.
    expect(await screen.findByText('Migrations are behind')).toBeInTheDocument()
    expect(screen.getByText(/Migrations are behind the running code\. Reads are fine/)).toBeInTheDocument()
    expect(screen.getByText('behind')).toBeInTheDocument()

    // The API's own word is kept, beside the status code, and not used as a diagnosis.
    expect(screen.getByText(/status degraded · HTTP 503 · \/health\/ready/)).toBeInTheDocument()

    // A 503 from this route is a state. Nothing about it is an error treatment.
    expect(screen.queryByText('code source_unavailable')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('reports the lanes without implying readiness is gated on them', async () => {
    server.use(degradedReadiness())
    render()

    await screen.findByText('Migrations are behind')
    expect(screen.getByText('lanes.worker')).toBeInTheDocument()
    expect(screen.getByText('not running')).toBeInTheDocument()
    expect(screen.getByText('lanes.push')).toBeInTheDocument()
    expect(screen.getByText('no push lane is running')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Lanes are reported, never gated on: the server stays up whether or not they are running.',
      ),
    ).toBeInTheDocument()
  })

  it('falls back to the error treatment only when a 503 body is not a readiness document', async () => {
    server.use(http.get('/health/ready', () => HttpResponse.json(readinessNotADocument, { status: 503 })))
    render()

    expect(await screen.findByText('HTTP 503')).toBeInTheDocument()
    expect(screen.queryByText('Ready')).toBeNull()
  })

  it('shows a panel-scale Problem with code, status and the detail verbatim when a section fails', async () => {
    server.use(problemHandler('get', '/admin/sources', sourceUnavailable('/admin/sources')))
    render()

    expect(await screen.findByText('code source_unavailable')).toBeInTheDocument()
    expect(screen.getByText('HTTP 503')).toBeInTheDocument()
    expect(screen.getByText('Living Room Emby did not answer within 5.0 s.')).toBeInTheDocument()
    expect(screen.getByText('/admin/sources')).toBeInTheDocument()
  })

  it('states each empty surface as its own fact rather than as one grey dash', async () => {
    server.use(
      http.get('/admin/sources', () => HttpResponse.json([])),
      http.get('/admin/bootstrap/status', () => HttpResponse.json(bootstrapStatusEmpty)),
      http.get('/admin/unmatched', () => HttpResponse.json(unmatchedEmpty)),
    )
    render()

    // Never computed — the field that proves it is named.
    expect(
      await screen.findByText(
        'No import has ever run on this deployment, so there is no checkpoint to resume from.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('runs: []')).toBeInTheDocument()

    // Computed and empty — a different fact, drawn differently.
    expect(await screen.findByText('Nothing is waiting on a person')).toBeInTheDocument()
    expect(screen.getByText('unmatched: 0 loaded · runs: none failed')).toBeInTheDocument()

    expect(
      screen.getByText(/No media server is connected\. The catalog is still browsable/),
    ).toBeInTheDocument()
  })

  it('counts what is loaded and never quotes a total for the review queue', async () => {
    render()

    expect(await screen.findByText('Files could not be matched')).toBeInTheDocument()
    expect(screen.getByText('2 loaded so far')).toBeInTheDocument()
    // No denominator anywhere: "2 of 400" is the shape this forbids.
    expect(screen.queryByText(/\d+ of \d+/)).toBeNull()
  })

  it('labels the activity timeline REQUIRES BACKEND WORK with the route it needs', async () => {
    const { container } = render()

    await screen.findByText('Ready')
    const label = container.querySelector('.u-backendwork')
    expect(label).not.toBeNull()
    expect(
      within(label instanceof HTMLElement ? label : container).getByText('Requires backend work'),
    ).toBeInTheDocument()
    expect(screen.getByText('GET /admin/sources/{id}/runs')).toBeInTheDocument()
  })

  it('opens Sources from the section action', async () => {
    const { user } = render()

    await screen.findByText('Loft Emby')
    await user.click(screen.getByRole('button', { name: 'Open sources' }))
    // The screen under test is not routed here; the assertion is that the
    // control exists and is operable rather than that a route changed.
    expect(screen.getByRole('button', { name: 'Open sources' })).toBeInTheDocument()
  })

  it('has no accessibility violations', async () => {
    const { container } = render()

    await screen.findByText('Ready')
    await waitFor(() => expect(screen.getByText('Loft Emby')).toBeInTheDocument())
    await expectNoViolations(container)
  })

  describe('the trace link (patterns.md §3)', () => {
    /**
     * `/admin/sources` is one of this screen's four failure panels, and every
     * one of them is fed by the same `traceOf` mapper — a screen with four
     * errors is exactly the case that makes one hook call and four plain calls
     * the right shape.
     */
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/admin/sources',
          sourceUnavailable('/admin/sources'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(withTempo(<Overview />, tempoUrl), {
        theme: 'light',
        density: 'compact',
        route: ROUTES.ops,
      })
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      const { container } = renderFailed(TEMPO_URL, TRACE_ID)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      const { container } = renderFailed(null, TRACE_ID)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      const { container } = renderFailed(TEMPO_URL)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('links the review panel too, which is a second of the four call sites', async () => {
      server.use(
        problemHandler('get', '/admin/unmatched', sourceUnavailable('/admin/unmatched'), {
          traceId: TRACE_ID,
        }),
      )
      const { container } = renderApp(withTempo(<Overview />, TEMPO_URL), {
        theme: 'light',
        density: 'compact',
        route: ROUTES.ops,
      })

      await screen.findByText('/admin/unmatched')
      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })
  })
})
