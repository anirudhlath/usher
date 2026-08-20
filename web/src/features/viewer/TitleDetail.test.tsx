import { afterEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router-dom'
import { renderApp, screen, waitFor, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { installFakeEventSource } from '@/test/sse'
import {
  TITLE_ENRICHED,
  TITLE_MISSING,
  TITLE_NOT_PLAYABLE,
  TITLE_SIMILAR_EMPTY,
  TITLE_SIMILAR_NEVER,
  TITLE_SIMILAR_STALE,
  TITLE_SKELETON,
} from '@/test/fixtures/ids'
import { titleCreditsEmpty, titleSkeletonEnriched } from '@/test/fixtures/viewer-screens'
import type { TitleResponse } from '@/api'
import { titleEnriched, titleSkeleton } from '@/test/fixtures/titles'
import { PLAYBACK_TICKET, TRACE_ID } from '@/test/fixtures/ids'
import { notFound, notPlayable, problemHandler } from '@/test/fixtures'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { deepLinkUrl, directTicketUrl, playNoTargets } from '@/test/fixtures/play'
import { ROUTES, titlePath } from '@/app/routes'
import TitleDetail from './TitleDetail'

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

function renderTitle(titleId: string) {
  return renderApp(
    <Routes>
      <Route path={ROUTES.title} element={<TitleDetail />} />
    </Routes>,
    { route: titlePath(titleId) },
  )
}

/** Serves one title document whatever id is asked for. */
function serveTitle(body: TitleResponse) {
  server.use(http.get('/titles/:title_id', () => HttpResponse.json(body)))
}

/**
 * patterns.md §13. A ticket URL is a secret: it is not rendered, not copied,
 * not shared and not logged. This asserts against the rendered markup **and**
 * every attribute a URL could hide in, because "not visible" and "not in the
 * DOM" are different claims and only the second one is the rule.
 */
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

describe('TitleDetail', () => {
  describe('ready', () => {
    it('renders its own h1, the composed per-copy specs and the credits', async () => {
      renderTitle(TITLE_ENRICHED)

      expect(await screen.findByRole('heading', { level: 1, name: 'Stalker' })).toBeInTheDocument()

      /* There is no `quality` field on the wire. The line is composed from
         `resolution` and `hdr_format` (plus codec and container), each printed
         exactly as the API sent it. */
      expect(screen.getByText('3840x2160 · HDR10 · hevc · mkv')).toBeInTheDocument()
      expect(screen.getByText('Living Room Emby')).toBeInTheDocument()

      expect(screen.getByRole('button', { name: 'Alexander Kaidanovsky, Stalker' })).toBeInTheDocument()
      // A cast entry with no character is a real stored row, not a hole.
      expect(screen.getByRole('button', { name: 'Nikolai Grinko' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Andrei Tarkovsky, Director' })).toBeInTheDocument()
      expect(screen.getAllByText('No photographs exist for people anywhere in this API.')).toHaveLength(2)

      expect(screen.getByText('Images · 3 on record')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^Resume at/ })).toBeInTheDocument()
    })

    it('passes an axe sweep', async () => {
      const { container } = renderTitle(TITLE_ENRICHED)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })
      await expectNoViolations(container)
    })
  })

  describe('loading', () => {
    it('shows a backdrop block and the hero skeleton, never a route spinner', async () => {
      const { container } = renderTitle(TITLE_ENRICHED)

      const region = container.querySelector('[aria-busy="true"]')
      expect(region).not.toBeNull()
      expect(screen.getByText('Loading title …')).toBeInTheDocument()
      expect(container.querySelector('.u-skel-hero')).not.toBeNull()
      // Every skeleton is aria-hidden; the region owns the announcement.
      for (const skeleton of Array.from(container.querySelectorAll('.u-skel-hero'))) {
        expect(skeleton).toHaveAttribute('aria-hidden', 'true')
      }

      await screen.findByRole('heading', { level: 1, name: 'Stalker' })
    })

    it('passes an axe sweep while loading', async () => {
      const { container } = renderTitle(TITLE_ENRICHED)
      await expectNoViolations(container)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })
    })
  })

  describe('empty', () => {
    it('renders the computed-and-empty treatment for cast, crew and images', async () => {
      serveTitle(titleCreditsEmpty)
      renderTitle(TITLE_ENRICHED)

      await screen.findByRole('heading', { level: 1, name: 'Stalker' })

      expect(screen.getByText('Enrichment ran for this title and returned no cast.')).toBeInTheDocument()
      expect(screen.getByText('Enrichment ran for this title and returned no crew.')).toBeInTheDocument()
      expect(screen.getByText('Enrichment ran for this title and returned no artwork.')).toBeInTheDocument()

      // `meta` names the field that proves the claim, and is not droppable.
      expect(screen.getByText('cast: []')).toBeInTheDocument()
      expect(screen.getByText('crew: []')).toBeInTheDocument()
      expect(screen.getByText('images: []')).toBeInTheDocument()
    })

    it('says a computed similarity list found nothing, with its computed_at', async () => {
      renderTitle(TITLE_SIMILAR_EMPTY)
      await screen.findByRole('heading', { level: 1, name: 'Andrei Rublev' })

      expect(screen.getByText(/Nothing scored close enough to show\./)).toBeInTheDocument()
      expect(screen.getByText('neighbors: [] · computed_at: 2026-08-16T04:12:09Z')).toBeInTheDocument()
    })

    it('distinguishes a never-computed similarity list from an empty one', async () => {
      serveTitle({ ...titleEnriched, id: TITLE_SIMILAR_NEVER })
      renderTitle(TITLE_SIMILAR_NEVER)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })

      expect(screen.getByText('We have never computed similar titles for this one.')).toBeInTheDocument()
      expect(screen.getByText('computed_at: null')).toBeInTheDocument()
      expect(screen.queryByText(/Nothing scored close enough/)).not.toBeInTheDocument()
    })
  })

  describe('error', () => {
    it('is page scale with back and search, and no retry', async () => {
      renderTitle(TITLE_MISSING)

      const heading = await screen.findByRole('heading', { level: 1 })
      expect(heading).toHaveTextContent("We couldn't find that.")

      // `code` and `status` in mono: an operator pastes them into a log query.
      expect(screen.getByText('code not_found')).toBeInTheDocument()
      expect(screen.getByText('HTTP 404')).toBeInTheDocument()
      // `detail` verbatim, never parsed.
      expect(screen.getByText(`No title with id ${TITLE_MISSING}.`)).toBeInTheDocument()

      expect(screen.getByRole('button', { name: 'Back to home' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Search' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    })

    it('moves focus to the failed page heading', async () => {
      renderTitle(TITLE_MISSING)
      const heading = await screen.findByRole('heading', { level: 1 })
      await waitFor(() => expect(heading).toHaveFocus())
    })

    it('passes an axe sweep', async () => {
      const { container } = renderTitle(TITLE_MISSING)
      await screen.findByRole('heading', { level: 1 })
      await expectNoViolations(container)
    })
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/titles/:title_id',
          notFound(`/titles/${TITLE_MISSING}`),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(
        withTempo(
          <Routes>
            <Route path={ROUTES.title} element={<TitleDetail />} />
          </Routes>,
          tempoUrl,
        ),
        { route: titlePath(TITLE_MISSING) },
      )
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

    it('carries the link on the play panel too, which is a second call site', async () => {
      serveTitle({ ...titleEnriched, id: TITLE_NOT_PLAYABLE })
      server.use(problemHandler('post', '/titles/:title_id/play', notPlayable(), { traceId: TRACE_ID }))
      const { container, user } = renderApp(
        withTempo(
          <Routes>
            <Route path={ROUTES.title} element={<TitleDetail />} />
          </Routes>,
          TEMPO_URL,
        ),
        { route: titlePath(TITLE_NOT_PLAYABLE) },
      )
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })
      await user.click(screen.getByRole('button', { name: /^Resume at/ }))

      await screen.findByText('code not_playable')
      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
      // The trace link is not a retry: `not_playable` still offers neither.
      expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
    })
  })

  describe('skeleton tier', () => {
    it('is a legitimate sparse record, not a broken one', async () => {
      renderTitle(TITLE_SKELETON)

      expect(await screen.findByRole('heading', { level: 1, name: 'Solaris' })).toBeInTheDocument()
      expect(screen.getByText('skeleton')).toBeInTheDocument()

      // The overview has never been computed for this row.
      expect(
        screen.getByText(/This title has never been enriched, so we have a name and a year/),
      ).toBeInTheDocument()
      expect(screen.getByText('overview: null · enrichment_state: skeleton')).toBeInTheDocument()

      // No copy anywhere, said as a fact about the record rather than an error.
      expect(screen.getByText(/No copy of this title exists on any source\./)).toBeInTheDocument()

      // And the notice that this is a live fetch, not a dead end.
      expect(screen.getByText(/Opening this title asked the server for full metadata\./)).toBeInTheDocument()
    })

    it('renders the not-applicable treatment for the three absent keys', async () => {
      const { container } = renderTitle(TITLE_SKELETON)
      await screen.findByRole('heading', { level: 1, name: 'Solaris' })

      for (const clause of [
        'Credits are not on this record; it has never been enriched.',
        'Crew credits are not on this record; it has never been enriched.',
        'No images are on this record; artwork arrives with enrichment.',
      ]) {
        // `na` is an em dash and one clause, with no border and no heading.
        const block = screen.getByText(`— ${clause}`)
        expect(block.closest('.u-state--na')).not.toBeNull()
        expect(block.closest('.u-state--empty')).toBeNull()
      }

      // …and none of them is the computed-and-empty treatment.
      expect(container.querySelector('.u-state--empty')).toBeNull()
      expect(screen.queryByText('cast: []')).not.toBeInTheDocument()
    })

    /**
     * The distinction this screen exists to keep. Absent is "not applicable to
     * this record"; `[]` is "we looked and there is nothing". Same field, two
     * facts, two treatments — asserted against each other so a change that
     * collapses them fails here.
     */
    it('renders an absent cast differently from an empty one', async () => {
      const absent = renderTitle(TITLE_SKELETON)
      await screen.findByRole('heading', { level: 1, name: 'Solaris' })
      const absentBlocks = absent.container.querySelectorAll('.u-state--na').length
      const absentEmpty = absent.container.querySelectorAll('.u-state--empty').length
      absent.unmount()

      serveTitle(titleCreditsEmpty)
      const empty = renderTitle(TITLE_ENRICHED)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })

      expect(absentBlocks).toBeGreaterThan(0)
      expect(absentEmpty).toBe(0)
      expect(empty.container.querySelectorAll('.u-state--empty').length).toBeGreaterThan(0)
      expect(empty.container.querySelectorAll('.u-state--na')).toHaveLength(0)
      expect(empty.container.textContent?.includes('Credits are not on this record')).toBe(false)
    })

    it('passes an axe sweep', async () => {
      const { container } = renderTitle(TITLE_SKELETON)
      await screen.findByRole('heading', { level: 1, name: 'Solaris' })
      await expectNoViolations(container)
    })
  })

  describe('similar titles', () => {
    it('still shows the neighbours when the list is stale', async () => {
      serveTitle({ ...titleEnriched, id: TITLE_SIMILAR_STALE })
      renderTitle(TITLE_SIMILAR_STALE)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })

      const similar = await screen.findByRole('region', { name: 'Similar titles' })
      // Stale content is SHOWN. Suppressing it would be the bigger lie.
      expect(within(similar).getByRole('button', { name: /Solaris, 1972/ })).toBeInTheDocument()
      expect(within(similar).getByText('stale')).toBeInTheDocument()
      expect(
        within(similar).getByText(/before the scoring blend changed\. Shown as they were\./),
      ).toBeInTheDocument()
    })

    it('offers no similarity reason, because the API has none', async () => {
      renderTitle(TITLE_ENRICHED)
      const similar = await screen.findByRole('region', { name: 'Similar titles' })
      expect(within(similar).queryByText(/because/i)).not.toBeInTheDocument()
      expect(within(similar).queryByText(/reason/i)).not.toBeInTheDocument()
    })
  })

  describe('playback', () => {
    it('needs a real pending state before the picker appears', async () => {
      const { user } = renderTitle(TITLE_ENRICHED)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })

      expect(screen.queryByRole('group', { name: 'Playback options' })).not.toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: /^Resume at/ }))

      const picker = await screen.findByRole('group', { name: 'Playback options' })
      expect(within(picker).getAllByRole('button')).toHaveLength(2)
      expect(screen.getByText('2 copies across 1 source')).toBeInTheDocument()
    })

    it('never puts a ticket URL in the DOM after a play action', async () => {
      const { container, user } = renderTitle(TITLE_ENRICHED)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })

      await user.click(screen.getByRole('button', { name: /^Resume at/ }))
      await screen.findByRole('group', { name: 'Playback options' })

      expectNoTicketAnywhere(container)
      // Including the deep link, whose ticket arrives percent-encoded.
      expect(container.innerHTML).not.toContain('x-callback-url')
    })

    it('offers "See other copies" and no retry when the copy is not playable', async () => {
      serveTitle({ ...titleEnriched, id: TITLE_NOT_PLAYABLE })
      const { user } = renderTitle(TITLE_NOT_PLAYABLE)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })

      await user.click(screen.getByRole('button', { name: /^Resume at/ }))

      expect(await screen.findByText('code not_playable')).toBeInTheDocument()
      expect(screen.getByText('HTTP 409')).toBeInTheDocument()
      expect(
        screen.getByText('No available copy of this title on any configured source.'),
      ).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'See other copies' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    })

    it('says so when a 200 carries no target at all', async () => {
      server.use(http.post('/titles/:title_id/play', () => HttpResponse.json(playNoTargets)))
      const { user } = renderTitle(TITLE_ENRICHED)
      await screen.findByRole('heading', { level: 1, name: 'Stalker' })

      await user.click(screen.getByRole('button', { name: /^Resume at/ }))

      expect(await screen.findByText('targets: []')).toBeInTheDocument()
      expect(screen.queryByRole('group', { name: 'Playback options' })).not.toBeInTheDocument()
    })
  })

  describe('live', () => {
    it('is correct with zero frames', async () => {
      renderTitle(TITLE_SKELETON)
      await screen.findByRole('heading', { level: 1, name: 'Solaris' })
      expect(screen.getByText('skeleton')).toBeInTheDocument()
      expect(screen.getByText('overview: null · enrichment_state: skeleton')).toBeInTheDocument()
    })

    it('patches an open skeleton title in place when enrichment lands', async () => {
      const stream = useFakeEventSource()
      const { container } = renderTitle(TITLE_SKELETON)
      await screen.findByRole('heading', { level: 1, name: 'Solaris' })
      expect(screen.getByText('skeleton')).toBeInTheDocument()

      // The server now holds the enriched row; the frame is what says so.
      serveTitle(titleSkeletonEnriched)
      stream.latest().open()
      stream.latest().emit('title.updated', {
        title_id: TITLE_SKELETON,
        episode_id: null,
        fields: ['overview', 'cast', 'crew', 'images'],
      })

      await waitFor(() => expect(screen.getByText(titleSkeletonEnriched.overview ?? '')).toBeInTheDocument())
      // Patched in place: same heading, no navigation, no focus theft.
      expect(screen.getByRole('heading', { level: 1, name: 'Solaris' })).toBeInTheDocument()
      expect(document.activeElement).toBe(document.body)
      // Opacity only — the highlight is a class, and it carries no transform.
      expect(container.querySelector('.u-title--patched')).not.toBeNull()
    })

    it('subscribes to this title only', async () => {
      const stream = useFakeEventSource()
      renderTitle(TITLE_SKELETON)
      await screen.findByRole('heading', { level: 1, name: 'Solaris' })

      expect(stream.latest().url).toBe(`/events?titles=${TITLE_SKELETON}`)
    })

    it('ignores a frame for another title', async () => {
      const stream = useFakeEventSource()
      const { container } = renderTitle(TITLE_SKELETON)
      await screen.findByRole('heading', { level: 1, name: 'Solaris' })

      stream.latest().open()
      stream.latest().emit('title.updated', {
        title_id: TITLE_ENRICHED,
        episode_id: null,
        fields: ['overview'],
      })

      await waitFor(() => expect(screen.getByText('skeleton')).toBeInTheDocument())
      expect(container.querySelector('.u-title--patched')).toBeNull()
      expect(titleSkeleton.overview).toBeNull()
    })
  })
})
