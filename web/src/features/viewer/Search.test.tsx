import { afterEach, describe, expect, it, vi } from 'vitest'
import { HttpResponse, delay, http } from 'msw'
import { act, renderApp, screen, waitFor, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { notFound, problemHandler, searchEmpty } from '@/test/fixtures'
import { TITLE_ENRICHED, TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { FakeEventSource } from '@/test/sse'
import Search from './Search'

/**
 * The one sentence §14 fixes verbatim: a coverage figure carries its
 * denominator or it does not ship, and the denominator is the *enriched* tier
 * rather than the catalog.
 */
const DENOMINATOR = /— of 130,647 enriched titles, not of the 1,272,869-row catalog\./

let restore: (() => void) | null = null

afterEach(() => {
  restore?.()
  restore = null
})

function useFakeEventSource(): () => FakeEventSource {
  const previous = globalThis.EventSource
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  restore = () => vi.stubGlobal('EventSource', previous)
  return () => {
    const latest = FakeEventSource.instances.at(-1)
    if (latest === undefined) throw new Error('no EventSource was constructed')
    return latest
  }
}

function field(): HTMLElement {
  return screen.getByRole('combobox', { name: 'Search the catalog' })
}

describe('Search', () => {
  describe('nothing asked yet', () => {
    it('says so rather than showing an empty result list', async () => {
      const { container } = renderApp(<Search />, { route: '/search' })

      expect(screen.getByRole('heading', { level: 1, name: 'Search' })).toBeInTheDocument()
      expect(screen.getByText('Nothing searched yet')).toBeInTheDocument()
      expect(screen.getByText('q: null')).toBeInTheDocument()

      await expectNoViolations(container)
    })
  })

  describe('ready', () => {
    it('renders the answer with what actually ran', async () => {
      const { container } = renderApp(<Search />, { route: '/search?q=tarkovsky' })

      expect(await screen.findByRole('button', { name: /Stalker/ })).toBeInTheDocument()
      expect(screen.getByText('mode fused')).toBeInTheDocument()
      expect(screen.getByText('search_id 0191f4ca-517a-73a8-9741-1e85c96ab28c')).toBeInTheDocument()
      // PRD 05 requires unowned results to be surfaced clearly marked, so the
      // badge is per row rather than a filter the reader has to apply.
      expect(screen.getAllByText('owned')).toHaveLength(2)
      expect(screen.getByText('catalog only')).toBeInTheDocument()

      await expectNoViolations(container)
    })

    it('prints semantic_coverage against its real denominator', async () => {
      renderApp(<Search />, { route: '/search?q=tarkovsky' })

      const coverage = await screen.findByText(DENOMINATOR, { selector: 'span' })
      expect(coverage).toHaveTextContent('semantic_coverage 0.98')

      // Never a share of the library, which is the fabrication §14 exists to
      // forbid: 0.98 of the enriched tier is ~10% of the catalog.
      expect(screen.queryAllByText(/98\s*%/)).toHaveLength(0)
      expect(screen.queryAllByText(/of the library/i)).toHaveLength(0)
    })

    it('treats a sparse result as a real row rather than as damage', async () => {
      renderApp(<Search />, { route: '/search?q=tarkovsky' })

      await screen.findByRole('button', { name: /Stalker/ })
      // Solaris carries `popularity: null`, and null is not zero.
      expect(screen.getByText('— popularity never measured')).toBeInTheDocument()
      expect(screen.queryAllByText('popularity 0.00')).toHaveLength(0)
      expect(
        screen.getByText(/Skeleton results are real catalog rows with a name and a year/),
      ).toBeInTheDocument()
    })

    it('prints the expansion the server actually used', async () => {
      renderApp(<Search />, { route: '/search?q=tarkovsky' })

      await screen.findByRole('button', { name: /Stalker/ })
      expect(screen.getByText('tarkovsky andrei soviet science fiction contemplative')).toBeInTheDocument()
    })
  })

  describe('the lane that ran', () => {
    it('says so when the server narrowed the mode', async () => {
      renderApp(<Search />, { route: '/search?q=something&mode=semantic' })

      expect(await screen.findByText('We narrowed this to lexical search.')).toBeInTheDocument()
      const notice = screen.getByRole('status')
      expect(within(notice).getByText('semantic')).toBeInTheDocument()
      expect(within(notice).getByText('full_text')).toBeInTheDocument()
    })

    it('does not print a coverage figure when no vector lane ran', async () => {
      renderApp(<Search />, { route: '/search?q=tarkovsky&mode=full_text' })

      await screen.findByRole('button', { name: /Stalker/ })
      // `semantic_coverage: 0.0` on a lexical answer is a fact about the lane,
      // not a measurement of coverage — §2's not-applicable treatment.
      expect(
        screen.getByText(/the lexical lane consults no vectors, so the field reads 0\.0/),
      ).toBeInTheDocument()
      expect(screen.queryAllByText(DENOMINATOR)).toHaveLength(0)
    })

    it('states that expansion is off when expanded_query is null', async () => {
      renderApp(<Search />, { route: '/search?q=tarkovsky&mode=full_text' })

      await screen.findByRole('button', { name: /Stalker/ })
      expect(screen.getByText(/Query expansion is off/)).toBeInTheDocument()
    })
  })

  describe('suggest', () => {
    it('shows both tiers under their own group headers, never merged', async () => {
      const { user } = renderApp(<Search />, { route: '/search' })

      await user.type(field(), 'stal')

      expect(
        await screen.findByText('Starts with · answers every keystroke at 4+ characters'),
      ).toBeInTheDocument()
      expect(await screen.findByText('Close matches · trigram + Levenshtein, debounced')).toBeInTheDocument()

      // Two queries against two indexes, so two groups. One merged list would
      // present a trigram hit as a worse prefix hit.
      await waitFor(() => expect(screen.getAllByRole('group')).toHaveLength(2))
      expect(screen.getAllByRole('option').length).toBeGreaterThanOrEqual(3)
    })

    it('submits free text to the URL, which is the source of truth', async () => {
      const { user } = renderApp(<Search />, { route: '/search' })

      await user.type(field(), 'tarkovsky{Enter}')

      expect(await screen.findByRole('button', { name: /Stalker/ })).toBeInTheDocument()
      expect(screen.getByText('mode fused')).toBeInTheDocument()
    })
  })

  describe('loading', () => {
    it('is a table-shaped skeleton of six rows, never a spinner', async () => {
      server.use(
        http.get('/search', async () => {
          await delay(40)
          return HttpResponse.json(searchEmpty)
        }),
      )

      const { container } = renderApp(<Search />, { route: '/search?q=tarkovsky' })

      expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
      expect(screen.getByText('Searching …')).toBeInTheDocument()
      expect(container.querySelectorAll('.u-skel-table__row')).toHaveLength(6)
      expect(container.querySelector('[role="progressbar"]')).toBeNull()

      await expectNoViolations(container)
      await screen.findByText('Nothing matched')
    })
  })

  describe('empty', () => {
    it('names the mode and the empty list that prove nothing matched', async () => {
      server.use(http.get('/search', () => HttpResponse.json(searchEmpty)))

      const { container } = renderApp(<Search />, { route: '/search?q=zzzzzzzz' })

      expect(await screen.findByText('Nothing matched')).toBeInTheDocument()
      expect(screen.getByText('mode: "fused" · results: []')).toBeInTheDocument()
      expect(screen.getByText(/Both lanes answered and neither found a match/)).toBeInTheDocument()

      await expectNoViolations(container)
    })
  })

  describe('error', () => {
    it('shows code, status and the server detail at page scale, with a way out', async () => {
      server.use(problemHandler('get', '/search', notFound('/search')))

      const { container } = renderApp(<Search />, { route: '/search?q=qqxzz' })

      expect(
        await screen.findByRole('heading', { level: 1, name: "We couldn't find that." }),
      ).toBeInTheDocument()
      expect(screen.getByText('code not_found')).toBeInTheDocument()
      expect(screen.getByText('HTTP 404')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Browse instead' })).toBeInTheDocument()
      // 404 is not retryable: the row does not exist.
      expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()

      await expectNoViolations(container)
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler('get', '/search', notFound('/search'), traceId === undefined ? {} : { traceId }),
      )
      return renderApp(withTempo(<Search />, tempoUrl), { route: '/search?q=qqxzz' })
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      const { container } = renderFailed(TEMPO_URL, TRACE_ID)
      await screen.findByText('code not_found')

      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      const { container } = renderFailed(null, TRACE_ID)
      await screen.findByText('code not_found')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      const { container } = renderFailed(TEMPO_URL)
      await screen.findByText('code not_found')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })
  })

  describe('live', () => {
    it('is fully correct when zero frames arrive', async () => {
      const latest = useFakeEventSource()
      renderApp(<Search />, { route: '/search?q=tarkovsky' })

      await screen.findByRole('button', { name: /Stalker/ })
      expect(latest().listenerCount()).toBeGreaterThan(0)
      expect(document.querySelectorAll('.u-row--patched')).toHaveLength(0)
    })

    it('patches a row in place without reordering the answer', async () => {
      const latest = useFakeEventSource()
      const { container } = renderApp(<Search />, { route: '/search?q=tarkovsky' })

      await screen.findByRole('button', { name: /Stalker/ })
      const before = Array.from(container.querySelectorAll('.u-row__title')).map((node) => node.textContent)

      act(() => {
        latest().emit('title.updated', {
          title_id: TITLE_ENRICHED,
          episode_id: null,
          fields: ['overview'],
        })
      })

      expect(screen.getByRole('button', { name: /Stalker/ })).toHaveClass('u-row--patched')
      expect(Array.from(container.querySelectorAll('.u-row__title')).map((node) => node.textContent)).toEqual(
        before,
      )
    })
  })
})
