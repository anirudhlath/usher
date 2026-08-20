import { describe, expect, it } from 'vitest'
import { Route, Routes } from 'react-router-dom'
import { renderApp, screen, waitFor } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { server } from '@/test/server'
import { notFound, problemHandler, sourceUnavailable, transportFailure } from '@/test/fixtures'
import { TRACE_ID } from '@/test/fixtures/ids'
import { request } from '@/api'
import { ROUTES } from '@/app/routes'
import { NotFound, ScreenProblem } from './NotFound'

/** A `*` route beside one real one, which is how `App.tsx` mounts it. */
function renderNotFound(route: string) {
  return renderApp(
    <Routes>
      <Route path={ROUTES.search} element={<h1>Search</h1>} />
      <Route path="*" element={<NotFound />} />
    </Routes>,
    { route },
  )
}

/**
 * A **real** failure, through the real client, so the `traceresponse` header is
 * parsed by the code that parses it in production rather than by the test.
 */
async function caught(handler: ReturnType<typeof problemHandler>, path: string): Promise<unknown> {
  server.use(handler)
  try {
    await request(path)
  } catch (error) {
    return error
  }
  throw new Error(`${path} did not fail`)
}

function renderScreenProblem(error: unknown, tempoUrl: string | null) {
  return renderApp(withTempo(<ScreenProblem error={error} />, tempoUrl))
}

describe('NotFound', () => {
  it('renders the page-scale not_found treatment with code, status and the route', async () => {
    const { container } = renderNotFound('/titles/not-a-route/extra')

    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent("We couldn't find that.")
    expect(screen.getByText('code not_found')).toBeVisible()
    expect(screen.getByText('HTTP 404')).toBeVisible()
    // `instance` is the route that failed, which is the whole point of showing it.
    expect(screen.getByText('/titles/not-a-route/extra')).toBeVisible()

    await expectNoViolations(container)
  })

  it('offers back and search, and no retry', async () => {
    renderNotFound('/nowhere')

    expect(await screen.findByRole('button', { name: 'Go back' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Search the catalog' })).toBeVisible()

    // patterns.md §3: `not_found` gets **no retry**. The row does not exist and
    // asking again produces the same 404.
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
  })

  it('moves focus to its heading', async () => {
    renderNotFound('/nowhere')

    const heading = await screen.findByRole('heading', { level: 1 })
    await waitFor(() => {
      expect(heading).toHaveFocus()
    })
    // Focusable for the announcement, but not a stop in the tab order.
    expect(heading).toHaveAttribute('tabindex', '-1')
  })

  it('leaves the app chrome intact: a skip link, a real main, and nothing trapped', async () => {
    const { container } = renderNotFound('/nowhere')
    await screen.findByRole('heading', { level: 1 })

    expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main')
    expect(container.querySelector('main#main')).not.toBeNull()
    // No scrim, no dialog, no overlay — the user is not trapped in the error.
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('search is a real way out', async () => {
    const { user } = renderNotFound('/nowhere')

    await user.click(await screen.findByRole('button', { name: 'Search the catalog' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Search' })).toBeVisible()
  })

  it('offers no trace link: the 404 is synthetic and no request was ever made', async () => {
    const { container } = renderNotFound('/nowhere')
    await screen.findByRole('heading', { level: 1 })

    // The router matched nothing. There is no response, no span and nothing in
    // Tempo to open, so there must be no anchor pretending otherwise.
    expect(traceLinks(container)).toHaveLength(0)
    expect(screen.queryByText(/open trace/i)).toBeNull()
  })
})

/**
 * `ScreenProblem` is the wrapper behind About, Collection, Person and Player,
 * so patterns.md §3's trace link is tested once here rather than four times
 * over. The three cases are the whole matrix, and the second is the one worth
 * writing carefully: an unconfigured Tempo must emit **no anchor**, because
 * `<a href="">` navigates to the current page.
 */
describe('ScreenProblem · the trace link (patterns.md §3)', () => {
  it('links into Tempo when the response carried a traceresponse header', async () => {
    const error = await caught(
      problemHandler('get', '/admin/sources', sourceUnavailable('/admin/sources'), {
        traceId: TRACE_ID,
      }),
      '/admin/sources',
    )

    const { container } = renderScreenProblem(error, TEMPO_URL)

    const links = traceLinks(container)
    expect(links).toHaveLength(1)
    expect(links[0]?.getAttribute('href')).toContain(TRACE_ID)
    expect(links[0]).toHaveAttribute('target', '_blank')
    // The id is shown, truncated, so an operator can tell two failures apart.
    expect(links[0]).toHaveTextContent(TRACE_ID.slice(0, 8))

    await expectNoViolations(container)
  })

  it('renders no anchor at all when Tempo is unconfigured, not a dead one', async () => {
    const error = await caught(
      problemHandler('get', '/admin/sources', sourceUnavailable('/admin/sources'), {
        traceId: TRACE_ID,
      }),
      '/admin/sources',
    )

    const { container } = renderScreenProblem(error, null)

    // The response *did* carry an id. There is simply nowhere to send it, and
    // absent beats dead: a link to "" costs a click to discover it does nothing.
    expect(traceLinks(container)).toHaveLength(0)
    expect(deadLinks(container)).toHaveLength(0)
    expect(screen.queryByText(/open trace/i)).toBeNull()
    // The failure itself is still rendered in full.
    expect(screen.getByText('code source_unavailable')).toBeVisible()
  })

  it('renders no anchor when Tempo is configured but the response carried no header', async () => {
    const error = await caught(
      problemHandler('get', '/admin/sources', sourceUnavailable('/admin/sources')),
      '/admin/sources',
    )

    const { container } = renderScreenProblem(error, TEMPO_URL)

    expect(traceLinks(container)).toHaveLength(0)
    expect(deadLinks(container)).toHaveLength(0)
    expect(screen.getByText('code source_unavailable')).toBeVisible()
  })

  it('renders no anchor for a transport failure, which never reached a span', async () => {
    server.use(transportFailure('get', '/admin/sources'))
    let error: unknown = null
    try {
      await request('/admin/sources')
    } catch (caughtError) {
      error = caughtError
    }

    const { container } = renderScreenProblem(error, TEMPO_URL)

    // Not an `UsherProblem` at all: `fetch` rejected, so there is no response
    // and no header. Inventing an id here would be the one thing worse than
    // no link.
    expect(traceLinks(container)).toHaveLength(0)
    expect(deadLinks(container)).toHaveLength(0)
    expect(screen.getByText("We couldn't reach the server.")).toBeVisible()
  })

  it('carries the link on a page-scale not_found too, beside back and search', async () => {
    const error = await caught(
      problemHandler('get', '/admin/sources', notFound('/admin/sources'), { traceId: TRACE_ID }),
      '/admin/sources',
    )

    const { container } = renderScreenProblem(error, TEMPO_URL)

    expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    expect(screen.getByRole('button', { name: 'Go back' })).toBeVisible()
    // Still no retry: `not_found` is not retryable however good the telemetry is.
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })
})
