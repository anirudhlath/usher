import { afterEach, describe, expect, it, vi } from 'vitest'
import { HttpResponse, delay, http } from 'msw'
import { act, createTestQueryClient, renderApp, screen, waitFor, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { home, homeEmpty, problemHandler, sourceUnavailable } from '@/test/fixtures'
import { TITLE_ENRICHED, TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { FakeEventSource } from '@/test/sse'
import Home from './Home'

/**
 * The detail a partial `/home` answers with. It is the **server's** sentence —
 * the client has no way to count rows that never arrived, so the number of
 * dropped rows can only come from the response, and §3 requires it printed
 * verbatim and never parsed.
 */
const DROPPED_ROWS =
  'Three rows timed out while building and were dropped. What you see is complete; what is missing is missing, not empty.'

let restore: (() => void) | null = null

afterEach(() => {
  restore?.()
  restore = null
})

/** Swaps `EventSource` without `unstubAllGlobals`, which would also take out
 *  `setup.ts`'s `IntersectionObserver` and `matchMedia` for the rest of the file. */
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

describe('Home', () => {
  describe('ready', () => {
    it('prints every row title and every row reason, and invents none', async () => {
      const { container } = renderApp(<Home />)

      await screen.findByRole('region', { name: 'Continue watching' })

      // Premise guard: this assertion is only worth anything if the fixture
      // actually carries both cases.
      const explained = home.rows.filter((row) => row.reason !== null)
      const unexplained = home.rows.filter((row) => row.reason === null)
      expect(explained.length).toBeGreaterThan(0)
      expect(unexplained.length).toBeGreaterThan(0)

      for (const row of explained) {
        const rail = screen.getByRole('region', { name: row.title })
        expect(within(rail).getByText(String(row.reason))).toBeInTheDocument()
      }
      for (const row of unexplained) {
        // `reason: null` is a real state. A row assembled by a `SELECT` has no
        // explanation to give, and "picked for you" would be a fabrication.
        const rail = screen.getByRole('region', { name: row.title })
        expect(rail.querySelector('.u-rail__reason')).toBeNull()
      }

      await expectNoViolations(container)
    })

    it('picks the card shape from display_hint and nothing else', async () => {
      renderApp(<Home />)

      const landscape = await screen.findByRole('region', { name: 'Continue watching' })
      const portrait = screen.getByRole('region', { name: 'Because you watched Andrei Tarkovsky' })

      expect(landscape.querySelectorAll('.u-card--landscape')).toHaveLength(2)
      expect(landscape.querySelectorAll('.u-card--poster')).toHaveLength(0)
      expect(portrait.querySelectorAll('.u-card--poster')).toHaveLength(3)
      expect(portrait.querySelectorAll('.u-card--landscape')).toHaveLength(0)
    })

    it('draws continue-watching progress with a real denominator', async () => {
      renderApp(<Home />)

      const rail = await screen.findByRole('region', { name: 'Continue watching' })

      const bars = within(rail).getAllByRole('progressbar')
      expect(bars).toHaveLength(2)
      // 3,142 s of 9,720 s. A card with no runtime would say so instead of
      // dividing by a number nobody has.
      expect(bars[0]).toHaveAttribute('aria-valuetext', '52 of 162 min watched')
    })

    it('composes one accessible name per card rather than nesting a play button', async () => {
      renderApp(<Home />)

      const rail = await screen.findByRole('region', { name: 'Continue watching' })
      expect(
        within(rail).getByRole('button', { name: 'Twin Peaks, S1E1 · Pilot, partly watched' }),
      ).toBeInTheDocument()
      // Two cards, two buttons. A nested play control would make it four.
      expect(within(rail).getAllByRole('button')).toHaveLength(2)
    })
  })

  describe('loading', () => {
    it('is a rail-shaped skeleton, three rows of six, never a spinner', async () => {
      server.use(
        http.get('/home', async () => {
          await delay(40)
          return HttpResponse.json(home)
        }),
      )

      const { container } = renderApp(<Home />)

      const region = container.querySelector('[aria-busy="true"]')
      expect(region).not.toBeNull()
      expect(screen.getByText('Loading your home screen …')).toBeInTheDocument()
      expect(container.querySelectorAll('.u-skel-rail')).toHaveLength(3)
      expect(container.querySelectorAll('.u-skel-rail > div')).toHaveLength(18)
      // §1: the skeleton itself is hidden from assistive tech; the region owns
      // the announcement.
      for (const skeleton of container.querySelectorAll('.u-skel-rail')) {
        expect(skeleton).toHaveAttribute('aria-hidden', 'true')
      }
      expect(container.querySelector('[role="progressbar"]')).toBeNull()

      await expectNoViolations(container)
      await screen.findByRole('region', { name: 'Continue watching' })
    })

    it('shows cached rows while revalidating, never a skeleton', async () => {
      const queryClient = createTestQueryClient()
      queryClient.setQueryData(['home'], home)
      // Stale, so mounting revalidates: this is the exact §1 case — a cached
      // surface with a request in flight.
      void queryClient.invalidateQueries({ queryKey: ['home'], refetchType: 'none' })

      const { container } = renderApp(<Home />, { queryClient })

      expect(screen.getByRole('region', { name: 'Continue watching' })).toBeInTheDocument()
      expect(container.querySelector('[aria-busy="true"]')).toBeNull()
      expect(container.querySelector('.u-skel-rail')).toBeNull()

      await waitFor(() => expect(queryClient.isFetching()).toBe(0))
    })
  })

  describe('empty', () => {
    it('says every provider returned empty rather than padding the screen', async () => {
      server.use(http.get('/home', () => HttpResponse.json(homeEmpty)))

      const { container } = renderApp(<Home />)

      expect(
        await screen.findByRole('heading', { level: 1, name: 'Nothing to show you yet' }),
      ).toBeInTheDocument()
      // The field that proves the claim, which §2 forbids dropping for tidiness.
      expect(screen.getByText('rows: []')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Browse the catalog' })).toBeInTheDocument()

      await expectNoViolations(container)
    })
  })

  describe('error', () => {
    it('renders code, status and the server detail verbatim at page scale', async () => {
      server.use(problemHandler('get', '/home', sourceUnavailable('/home')))

      const { container } = renderApp(<Home />)

      expect(
        await screen.findByRole('heading', {
          level: 1,
          name: "We couldn't build your home screen.",
        }),
      ).toBeInTheDocument()
      expect(screen.getByText('Living Room Emby did not answer within 5.0 s.')).toBeInTheDocument()
      expect(screen.getByText('code source_unavailable')).toBeInTheDocument()
      expect(screen.getByText('HTTP 503')).toBeInTheDocument()
      expect(screen.getByText('/home')).toBeInTheDocument()

      await expectNoViolations(container)
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    /** `/home` fails; the page-scale `Problem` is what renders. */
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler('get', '/home', sourceUnavailable('/home'), traceId === undefined ? {} : { traceId }),
      )
      return renderApp(withTempo(<Home />, tempoUrl))
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      const { container } = renderFailed(TEMPO_URL, TRACE_ID)
      await screen.findByText('code source_unavailable')

      const links = traceLinks(container)
      expect(links).toHaveLength(1)
      expect(links[0]?.getAttribute('href')).toContain(TRACE_ID)
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

  describe('degraded', () => {
    it('keeps the rows it has and says what was dropped, in the server’s words', async () => {
      const queryClient = createTestQueryClient()
      renderApp(<Home />, { queryClient })
      await screen.findByRole('region', { name: 'Continue watching' })

      server.use(problemHandler('get', '/home', sourceUnavailable('/home', { detail: DROPPED_ROWS })))
      await act(async () => {
        await queryClient.refetchQueries({ queryKey: ['home'] })
      })

      expect(await screen.findByText(DROPPED_ROWS)).toBeInTheDocument()
      expect(screen.getByText('Showing a partial home screen.')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Rebuild rows' })).toBeInTheDocument()
      // What survived is still on screen: dropping it to show the failure would
      // lose rows that arrived.
      expect(screen.getByRole('region', { name: 'Continue watching' })).toBeInTheDocument()
    })
  })

  describe('keyboard', () => {
    it('moves focus along a rail with the arrow keys and never uses scrollIntoView', async () => {
      const scrollIntoView = vi.fn<() => void>()
      const previous = Element.prototype.scrollIntoView
      Element.prototype.scrollIntoView = scrollIntoView

      try {
        const { user } = renderApp(<Home />)
        const rail = await screen.findByRole('region', { name: 'Continue watching' })
        const cards = within(rail).getAllByRole('button')

        cards[0]?.focus()
        expect(document.activeElement).toBe(cards[0])

        await user.keyboard('{ArrowRight}')
        expect(document.activeElement).toBe(cards[1])

        await user.keyboard('{ArrowLeft}')
        expect(document.activeElement).toBe(cards[0])

        // Clamped, not wrapped: holding a key never cycles past the boundary.
        await user.keyboard('{ArrowLeft}')
        expect(document.activeElement).toBe(cards[0])

        expect(scrollIntoView).not.toHaveBeenCalled()
      } finally {
        Element.prototype.scrollIntoView = previous
      }
    })
  })

  describe('live', () => {
    it('is fully correct when zero frames arrive', async () => {
      const latest = useFakeEventSource()
      renderApp(<Home />)

      await screen.findByRole('region', { name: 'Continue watching' })
      // Constructed, never opened, never delivered anything — which is what a
      // lossy in-process bus with nobody publishing looks like.
      expect(latest().listenerCount()).toBeGreaterThan(0)
      expect(document.querySelectorAll('.u-card--patched')).toHaveLength(0)
      expect(screen.getByRole('region', { name: 'Recently added' })).toBeInTheDocument()
    })

    it('patches a card in place without moving it or stealing focus', async () => {
      const latest = useFakeEventSource()
      renderApp(<Home />)

      const rail = await screen.findByRole('region', {
        name: 'Because you watched Andrei Tarkovsky',
      })
      const before = within(rail)
        .getAllByRole('button')
        .map((card) => card.getAttribute('aria-label'))
      const solaris = within(rail).getByRole('button', { name: 'Solaris, 1972' })
      solaris.focus()

      act(() => {
        latest().emit('title.updated', {
          title_id: TITLE_ENRICHED,
          episode_id: null,
          fields: ['overview'],
        })
      })

      const stalker = within(rail).getByRole('button', { name: 'Stalker, 1979, partly watched' })
      expect(stalker).toHaveClass('u-card--patched')
      // Colour only: the order is untouched and the focus did not move.
      expect(
        within(rail)
          .getAllByRole('button')
          .map((card) => card.getAttribute('aria-label')),
      ).toEqual(before)
      expect(document.activeElement).toBe(solaris)
    })
  })
})
