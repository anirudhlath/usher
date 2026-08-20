import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { ToastProvider } from '@/patterns'
import { ToastStack } from '@/features/shared/ToastStack'
import { renderApp, screen, within } from '@/test/render'
import { server } from '@/test/server'
import { expectNoViolations } from '@/test/axe'
import {
  bootstrapStatus,
  bootstrapStatusEmpty,
  importRunning,
  problemHandler,
  sourceUnavailable,
} from '@/test/fixtures'
import type { BootstrapStatusResponse } from '@/api'
import { TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES } from '@/app/routes'
import BootstrapScreen from './Bootstrap'

function render() {
  return renderApp(
    <ToastProvider>
      <BootstrapScreen />
      <ToastStack />
    </ToastProvider>,
    { theme: 'light', density: 'compact', route: ROUTES.bootstrap },
  )
}

/**
 * The shipped fixture's `heartbeat_at` is a fixed timestamp and is therefore
 * always older than the 120 s threshold by the time a test runs. The stall
 * boundary is a fact about *age*, so it is dated against the same clock the
 * screen reads — no fake timers, and the assertion is exercisable at 119 and
 * 121 exactly as `CursorProgress` was designed to be.
 */
function statusWithHeartbeat(agoSeconds: number): BootstrapStatusResponse {
  return {
    ...bootstrapStatus,
    runs: [
      {
        ...importRunning,
        started_at: new Date(Date.now() - 3_600_000).toISOString(),
        heartbeat_at: new Date(Date.now() - agoSeconds * 1000).toISOString(),
      },
    ],
  }
}

function heartbeatHandler(agoSeconds: number) {
  return http.get('/admin/bootstrap/status', () => HttpResponse.json(statusWithHeartbeat(agoSeconds)))
}

describe('Bootstrap', () => {
  it('lists the six phases in mandatory execution order and says why the order is not stylistic', async () => {
    const { container } = render()

    expect(await screen.findByRole('heading', { level: 1, name: 'Bootstrap' })).toBeInTheDocument()
    await screen.findByText('IMDb basics')

    const wire = Array.from(container.querySelectorAll('.u-mono'))
      .map((node) => node.textContent)
      .filter((text): text is string =>
        ['imdb', 'credit-names', 'aliases', 'tmdb-ids', 'crosswalk', 'movielens'].includes(text ?? ''),
      )
    expect(wire).toEqual(['imdb', 'credit-names', 'aliases', 'tmdb-ids', 'crosswalk', 'movielens'])

    expect(screen.getByText(/joins to titles on imdb_id, and it writes only skeletons/)).toBeInTheDocument()
  })

  it('shows the loading state as a table-shaped skeleton in a busy region', () => {
    const { container } = render()

    expect(screen.getByText('Loading bootstrap status …')).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  })

  it('renders no percent character and no aria-valuenow in its cursor progress', async () => {
    server.use(heartbeatHandler(4))
    const { container } = render()

    await screen.findByText('No completion estimate — the server reports a cursor, not a percentage.')

    const bars = screen.getAllByRole('progressbar')
    expect(bars.length).toBeGreaterThan(0)
    for (const bar of bars) {
      expect(bar).not.toHaveAttribute('aria-valuenow')
      expect(bar).toHaveAttribute('aria-valuetext')
      const card = bar.closest('.u-cursor')
      expect(card).not.toBeNull()
      expect(card?.textContent ?? '').not.toContain('%')
    }

    // The resume point, verbatim and in mono.
    expect(within(container).getByText(String(importRunning.position))).toBeInTheDocument()
  })

  it('shows rows/sec as an em dash until a second poll has happened', async () => {
    server.use(heartbeatHandler(4))
    render()

    const label = await screen.findByText('rows / sec')
    const value = label.nextElementSibling
    expect(value?.textContent).toBe('—')
  })

  it('does not call a heartbeat 119 s old stalled', async () => {
    server.use(heartbeatHandler(119))
    render()

    await screen.findByText('No completion estimate — the server reports a cursor, not a percentage.')
    expect(screen.queryByText('Stalled?')).toBeNull()
    expect(screen.queryByText('stalled?')).toBeNull()
  })

  it('calls a heartbeat 121 s old "Stalled?" — with the question mark, because the inference is ours', async () => {
    server.use(heartbeatHandler(121))
    render()

    expect(await screen.findByText('Stalled?')).toBeInTheDocument()
    expect(screen.getByText(/No heartbeat for over 120 s\. The import may have died/)).toBeInTheDocument()
    expect(
      screen.queryByText('No completion estimate — the server reports a cursor, not a percentage.'),
    ).toBeNull()
  })

  it('polls only while something is running, and says so when nothing is', async () => {
    server.use(heartbeatHandler(4))
    const { unmount } = render()
    expect(await screen.findByText('polling every 10 s while something runs')).toBeInTheDocument()
    unmount()

    server.use(
      http.get('/admin/bootstrap/status', () =>
        HttpResponse.json({
          ...bootstrapStatus,
          runs: bootstrapStatus.runs.filter((run) => run.status !== 'running'),
        }),
      ),
    )
    render()
    expect(await screen.findByText('idle — not polling')).toBeInTheDocument()
  })

  it('treats a failed run as a normal state: bad tone, the error verbatim, the position kept, and "Resume"', async () => {
    const { container } = render()

    // Named twice on purpose: once as the live run, once as the phase it is.
    expect((await screen.findAllByText('crosswalk')).length).toBeGreaterThan(0)
    // The status word in the bad tone, carried by the class the CSS keys on.
    expect(container.querySelector('.u-cursor__status--failed')?.textContent).toBe('failed')
    expect(screen.getByText('wdqs: HTTP 429 after 3 retries (query timeout 60 s)')).toBeInTheDocument()
    expect(screen.getByText('88,140')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument()
  })

  it('confirms an import with four facts, is not red, and raises a Queued receipt carrying the job key', async () => {
    const { user } = render()

    await user.click(await screen.findByRole('button', { name: 'Resume' }))

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Run the Wikidata crosswalk phase?')).toBeInTheDocument()
    expect(within(dialog).getByText('downloads')).toBeInTheDocument()
    expect(within(dialog).getByText('measured')).toBeInTheDocument()
    expect(within(dialog).getByText('writes')).toBeInTheDocument()
    expect(within(dialog).getByText('resumable')).toBeInTheDocument()

    // An import is expensive-but-safe, so the confirm is `primary`, never red.
    const confirm = within(dialog).getByRole('button', { name: 'Resume import' })
    expect(confirm).toHaveClass('u-btn--primary')
    expect(confirm).not.toHaveClass('u-btn--danger-solid')

    await user.click(confirm)

    const toast = await screen.findByRole('status')
    expect(toast.textContent).toContain('Queued')
    expect(toast.textContent).toContain('Queued the crosswalk phase')
    expect(within(toast).getByText('key bootstrap:crosswalk')).toBeInTheDocument()
    expect(within(toast).getByRole('link', { name: /Watch it under Running now/ })).toBeInTheDocument()
  })

  it('states an unmeasured duration rather than inventing a range', async () => {
    const { user } = render()

    // `credit-names` has never run here, so nothing has measured it.
    await user.click((await screen.findAllByRole('button', { name: 'Run' }))[0] as HTMLElement)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Run the Credit names phase?')).toBeInTheDocument()
    expect(within(dialog).getByText('not measured on this deployment')).toBeInTheDocument()
  })

  it('prints a measured duration read off the run that actually finished here', async () => {
    render()

    // movielens: 2026-08-17T23:04:12Z → 23:19:38Z is 15 m 26 s.
    expect(await screen.findByText(/15 m 26 s on this deployment/)).toBeInTheDocument()
  })

  it('prints genome coverage as counts and as a ratio whose denominator is named', async () => {
    render()

    expect(await screen.findByText('128,400 / 130,647 — 98.3%')).toBeInTheDocument()
    expect(screen.getByText(/of enriched titles — the denominator is/)).toBeInTheDocument()
    expect(screen.getByText('128,400 / 1,272,869 — 10.1%')).toBeInTheDocument()
    expect(screen.getByText('enriched_with_vector')).toBeInTheDocument()
  })

  it('states the never-built database as a fact about the database, not as an error', async () => {
    server.use(http.get('/admin/bootstrap/status', () => HttpResponse.json(bootstrapStatusEmpty)))
    render()

    expect(await screen.findByText('The catalog has never been built')).toBeInTheDocument()
    expect(
      screen.getByText(
        'No import has ever run on this deployment, so there is no checkpoint to resume from.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('runs: [] · titles: 0')).toBeInTheDocument()
    // No fabricated ratio out of a zero denominator.
    expect(screen.getAllByText('0 / 0 — no denominator to divide by').length).toBeGreaterThan(0)
    expect(screen.getByText('idle — not polling')).toBeInTheDocument()
  })

  it('renders a failed status read as a panel-scale Problem with code, status and detail', async () => {
    server.use(problemHandler('get', '/admin/bootstrap/status', sourceUnavailable('/admin/bootstrap/status')))
    render()

    expect(await screen.findByText('code source_unavailable')).toBeInTheDocument()
    expect(screen.getByText('HTTP 503')).toBeInTheDocument()
    expect(screen.getByText('Living Room Emby did not answer within 5.0 s.')).toBeInTheDocument()
  })

  it('has no accessibility violations', async () => {
    server.use(heartbeatHandler(4))
    const { container } = render()

    await screen.findByText('No completion estimate — the server reports a cursor, not a percentage.')
    await expectNoViolations(container)
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/admin/bootstrap/status',
          sourceUnavailable('/admin/bootstrap/status'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(
        withTempo(
          <ToastProvider>
            <BootstrapScreen />
            <ToastStack />
          </ToastProvider>,
          tempoUrl,
        ),
        { theme: 'light', density: 'compact', route: ROUTES.bootstrap },
      )
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
  })
})
