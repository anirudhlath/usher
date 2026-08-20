import { describe, expect, it } from 'vitest'
import { HttpResponse, delay, http } from 'msw'
import { renderApp, screen, waitFor } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { browseSinglePage } from '@/test/handlers'
import {
  browseEmpty,
  browsePageOne,
  browseUnpredicated,
  facetsNotRequested,
  invalidCursor,
  problemHandler,
  sourceUnavailable,
  traceResponse,
  problemResponse,
  validationFailed,
} from '@/test/fixtures'
import { TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import Browse from './Browse'

function rowCount(container: HTMLElement): number {
  return container.querySelectorAll('.u-row').length
}

describe('Browse', () => {
  describe('ready', () => {
    it('lists the page it was given, with no total anywhere', async () => {
      const { container } = renderApp(<Browse />, { route: '/browse' })

      expect(screen.getByRole('heading', { level: 1, name: 'Browse' })).toBeInTheDocument()
      await waitFor(() => expect(rowCount(container)).toBe(4))
      expect(screen.getByRole('button', { name: /Stalker/ })).toBeInTheDocument()

      await expectNoViolations(container)
    })

    it('renders no result count, no total and no page number', async () => {
      const { container } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      // "N results", "N of M" and "page N" are the three shapes §4 forbids.
      expect(screen.queryAllByText(/\d[\d,]*\s+results?\b/i)).toHaveLength(0)
      expect(screen.queryAllByText(/\b\d[\d,]*\s+of\s+\d/)).toHaveLength(0)
      expect(screen.queryAllByText(/\bpage\s+\d/i)).toHaveLength(0)
      expect(screen.queryByRole('navigation')).toBeNull()

      // What is allowed is a count of what has *loaded*, with no denominator.
      expect(screen.getByText('4 loaded so far · there may be more')).toBeInTheDocument()
      // The catalog's own size is a real denominator from bootstrap status, and
      // it is explicitly not a count of this result set.
      expect(
        screen.getByText('1,272,869 titles in the catalog. Results are not counted.'),
      ).toBeInTheDocument()
    })

    it('states that a list has ended rather than stopping silently', async () => {
      server.use(browseSinglePage())

      const { container } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(2))

      expect(screen.getByText('That is everything we have for this filter.')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    })

    it('renders unmeasured popularity as never measured rather than as zero', async () => {
      const { container } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      // Solaris has `popularity: null` and `vote_count: null`.
      expect(screen.getByText('— popularity never measured')).toBeInTheDocument()
      expect(screen.getByText('— never rated')).toBeInTheDocument()
      expect(screen.queryAllByText('popularity 0.00')).toHaveLength(0)
    })
  })

  describe('loading', () => {
    it('is a table-shaped skeleton of eight rows, never a spinner', async () => {
      server.use(
        http.get('/browse', async () => {
          await delay(40)
          return HttpResponse.json(browseUnpredicated)
        }),
      )

      const { container } = renderApp(<Browse />, { route: '/browse' })

      expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
      expect(screen.getByText('Loading the catalog …')).toBeInTheDocument()
      expect(container.querySelectorAll('.u-skel-table__row')).toHaveLength(8)
      expect(container.querySelector('[role="progressbar"]')).toBeNull()

      await expectNoViolations(container)
      await waitFor(() => expect(rowCount(container)).toBe(4))
    })
  })

  describe('empty', () => {
    it('names the fields that prove the list is empty rather than missing', async () => {
      server.use(http.get('/browse', () => HttpResponse.json(browseEmpty)))

      const { container } = renderApp(<Browse />, { route: '/browse?year=1893' })

      expect(await screen.findByText('No titles match this filter')).toBeInTheDocument()
      expect(screen.getByText('items: [] · next_cursor: null')).toBeInTheDocument()
      expect(screen.getByText(/none of them match this filter/)).toBeInTheDocument()

      await expectNoViolations(container)
    })
  })

  describe('error', () => {
    it('shows code, status and the server detail, and offers no retry for a 422', async () => {
      server.use(
        problemHandler(
          'get',
          '/browse',
          validationFailed('/browse', {
            errors: [{ loc: ['query', 'year'], msg: 'value is not a valid integer' }],
          }),
        ),
      )

      const { container } = renderApp(<Browse />, { route: '/browse?year=nineteen' })

      expect(await screen.findByText('The request body failed validation.')).toBeInTheDocument()
      expect(screen.getByText('code validation_failed')).toBeInTheDocument()
      expect(screen.getByText('HTTP 422')).toBeInTheDocument()
      // `errors[].loc` names the field, which is the whole recovery for a 422.
      expect(screen.getByText(/value is not a valid integer/)).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()

      await expectNoViolations(container)
    })

    it('never renders invalid_cursor — it restarts the list from the top', async () => {
      server.use(
        http.get('/browse', ({ request }) => {
          const cursor = new URL(request.url).searchParams.get('cursor')
          if (cursor !== null) return problemResponse(invalidCursor('/browse'))
          return HttpResponse.json(browsePageOne)
        }),
      )

      const { container, user } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      await user.click(screen.getByRole('button', { name: 'Load more' }))

      // Back to page one, with nothing said: the user changed nothing and has
      // nothing to fix.
      await waitFor(() => expect(rowCount(container)).toBe(4))
      expect(screen.queryAllByText(/invalid_cursor/)).toHaveLength(0)
      expect(screen.queryAllByText(/does not match this query/)).toHaveLength(0)
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/browse',
          sourceUnavailable('/browse'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(withTempo(<Browse />, tempoUrl), { route: '/browse' })
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

    it('carries the link on a page that failed after the first, beside the list that stands', async () => {
      // Page one lands, page two fails: a second call site, and the one where
      // the failure sits *beside* real items rather than replacing them.
      server.use(
        http.get('/browse', ({ request }) => {
          const cursor = new URL(request.url).searchParams.get('cursor')
          if (cursor === null) return HttpResponse.json(browsePageOne)
          return problemResponse(sourceUnavailable('/browse'), { traceresponse: traceResponse() })
        }),
      )

      const { container, user } = renderApp(withTempo(<Browse />, TEMPO_URL), { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      await user.click(screen.getByRole('button', { name: 'Load more' }))

      await screen.findByText('code source_unavailable')
      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
      // The items that did arrive are still on screen.
      expect(rowCount(container)).toBe(4)
    })
  })

  describe('facets', () => {
    it('explains an unfiltered catalog with the unpredicated sentence', async () => {
      const { container } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      expect(screen.getByText('Facet counts unavailable')).toBeInTheDocument()
      expect(screen.getByText(/Counts are only computed once a filter is set/)).toBeInTheDocument()
      expect(screen.getByText('facets: { computed: false, reason: "unpredicated" }')).toBeInTheDocument()
      expect(screen.queryAllByText(/Facets were not requested/)).toHaveLength(0)
    })

    it('uses a different sentence when the server was never asked', async () => {
      server.use(
        http.get('/browse', () => HttpResponse.json({ ...browsePageOne, facets: facetsNotRequested })),
      )

      const { container } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      expect(screen.getByText(/Facets were not requested/)).toBeInTheDocument()
      expect(screen.getByText('facets: { computed: false, reason: "not_requested" }')).toBeInTheDocument()
      expect(screen.queryAllByText(/Counts are only computed once a filter is set/)).toHaveLength(0)
    })

    it('shows real counts once a filter is set, against the filter set', async () => {
      const { container } = renderApp(<Browse />, { route: '/browse?genre=Drama' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      expect(screen.getByText('412')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Science Fiction/ })).toBeInTheDocument()
      expect(
        screen.getByText('Counts are for the current filter set, not the whole catalog.'),
      ).toBeInTheDocument()
      expect(screen.queryAllByText('Facet counts unavailable')).toHaveLength(0)
    })
  })

  describe('filters', () => {
    it('cycles the owned filter through three states, and is not a checkbox', async () => {
      const { container, user } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      const chip = screen.getByRole('button', { name: /Owned/ })
      expect(screen.queryByRole('checkbox')).toBeNull()
      expect(chip).toHaveAttribute('aria-pressed', 'false')
      expect(chip).toHaveTextContent('Either')

      await user.click(chip)
      expect(screen.getByRole('button', { name: /Owned/ })).toHaveTextContent('Owned')
      expect(screen.getByRole('button', { name: /Owned/ })).toHaveAttribute('aria-pressed', 'true')

      await user.click(screen.getByRole('button', { name: /Owned/ }))
      expect(screen.getByRole('button', { name: /Owned/ })).toHaveTextContent('Not owned')

      await user.click(screen.getByRole('button', { name: /Owned/ }))
      expect(screen.getByRole('button', { name: /Owned/ })).toHaveTextContent('Either')
    })

    it('discards accumulated pages and restarts from the top when a filter changes', async () => {
      const { container, user } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      await user.click(screen.getByRole('button', { name: 'Load more' }))
      await waitFor(() => expect(rowCount(container)).toBe(6))

      await user.click(screen.getByRole('button', { name: /Owned/ }))

      // Page one only. An outstanding cursor cannot survive a filter change.
      await waitFor(() => expect(rowCount(container)).toBe(4))
    })

    it('switches density without restarting the list', async () => {
      const { container, user } = renderApp(<Browse />, { route: '/browse' })
      await waitFor(() => expect(rowCount(container)).toBe(4))

      await user.click(screen.getByRole('button', { name: 'Grid density' }))

      await waitFor(() => expect(container.querySelectorAll('.u-card--poster')).toHaveLength(4))
      expect(rowCount(container)).toBe(0)
      expect(screen.getByRole('button', { name: 'Grid density' })).toHaveAttribute('aria-pressed', 'true')
    })
  })
})
