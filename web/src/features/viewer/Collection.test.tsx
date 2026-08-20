import { describe, expect, it } from 'vitest'
import { Route, Routes } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import type { CollectionResponse } from '@/api'
import { renderApp, screen, waitFor } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { collection, collectionUnowned, problemHandler, sourceUnavailable } from '@/test/fixtures'
import { COLLECTION_TRILOGY, TITLE_MISSING, TITLE_SERIES, TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES, collectionPath } from '@/app/routes'
import Collection from './Collection'

function renderCollection(collectionId: string) {
  return renderApp(
    <Routes>
      <Route path={ROUTES.collection} element={<Collection />} />
    </Routes>,
    { route: collectionPath(collectionId) },
  )
}

/** Member names in DOM order, which is the order the repository returned. */
function memberOrder(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.u-card__title')].map((node) => node.textContent ?? '')
}

describe('Collection', () => {
  it('shows completion as a share, because both of its numbers are given', async () => {
    const { container } = renderCollection(COLLECTION_TRILOGY)

    expect(await screen.findByRole('heading', { level: 1, name: 'The Zone Trilogy' })).toBeVisible()
    expect(screen.getByText('2 of 3')).toBeVisible()
    expect(screen.getByText('owned · 67% complete')).toBeVisible()

    // The bar carries the same claim in words, never in hue alone.
    const bar = screen.getByRole('progressbar', { name: 'Collection completion' })
    expect(bar).toHaveAttribute('aria-valuetext', '2 of 3 owned — 67% complete')
    expect(bar).toHaveAttribute('aria-valuenow', '67')

    // And it says why it is allowed to exist here and nowhere else.
    expect(screen.getByText(/a percentage is honest here and nowhere else in this product/i)).toBeVisible()

    await expectNoViolations(container)
  })

  it('keeps members in release order and dims the unowned in place rather than hiding them', async () => {
    const { container } = renderCollection(COLLECTION_TRILOGY)
    await screen.findByRole('heading', { level: 1, name: 'The Zone Trilogy' })

    expect(memberOrder(container)).toEqual(['Solaris', 'The Mirror', 'Stalker'])

    // Solaris is the unowned one and it is also the first by release date:
    // sorting the owned to the top would turn a timeline into two piles.
    const solaris = screen.getByRole('button', { name: 'Solaris, 1972' })
    expect(solaris).toHaveClass('u-card--unowned')
    expect(screen.getByText('not owned')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Stalker, 1979' })).not.toHaveClass('u-card--unowned')
  })

  it('renders 0 of 3 as a real number rather than as an empty state', async () => {
    const { container } = renderCollection(collectionUnowned.id)

    expect(await screen.findByText('0 of 3')).toBeVisible()
    expect(screen.getByText('owned · 0% complete')).toBeVisible()
    expect(memberOrder(container)).toHaveLength(3)
  })

  it('shows a skeleton shaped like the grid while the record is pending', async () => {
    const { container } = renderCollection(COLLECTION_TRILOGY)

    expect(screen.getByText('Loading this collection …')).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()

    await screen.findByRole('heading', { level: 1, name: 'The Zone Trilogy' })
  })

  it('treats an empty collection as not applicable, not as a failure', async () => {
    const empty: CollectionResponse = { ...collection, owned_count: 0, total_count: 0, titles: [] }
    server.use(http.get('/collections/:collection_id', () => HttpResponse.json(empty)))

    const { container } = renderCollection(COLLECTION_TRILOGY)

    expect(
      await screen.findByText(/Collections are films only — a television library correctly never gets one/i),
    ).toBeVisible()
    // No denominator to divide by, so no share is invented from one.
    expect(container.textContent ?? '').not.toContain('%')
    expect(screen.getByText('0 of 0')).toBeVisible()

    await expectNoViolations(container)
  })

  it('gives a series reaching this screen the not-applicable treatment', async () => {
    const withSeries: CollectionResponse = {
      ...collection,
      titles: [
        ...collection.titles,
        {
          title_id: TITLE_SERIES,
          kind: 'series',
          name: 'Twin Peaks',
          year: 1990,
          enrichment_state: 'enriched',
          owned: true,
        },
      ],
    }
    server.use(http.get('/collections/:collection_id', () => HttpResponse.json(withSeries)))

    const { container } = renderCollection(COLLECTION_TRILOGY)
    await screen.findByRole('heading', { level: 1, name: 'The Zone Trilogy' })

    expect(screen.getByText(/1 member of this record is not a film/i)).toBeVisible()
    expect(screen.queryByRole('button', { name: /Twin Peaks/ })).toBeNull()
    expect(memberOrder(container)).toEqual(['Solaris', 'The Mirror', 'Stalker'])
  })

  it('renders a 404 at page scale with back and search, and no retry', async () => {
    const { container } = renderCollection(TITLE_MISSING)

    expect(await screen.findByRole('button', { name: 'Go back' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Search the catalog' })).toBeVisible()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
    expect(screen.getByText('code not_found')).toBeVisible()
    expect(screen.getByText('HTTP 404')).toBeVisible()

    await expectNoViolations(container)
  })

  it('reports a transport failure as having no status', async () => {
    server.use(http.get('/collections/:collection_id', () => HttpResponse.error()))
    renderCollection(COLLECTION_TRILOGY)

    await waitFor(() => {
      expect(screen.getByText("We couldn't reach the server.")).toBeVisible()
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    /**
     * This screen reaches `Problem` through `ScreenProblem`, so this proves the
     * wrapper's wiring on a real screen; `NotFound.test.tsx` carries the full
     * matrix for the wrapper itself.
     */
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/collections/:collection_id',
          sourceUnavailable('/collections/…'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(
        withTempo(
          <Routes>
            <Route path={ROUTES.collection} element={<Collection />} />
          </Routes>,
          tempoUrl,
        ),
        { route: collectionPath(COLLECTION_TRILOGY) },
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
