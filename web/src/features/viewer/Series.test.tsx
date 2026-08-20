import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router-dom'
import { renderApp, screen, waitFor, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { installFakeEventSource } from '@/test/sse'
import { EPISODE_PILOT, TITLE_MISSING, TITLE_SERIES, TRACE_ID } from '@/test/fixtures/ids'
import { notFound, problemHandler, sourceUnavailable } from '@/test/fixtures'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { seasonEpisodesPageTwo, titleSeries } from '@/test/fixtures/titles'
import { seasonsWithSpecials } from '@/test/fixtures/viewer-screens'
import type { SeasonsResponse } from '@/api'
import { ROUTES, seriesPath } from '@/app/routes'
import Series from './Series'

/**
 * Swaps `EventSource` for the controllable fake and puts the previous value
 * back afterwards. Deliberately **not** `vi.unstubAllGlobals()`: jsdom ships no
 * `EventSource` at all, so unstubbing would remove `setup.ts`'s inert one too
 * and every later test in this file would crash on `new EventSource`.
 */
let restoreEventSource: (() => void) | null = null

afterEach(() => {
  restoreEventSource?.()
  restoreEventSource = null
})

function useFakeEventSource(): ReturnType<typeof installFakeEventSource> {
  const previous = globalThis.EventSource
  const stream = installFakeEventSource()
  restoreEventSource = () => vi.stubGlobal('EventSource', previous)
  return stream
}

function renderSeries(titleId: string) {
  return renderApp(
    <Routes>
      <Route path={ROUTES.series} element={<Series />} />
    </Routes>,
    { route: seriesPath(titleId) },
  )
}

function serveSeasons(body: SeasonsResponse) {
  server.use(http.get('/series/:title_id/seasons', () => HttpResponse.json(body)))
}

describe('Series', () => {
  describe('ready', () => {
    it('renders its own h1, the season switcher and the episode list', async () => {
      renderSeries(TITLE_SERIES)

      expect(await screen.findByRole('heading', { level: 1, name: 'Twin Peaks' })).toBeInTheDocument()

      const tabs = await screen.findByRole('tablist')
      expect(within(tabs).getByRole('tab', { name: /Season 1/ })).toHaveAttribute('aria-selected', 'true')
      expect(within(tabs).getByRole('tab', { name: /Season 2/ })).toBeInTheDocument()

      expect(await screen.findByRole('button', { name: 'Pilot' })).toBeInTheDocument()
      expect(screen.getByText('1990-04-08 · 94 min')).toBeInTheDocument()
      // The second episode has no name of its own; that is a real stored row.
      expect(screen.getByRole('button', { name: 'No name on record' })).toBeInTheDocument()
    })

    it('passes an axe sweep', async () => {
      const { container } = renderSeries(TITLE_SERIES)
      await screen.findByRole('button', { name: 'Pilot' })
      await expectNoViolations(container)
    })
  })

  describe('the season switcher', () => {
    it('includes Specials, and does not select it by default', async () => {
      serveSeasons(seasonsWithSpecials)
      renderSeries(TITLE_SERIES)

      const tabs = await screen.findByRole('tablist')
      const specials = within(tabs).getByRole('tab', { name: /Specials/ })
      expect(specials).toBeInTheDocument()
      expect(specials).toHaveAttribute('aria-selected', 'false')
      expect(within(tabs).getByRole('tab', { name: /Season 1/ })).toHaveAttribute('aria-selected', 'true')
    })

    it('prints the provider count on the tab it belongs to', async () => {
      serveSeasons(seasonsWithSpecials)
      renderSeries(TITLE_SERIES)

      const tabs = await screen.findByRole('tablist')
      expect(within(tabs).getByRole('tab', { name: /Specials/ })).toHaveTextContent('6')
      expect(within(tabs).getByRole('tab', { name: /Season 1/ })).toHaveTextContent('8')
      // Season 2's count is null and no number is invented for it.
      const seasonTwo = within(tabs).getByRole('tab', { name: 'Season 2' })
      expect(seasonTwo.querySelector('.u-tab__count')).toBeNull()
    })

    it('switches the episode list when a season is chosen', async () => {
      serveSeasons(seasonsWithSpecials)
      const { user } = renderSeries(TITLE_SERIES)

      await screen.findByRole('button', { name: 'Pilot' })
      await user.click(screen.getByRole('tab', { name: /Specials/ }))

      await waitFor(() => expect(screen.queryByRole('button', { name: 'Pilot' })).not.toBeInTheDocument())
    })
  })

  describe('loading', () => {
    it('is a four-row episode skeleton with a 16:9 block in the leading cell', async () => {
      const { container } = renderSeries(TITLE_SERIES)

      expect(screen.getByText('Loading series …')).toBeInTheDocument()
      expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()

      const rows = container.querySelectorAll('.u-series__skeletonrow')
      expect(rows).toHaveLength(4)
      for (const row of Array.from(rows)) {
        const still = row.querySelector('.u-series__skeletonstill .u-skel')
        expect(still).not.toBeNull()
        expect(still?.getAttribute('style')).toContain('aspect-ratio: 16 / 9')
      }

      await screen.findByRole('heading', { level: 1, name: 'Twin Peaks' })
    })

    it('passes an axe sweep while loading', async () => {
      const { container } = renderSeries(TITLE_SERIES)
      await expectNoViolations(container)
      await screen.findByRole('heading', { level: 1, name: 'Twin Peaks' })
    })
  })

  describe('empty', () => {
    it('names both numbers when a season returns no episodes', async () => {
      serveSeasons(seasonsWithSpecials)
      const { user } = renderSeries(TITLE_SERIES)

      await screen.findByRole('button', { name: 'Pilot' })
      await user.click(screen.getByRole('tab', { name: /Specials/ }))

      expect(
        await screen.findByText(
          "The provider reports 6 episodes for Specials, and the episode list came back empty. Both numbers are true: the count is the provider's, the list is what we hold.",
        ),
      ).toBeInTheDocument()
      expect(screen.getByText('items: [] · next_cursor: null')).toBeInTheDocument()
    })

    it('says a season with no count has nothing to compare against', async () => {
      const { user } = renderSeries(TITLE_SERIES)
      await screen.findByRole('button', { name: 'Pilot' })

      await user.click(screen.getByRole('tab', { name: /Season 2/ }))

      expect(
        await screen.findByText(
          /The episode list for Season 2 came back empty, and the provider gave no count/,
        ),
      ).toBeInTheDocument()
    })

    it('uses the never-computed treatment for a null episode_count', async () => {
      // A season with episodes but no provider count: there is no denominator,
      // and the screen says which field proves it rather than printing a dash.
      server.use(http.get('/seasons/:season_id/episodes', () => HttpResponse.json(seasonEpisodesPageTwo)))
      const { user } = renderSeries(TITLE_SERIES)
      await screen.findByRole('tablist')

      await user.click(screen.getByRole('tab', { name: /Season 2/ }))

      expect(await screen.findByText('episode_count: null')).toBeInTheDocument()
      expect(
        screen.getByText(/The provider never supplied an episode count for this season/),
      ).toBeInTheDocument()
    })
  })

  describe('error', () => {
    it('is page scale with back and search, and no retry', async () => {
      renderSeries(TITLE_MISSING)

      const heading = await screen.findByRole('heading', { level: 1 })
      expect(heading).toHaveTextContent("We couldn't find that.")
      expect(screen.getByText('code not_found')).toBeInTheDocument()
      expect(screen.getByText('HTTP 404')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Back to home' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    })

    it('passes an axe sweep', async () => {
      const { container } = renderSeries(TITLE_MISSING)
      await screen.findByRole('heading', { level: 1 })
      await expectNoViolations(container)
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    function render(titleId: string, tempoUrl: string | null) {
      return renderApp(
        withTempo(
          <Routes>
            <Route path={ROUTES.series} element={<Series />} />
          </Routes>,
          tempoUrl,
        ),
        { route: seriesPath(titleId) },
      )
    }

    function failTitle(traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/titles/:title_id',
          notFound(`/titles/${TITLE_MISSING}`),
          traceId === undefined ? {} : { traceId },
        ),
      )
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      failTitle(TRACE_ID)
      const { container } = render(TITLE_MISSING, TEMPO_URL)
      await screen.findByText('code not_found')

      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      failTitle(TRACE_ID)
      const { container } = render(TITLE_MISSING, null)
      await screen.findByText('code not_found')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      failTitle()
      const { container } = render(TITLE_MISSING, TEMPO_URL)
      await screen.findByText('code not_found')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('carries the link on the season panel, which is its own component and its own hook call', async () => {
      server.use(
        problemHandler('get', '/seasons/:season_id/episodes', sourceUnavailable('/seasons/…/episodes'), {
          traceId: TRACE_ID,
        }),
      )
      const { container } = render(TITLE_SERIES, TEMPO_URL)

      await screen.findByText('code source_unavailable')
      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })
  })

  describe('skeleton tier', () => {
    it('renders a sparse series without artwork or seasons as a fact, not a failure', async () => {
      server.use(
        http.get('/titles/:title_id', () =>
          HttpResponse.json({
            id: TITLE_SERIES,
            kind: 'series',
            name: 'Roadside Picnic',
            year: 2027,
            overview: null,
            tagline: null,
            runtime_minutes: null,
            genres: [],
            community_rating: null,
            enrichment_state: 'skeleton',
            enrichment_error: null,
            availability: [],
            watch_state: null,
          }),
        ),
      )
      serveSeasons({ seasons: [] })
      const { container } = renderSeries(TITLE_SERIES)

      expect(await screen.findByRole('heading', { level: 1, name: 'Roadside Picnic' })).toBeInTheDocument()
      expect(await screen.findByText('seasons: []')).toBeInTheDocument()
      expect(screen.getByText('No artwork on record')).toBeInTheDocument()
      expect(screen.queryByRole('tablist')).not.toBeInTheDocument()

      await expectNoViolations(container)
    })
  })

  describe('the episode_count disagreement', () => {
    it('counts loaded episodes while the walk is open, and claims no total', async () => {
      renderSeries(TITLE_SERIES)
      await screen.findByRole('button', { name: 'Pilot' })

      expect(screen.getByText('provider says 8 · 2 loaded so far')).toBeInTheDocument()
      expect(screen.queryByText(/we hold/)).not.toBeInTheDocument()
      expect(screen.getByText('2 loaded so far')).toBeInTheDocument()
    })

    it('states the disagreement plainly once the walk has ended', async () => {
      const { user } = renderSeries(TITLE_SERIES)
      await screen.findByRole('button', { name: 'Pilot' })

      await user.click(screen.getByRole('button', { name: 'Load more' }))

      expect(await screen.findByText('provider says 8 · we hold 3')).toBeInTheDocument()
      expect(
        screen.getByText("Both numbers are true: the count is the provider's, the list is what we hold."),
      ).toBeInTheDocument()
      // And a keyset list that has ended owes the reader a sentence.
      expect(screen.getByText('That is every episode we hold for Season 1.')).toBeInTheDocument()
      // Never a page number, a total or a result count.
      expect(screen.queryByText(/of 8/)).not.toBeInTheDocument()
    })
  })

  describe('per-episode progress', () => {
    it('shows nothing but the reason when nothing has been written', async () => {
      renderSeries(TITLE_SERIES)
      await screen.findByRole('button', { name: 'Pilot' })

      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
      expect(screen.getByText(/Episode progress is written, not read/)).toBeInTheDocument()
    })

    it('records what this client wrote and says it was this client', async () => {
      const { user } = renderSeries(TITLE_SERIES)
      await screen.findByRole('button', { name: 'Pilot' })

      const rows = screen.getAllByRole('listitem')
      const pilot = rows[0]
      expect(pilot).toBeDefined()
      await user.click(within(pilot as HTMLElement).getByRole('button', { name: 'Mark watched' }))

      expect(await screen.findByText(/progress from this client/)).toBeInTheDocument()
      expect(within(pilot as HTMLElement).getByText('watched')).toBeInTheDocument()
    })

    it('accepts another device over the live channel, and is correct without one', async () => {
      const stream = useFakeEventSource()
      renderSeries(TITLE_SERIES)
      await screen.findByRole('button', { name: 'Pilot' })
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()

      stream.latest().open()
      stream.latest().emit('watchstate.updated', {
        title_id: TITLE_SERIES,
        episode_id: EPISODE_PILOT,
        position_seconds: 1_200,
        played: false,
        observed_at: '2026-08-18T21:14:02Z',
      })

      expect(await screen.findByText(/progress from another device/)).toBeInTheDocument()
      expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '20 of 94 min watched')
      expect(titleSeries.watch_state).toBeNull()
    })
  })
})
