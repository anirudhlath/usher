import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router-dom'
import { renderApp, screen, waitFor, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { installFakeEventSource } from '@/test/sse'
import { EPISODE_PILOT, EPISODE_SECOND, PLAYBACK_TICKET, TITLE_SERIES, TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { titleSkeleton } from '@/test/fixtures/titles'
import { deepLinkUrl, directTicketUrl, playNoTargets } from '@/test/fixtures/play'
import { notFound, notPlayable, problemHandler, problemResponse } from '@/test/fixtures/problems'
import { ROUTES, episodePath, seriesPath } from '@/app/routes'
import Episode from './Episode'

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

function renderEpisode(episodeId: string) {
  return renderApp(
    <Routes>
      <Route path={ROUTES.episode} element={<Episode />} />
    </Routes>,
    { route: episodePath(episodeId) },
  )
}

/**
 * Everything the ticket could be recognised by: the fernet string itself, both
 * arms of the response (the deep link carries the same ticket percent-encoded,
 * where a naive `.includes('/stream/')` check silently misses it), the stream
 * path, the deep-link scheme, and the origin Usher minted the URL against.
 */
const SECRETS = [
  PLAYBACK_TICKET,
  directTicketUrl,
  deepLinkUrl,
  '/stream/',
  'infuse://',
  new URL(directTicketUrl).origin,
]
const URL_ATTRIBUTES = ['href', 'src', 'title', 'aria-label', 'value', 'data-url', 'data-href']

/** patterns.md §13: the ticket is a secret, in the markup as well as on screen. */
function expectNoTicketAnywhere(container: HTMLElement) {
  for (const secret of SECRETS) {
    expect(container.innerHTML).not.toContain(secret)
    expect(document.body.innerHTML).not.toContain(secret)
  }
  for (const element of Array.from(container.querySelectorAll('*'))) {
    for (const attribute of URL_ATTRIBUTES) {
      const value = element.getAttribute(attribute)
      if (value === null) continue
      for (const secret of SECRETS) expect(value).not.toContain(secret)
    }
  }
}

describe('Episode', () => {
  describe('ready', () => {
    it('renders its own h1 and a breadcrumb resolved through the parent title', async () => {
      renderEpisode(EPISODE_PILOT)

      expect(await screen.findByRole('heading', { level: 1, name: 'Pilot' })).toBeInTheDocument()

      const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' })
      // The series' real name, climbed to through `title_id`, not an id.
      const link = within(crumbs).getByRole('link', { name: 'Twin Peaks' })
      expect(link).toHaveAttribute('href', seriesPath(TITLE_SERIES))
      expect(within(crumbs).getByText('S01E01')).toBeInTheDocument()

      expect(screen.getByText('The body of Laura Palmer is found wrapped in plastic.')).toBeInTheDocument()
      expect(screen.getByText('1990-04-08 · 94 min · absolute 1')).toBeInTheDocument()
    })

    it('passes an axe sweep', async () => {
      const { container } = renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })
      await expectNoViolations(container)
    })
  })

  describe('loading', () => {
    it('shows a shaped skeleton and never a route spinner', async () => {
      const { container } = renderEpisode(EPISODE_PILOT)

      expect(screen.getByText('Loading episode …')).toBeInTheDocument()
      expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
      const still = container.querySelector('.u-episode__still .u-skel')
      expect(still?.getAttribute('style')).toContain('aspect-ratio: 16 / 9')

      await screen.findByRole('heading', { level: 1, name: 'Pilot' })
    })

    it('passes an axe sweep while loading', async () => {
      const { container } = renderEpisode(EPISODE_PILOT)
      await expectNoViolations(container)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })
    })
  })

  describe('empty', () => {
    it('names the missing name and overview as the separate facts they are', async () => {
      renderEpisode(EPISODE_SECOND)

      await screen.findByRole('heading', { level: 1, name: 'S01E02' })
      expect(screen.getByText('— No name is on record for this episode.')).toBeInTheDocument()
      expect(screen.getByText('No overview has ever been written for this episode.')).toBeInTheDocument()
      expect(screen.getByText('overview: null')).toBeInTheDocument()
    })

    it('says so when a 200 carries no target at all', async () => {
      server.use(http.post('/episodes/:episode_id/play', () => HttpResponse.json(playNoTargets)))
      const { user } = renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      await user.click(screen.getByRole('button', { name: 'Play' }))

      expect(await screen.findByText('targets: []')).toBeInTheDocument()
      expect(screen.queryByRole('group', { name: 'Playback options' })).not.toBeInTheDocument()
    })
  })

  describe('error', () => {
    it('is page scale with back and search, and no retry', async () => {
      renderEpisode('0191f4c4-0000-7000-8000-000000000000')

      const heading = await screen.findByRole('heading', { level: 1 })
      expect(heading).toHaveTextContent("We couldn't find that.")
      expect(screen.getByText('code not_found')).toBeInTheDocument()
      expect(screen.getByText('HTTP 404')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Back to home' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    })

    it('offers "See other copies" and no retry when the episode is not playable', async () => {
      server.use(
        http.post('/episodes/:episode_id/play', () =>
          problemResponse(notPlayable(`/episodes/${EPISODE_PILOT}/play`)),
        ),
      )
      const { user } = renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      await user.click(screen.getByRole('button', { name: 'Play' }))

      expect(await screen.findByText('code not_playable')).toBeInTheDocument()
      expect(screen.getByText('HTTP 409')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'See other copies' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    })

    it('prints the parent id when the climb to the title fails', async () => {
      server.use(http.get('/titles/:title_id', () => problemResponse(notPlayable(`/titles/${TITLE_SERIES}`))))
      renderEpisode(EPISODE_PILOT)

      await screen.findByRole('heading', { level: 1, name: 'Pilot' })
      expect(await screen.findByText(`title_id ${TITLE_SERIES}`)).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'Twin Peaks' })).not.toBeInTheDocument()
    })

    it('passes an axe sweep', async () => {
      const { container } = renderEpisode('0191f4c4-0000-7000-8000-000000000000')
      await screen.findByRole('heading', { level: 1 })
      await expectNoViolations(container)
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    const MISSING = '0191f4c4-0000-7000-8000-000000000000'

    function render(episodeId: string, tempoUrl: string | null) {
      return renderApp(
        withTempo(
          <Routes>
            <Route path={ROUTES.episode} element={<Episode />} />
          </Routes>,
          tempoUrl,
        ),
        { route: episodePath(episodeId) },
      )
    }

    function failEpisode(traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/episodes/:episode_id',
          notFound(`/episodes/${MISSING}`),
          traceId === undefined ? {} : { traceId },
        ),
      )
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      failEpisode(TRACE_ID)
      const { container } = render(MISSING, TEMPO_URL)
      await screen.findByText('code not_found')

      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      failEpisode(TRACE_ID)
      const { container } = render(MISSING, null)
      await screen.findByText('code not_found')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      failEpisode()
      const { container } = render(MISSING, TEMPO_URL)
      await screen.findByText('code not_found')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('carries the link on the play panel too, which is a second call site', async () => {
      server.use(
        problemHandler('post', '/episodes/:episode_id/play', notPlayable(`/episodes/${EPISODE_PILOT}/play`), {
          traceId: TRACE_ID,
        }),
      )
      const { container, user } = render(EPISODE_PILOT, TEMPO_URL)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      await user.click(screen.getByRole('button', { name: 'Play' }))

      await screen.findByText('code not_playable')
      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })
  })

  describe('skeleton tier', () => {
    it('climbs to a skeleton parent without treating it as a failure', async () => {
      server.use(http.get('/titles/:title_id', () => HttpResponse.json(titleSkeleton)))
      const { container } = renderEpisode(EPISODE_PILOT)

      await screen.findByRole('heading', { level: 1, name: 'Pilot' })
      expect(await screen.findByRole('link', { name: 'Solaris' })).toBeInTheDocument()
      // The parent being sparse changes nothing about this episode's own record.
      expect(screen.getByText('The body of Laura Palmer is found wrapped in plastic.')).toBeInTheDocument()
      await expectNoViolations(container)
    })
  })

  describe('watch state', () => {
    it('says the route reports none, rather than drawing a reading it does not have', async () => {
      renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
      expect(screen.getByText(/Episode progress is written, not read/)).toBeInTheDocument()
      expect(screen.getByText('GET /episodes/{episode_id} · no watch_state field')).toBeInTheDocument()
    })

    it('shows what this client wrote, and says that is what it is', async () => {
      const { user } = renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      await user.click(screen.getByRole('button', { name: 'Mark watched' }))

      expect(
        await screen.findByText(/This is what this client wrote at .+, not a reading/),
      ).toBeInTheDocument()
      expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', 'Watched')
      expect(screen.getByText('watched')).toBeInTheDocument()
    })

    it('attributes a live frame to the device that sent it', async () => {
      const stream = useFakeEventSource()
      renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      stream.latest().open()
      stream.latest().emit('watchstate.updated', {
        title_id: TITLE_SERIES,
        episode_id: EPISODE_PILOT,
        position_seconds: 2_820,
        played: false,
        observed_at: '2026-08-18T21:14:02Z',
      })

      expect(
        await screen.findByText(/Another device reported this at .+ over the live channel/),
      ).toBeInTheDocument()
      expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '47 of 94 min watched')
    })

    it('ignores a frame for a different episode', async () => {
      const stream = useFakeEventSource()
      renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      stream.latest().open()
      stream.latest().emit('watchstate.updated', {
        title_id: TITLE_SERIES,
        episode_id: EPISODE_SECOND,
        position_seconds: 640,
        played: false,
        observed_at: '2026-08-18T21:14:02Z',
      })

      await waitFor(() =>
        expect(screen.getByText(/Episode progress is written, not read/)).toBeInTheDocument(),
      )
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    })
  })

  describe('playback', () => {
    it('needs a real pending state before the picker appears', async () => {
      const { user } = renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      expect(screen.queryByRole('group', { name: 'Playback options' })).not.toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Play' }))

      const picker = await screen.findByRole('group', { name: 'Playback options' })
      expect(within(picker).getAllByRole('button')).toHaveLength(1)
      expect(screen.getByText('1 copy across 1 source')).toBeInTheDocument()
    })

    it('never puts a ticket URL in the DOM after a play action', async () => {
      const { container, user } = renderEpisode(EPISODE_PILOT)
      await screen.findByRole('heading', { level: 1, name: 'Pilot' })

      await user.click(screen.getByRole('button', { name: 'Play' }))
      const picker = await screen.findByRole('group', { name: 'Playback options' })

      expectNoTicketAnywhere(container)
      // The copy is still described — what is withheld is the ticket, not the
      // information a person needs to choose.
      expect(within(picker).getByText('1920x1080')).toBeInTheDocument()
      expect(container.innerHTML).not.toContain('x-callback-url')
    })
  })
})
