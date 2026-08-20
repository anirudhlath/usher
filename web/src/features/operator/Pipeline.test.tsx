import { describe, expect, it } from 'vitest'
import { renderApp, screen, waitFor } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { degradedReadiness } from '@/test/handlers'
import { methodNotAllowed, problemHandler } from '@/test/fixtures'
import { TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES } from '@/app/routes'
import Pipeline from './Pipeline'

/**
 * Pipeline is the one screen in this console that is *entirely* REQUIRES BACKEND
 * WORK, so most of these assertions are about what it refuses to draw. The two
 * that matter most: the four missing routes are on screen in mono, and no cell
 * anywhere carries a count.
 */
function renderPipeline() {
  return renderApp(<Pipeline />, {
    theme: 'light',
    density: 'compact',
    route: ROUTES.pipeline,
  })
}

describe('Pipeline', () => {
  describe('ready', () => {
    it('names the four routes it needs, in mono', async () => {
      const { container } = renderPipeline()

      const routes = container.querySelector('.u-backendwork__routes')
      expect(routes).not.toBeNull()
      expect(routes?.textContent).toContain('GET /admin/jobs?kind=&state=')
      expect(routes?.textContent).toContain('GET /admin/jobs/{key}')
      expect(routes?.textContent).toContain('GET /admin/jobs/stats')
      expect(routes?.textContent).toContain('POST /admin/jobs/{id}/release')

      expect(screen.getByText('Requires backend work')).toBeInTheDocument()
      await waitFor(() => expect(screen.getByText('worker running')).toBeInTheDocument())
    })

    it('shows the nine job kinds and the four priorities as vocabulary', async () => {
      renderPipeline()

      for (const kind of [
        'match',
        'enrich',
        'watch_history',
        'index',
        'derive',
        'curate',
        'watch_writeback',
        'sync',
        'bootstrap',
      ]) {
        expect(screen.getByRole('cell', { name: kind })).toBeInTheDocument()
      }

      expect(screen.getByText('DEMAND 100')).toBeInTheDocument()
      expect(screen.getByText('VISIBLE 80')).toBeInTheDocument()
      expect(screen.getByText('NEW 50')).toBeInTheDocument()
      expect(screen.getByText('BACKFILL 20')).toBeInTheDocument()

      await waitFor(() => expect(screen.getByText('worker running')).toBeInTheDocument())
    })

    it('offers a release control that is inert, because no route is behind it', async () => {
      renderPipeline()
      expect(screen.getByRole('button', { name: 'Release all parked jobs' })).toBeDisabled()
      await waitFor(() => expect(screen.getByText('worker running')).toBeInTheDocument())
    })

    it('says where every 202 receipt points, and why it cannot answer yet', async () => {
      renderPipeline()
      expect(screen.getByText('Where every receipt points')).toBeInTheDocument()
      expect(screen.getByText('GET /admin/jobs/{key} — no route')).toBeInTheDocument()
      await waitFor(() => expect(screen.getByText('worker running')).toBeInTheDocument())
    })
  })

  describe('no fabricated counts', () => {
    it('renders never-fired panels rather than numbers', async () => {
      const { container } = renderPipeline()

      // Every panel would carry a value if one had ever been sampled. None has.
      expect(container.querySelectorAll('.u-panel__value')).toHaveLength(0)
      expect(container.querySelectorAll('.u-panel--never')).toHaveLength(3)
      expect(screen.getAllByText('No sample has ever arrived for this metric.')).toHaveLength(3)
      // Twice each: the panel header and the never-fired body both print it.
      expect(screen.getAllByText('usher.jobs.queued')).toHaveLength(2)
      expect(screen.getAllByText('usher.jobs.parked')).toHaveLength(2)
      expect(screen.getAllByText('usher.jobs.duration')).toHaveLength(2)

      await waitFor(() => expect(screen.getByText('worker running')).toBeInTheDocument())
    })

    it('puts a never-computed block where each missing number would go', async () => {
      renderPipeline()

      expect(screen.getByText('No depth has ever been read')).toBeInTheDocument()
      expect(screen.getByText('No parked job has ever been listed')).toBeInTheDocument()
      expect(
        screen.getByText('GET /admin/jobs/stats · GET /admin/jobs?kind=&state= — neither route exists'),
      ).toBeInTheDocument()
      expect(
        screen.getByText(
          'GET /admin/jobs?state=parked · POST /admin/jobs/{id}/release — neither route exists',
        ),
      ).toBeInTheDocument()

      await waitFor(() => expect(screen.getByText('worker running')).toBeInTheDocument())
    })
  })

  describe('loading', () => {
    it('says the worker lane is arriving without a route-level spinner', () => {
      renderPipeline()
      expect(screen.getByText('Loading the worker lane …')).toBeInTheDocument()
      // The designed shape is on screen the whole time; only the one pending
      // fact is a placeholder.
      expect(screen.getByText('Requires backend work')).toBeInTheDocument()
    })
  })

  describe('empty', () => {
    it('reports a worker lane that is not running without inventing a backlog', async () => {
      server.use(degradedReadiness())
      renderPipeline()

      await waitFor(() => expect(screen.getByText('worker not running')).toBeInTheDocument())
      expect(screen.getByText('No depth has ever been read')).toBeInTheDocument()
      expect(screen.queryByText('worker running')).not.toBeInTheDocument()
    })
  })

  describe('error', () => {
    it('shows code, status and the server detail verbatim', async () => {
      server.use(problemHandler('get', '/health/ready', methodNotAllowed('/health/ready')))
      renderPipeline()

      await waitFor(() =>
        expect(
          screen.getByText('POST is not allowed on this route. Allowed: GET, HEAD.'),
        ).toBeInTheDocument(),
      )
      expect(screen.getByText('code method_not_allowed')).toBeInTheDocument()
      expect(screen.getByText('HTTP 405')).toBeInTheDocument()
      // 405 is a developer error: shown plainly, with no recovery offered.
      expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
      // The rest of the screen survives the failure.
      expect(screen.getByText('Requires backend work')).toBeInTheDocument()
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/health/ready',
          methodNotAllowed('/health/ready'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(withTempo(<Pipeline />, tempoUrl), {
        theme: 'light',
        density: 'compact',
        route: ROUTES.pipeline,
      })
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      const { container } = renderFailed(TEMPO_URL, TRACE_ID)
      await screen.findByText('code method_not_allowed')

      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      const { container } = renderFailed(null, TRACE_ID)
      await screen.findByText('code method_not_allowed')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      const { container } = renderFailed(TEMPO_URL)
      await screen.findByText('code method_not_allowed')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })
  })

  describe('accessibility', () => {
    it('has no axe violations', async () => {
      const { container } = renderPipeline()
      await waitFor(() => expect(screen.getByText('worker running')).toBeInTheDocument())
      await expectNoViolations(container)
    })
  })
})
