import { describe, expect, it } from 'vitest'
import { Route, Routes } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import type { PlayResponse } from '@/api'
import { streamPath } from '@/api'
import { fireEvent, renderApp, screen, waitFor } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import {
  deepLinkUrl,
  directTicketUrl,
  playNoTargets,
  playTargets,
  problemHandler,
  problemResponse,
  sourceUnavailable,
  ticketInvalid,
} from '@/test/fixtures'
import { PLAYBACK_TICKET, TITLE_ENRICHED, TITLE_NOT_PLAYABLE, TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES, playerPath } from '@/app/routes'
import Player from './Player'

function renderPlayer(kind: 'title' | 'episode', id: string) {
  return renderApp(
    <Routes>
      <Route path={ROUTES.player} element={<Player />} />
    </Routes>,
    { route: playerPath(kind, id) },
  )
}

function video(container: HTMLElement): HTMLVideoElement {
  const element = container.querySelector('video')
  if (element === null) throw new Error('expected an inline player')
  return element
}

/**
 * **The ticket is a secret and this is the assertion that keeps it one.**
 *
 * Every attribute of every node is walked, not just the ones a reviewer would
 * think to look at, plus all text and every anchor. The single exception is the
 * media element's own `src` — the ticket has to reach a decoder somehow, and
 * a `<video>` has exactly one way in. That one is checked separately below and
 * held to a stricter rule than "does not leak": it must be the **same-origin
 * path** `/stream/{ticket}` and never `target.url`, which names a host Usher
 * read off a request header and is cross-origin besides.
 */
function expectNoTicketUrl(container: HTMLElement): void {
  const html = container.innerHTML

  // Neither arm of the play response, in either encoding. A `.includes('/stream/')`
  // check catches the direct one and silently misses the deep link, where the
  // separators arrive as `%2F`.
  expect(html).not.toContain(directTicketUrl)
  expect(html).not.toContain(deepLinkUrl)
  expect(html).not.toContain(encodeURIComponent(directTicketUrl))
  expect(html).not.toContain('192.168.50.158')

  // No anchors at all on this screen: an `href` puts a secret in the status bar
  // on hover, in the context menu, and in the middle-click buffer.
  expect(container.querySelectorAll('a')).toHaveLength(0)

  for (const element of container.querySelectorAll('*')) {
    for (const attribute of Array.from(element.attributes)) {
      if (element instanceof HTMLMediaElement && attribute.name === 'src') continue
      expect(`${attribute.name}="${attribute.value}"`).not.toContain(PLAYBACK_TICKET)
    }
  }

  // Nothing readable, and nothing announced.
  expect(container.textContent ?? '').not.toContain(PLAYBACK_TICKET)
  expect(container.textContent ?? '').not.toContain('/stream/')

  // And no affordance whose whole purpose would be to hand it over.
  expect(screen.queryByRole('button', { name: /copy|share|link address/i })).toBeNull()
}

describe('Player', () => {
  it('plays inline from a same-origin path and never emits a ticket URL', async () => {
    const { container } = renderPlayer('title', TITLE_ENRICHED)

    await screen.findByRole('button', { name: 'Play' })
    expectNoTicketUrl(container)

    // The one sanctioned carrier, and it is a path rather than a URL: no host,
    // no scheme, and not the absolute address the API handed back.
    const player = video(container)
    expect(player.getAttribute('src')).toBe(streamPath(PLAYBACK_TICKET))
    expect(player.getAttribute('src')).not.toBe(directTicketUrl)
    expect(player.getAttribute('src')?.startsWith('/stream/')).toBe(true)

    await expectNoViolations(container)
  })

  it('emits no ticket URL on the hand-off surface either', async () => {
    const handOffOnly: PlayResponse = {
      targets: playTargets.targets.filter((target) => target.kind === 'deep_link'),
    }
    server.use(http.post('/titles/:title_id/play', () => HttpResponse.json(handOffOnly)))

    const { container } = renderPlayer('title', TITLE_ENRICHED)

    expect(await screen.findByRole('heading', { level: 2, name: 'Handed off to infuse' })).toBeVisible()
    // The "open it again" control is a button, not a link: an anchor would need
    // the deep link as an `href`.
    expect(screen.getByRole('button', { name: 'Open infuse again' }).tagName).toBe('BUTTON')
    expectNoTicketUrl(container)
  })

  it('shows a skeleton while /play resolves, not a route spinner', () => {
    renderPlayer('title', TITLE_ENRICHED)
    expect(screen.getByText('Finding copies of this …')).toBeInTheDocument()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('gives every control a name, and Space is play / pause', async () => {
    const { container, user } = renderPlayer('title', TITLE_ENRICHED)

    const play = await screen.findByRole('button', { name: 'Play' })
    expect(screen.getByRole('button', { name: 'Back 10 seconds' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Forward 30 seconds' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeVisible()
    expect(screen.getByRole('slider', { name: 'Seek' })).toBeVisible()

    await user.click(play)
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeVisible()

    // Space, with focus nowhere in particular, toggles it back.
    video(container).blur()
    document.body.focus()
    await user.keyboard(' ')
    expect(await screen.findByRole('button', { name: 'Play' })).toBeVisible()
  })

  it('keeps a captions button even though no track exists, and says so when pressed', async () => {
    const { user } = renderPlayer('title', TITLE_ENRICHED)

    const captions = await screen.findByRole('button', { name: 'Subtitles' })
    expect(captions).toHaveAttribute('aria-expanded', 'false')

    const explanation = screen.getByText(/No subtitle track was supplied with this copy\./)
    expect(explanation).not.toBeVisible()

    await user.click(captions)
    expect(captions).toHaveAttribute('aria-expanded', 'true')
    expect(explanation).toBeVisible()
    // The absence is attributed to the API rather than to the copy or to us.
    expect(explanation).toHaveTextContent('carries no subtitle streams at all')
  })

  it('recovers an expired ticket in one tap', async () => {
    // The probe reads this because an expired ticket is refused *before* the
    // redirect, same-origin, where a status is still readable.
    server.use(http.get('/stream/:ticket', () => problemResponse(ticketInvalid('/stream/{ticket}'))))

    const { container, user } = renderPlayer('title', TITLE_ENRICHED)
    await screen.findByRole('button', { name: 'Play' })

    // A media `error` is a browser-originated event with no user-event
    // equivalent, so it is dispatched rather than driven.
    fireEvent.error(video(container))

    expect(await screen.findByRole('heading', { level: 2, name: 'That link expired' })).toBeVisible()
    expect(screen.getByText('code ticket_invalid · HTTP 404 · one tap re-requests')).toBeVisible()

    // One tap. It resumes from where the response said we were: 3142 s.
    const again = screen.getByRole('button', { name: 'Play again from 52:22' })
    await user.click(again)

    await waitFor(() => {
      expect(container.querySelector('video')).not.toBeNull()
    })
    expect(screen.queryByText('That link expired')).toBeNull()
    expectNoTicketUrl(container)
  })

  it('tells a decode refusal apart from a playback failure', async () => {
    // The bytes flow: 206 from the same-origin ticket route. So the network is
    // not what failed, and saying "playback is broken" here would be wrong.
    server.use(http.get('/stream/:ticket', () => new HttpResponse(new Uint8Array([0, 1]), { status: 206 })))

    const { container } = renderPlayer('title', TITLE_ENRICHED)
    await screen.findByRole('button', { name: 'Play' })

    fireEvent.error(video(container))

    expect(
      await screen.findByRole('heading', { level: 2, name: "Your browser can't decode this copy" }),
    ).toBeVisible()
    expect(screen.getByText(/The network is fine and the file is intact/)).toBeVisible()
    expect(screen.getByText(/has no hevc decoder for mkv/)).toBeVisible()
    expect(screen.getByText(/the decoder refused before the first frame/)).toBeVisible()

    // It is not offered as the default copy again.
    expect(screen.getByRole('button', { name: /your browser can't decode this/i })).toBeVisible()
    expect(screen.queryByText('That link expired')).toBeNull()

    await expectNoViolations(container)
  })

  it('renders 409 not_playable at panel scale, with no retry and other copies offered', async () => {
    const { container } = renderPlayer('title', TITLE_NOT_PLAYABLE)

    expect(await screen.findByText('code not_playable')).toBeVisible()
    expect(screen.getByText('HTTP 409')).toBeVisible()
    expect(screen.getByText('No available copy of this title on any configured source.')).toBeVisible()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
    expect(screen.getByRole('button', { name: 'See other copies' })).toBeVisible()
    expect(screen.getByText('409 gets no retry button. Retrying cannot conjure a file.')).toBeVisible()

    await expectNoViolations(container)
  })

  it('says a 200 with no targets succeeded and had nothing on the other end', async () => {
    server.use(http.post('/titles/:title_id/play', () => HttpResponse.json(playNoTargets)))
    const { container } = renderPlayer('title', TITLE_ENRICHED)

    expect(await screen.findByText('No copy came back')).toBeVisible()
    expect(screen.getByText('targets: []')).toBeVisible()
    expect(container.querySelector('video')).toBeNull()

    await expectNoViolations(container)
  })

  describe('the trace link (patterns.md §3)', () => {
    /**
     * Player reaches `Problem` through `ScreenProblem`, so this proves the
     * wrapper's wiring on a real screen; `NotFound.test.tsx` carries the full
     * matrix for the wrapper itself.
     */
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'post',
          '/titles/:title_id/play',
          sourceUnavailable(`/titles/${TITLE_ENRICHED}/play`),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(
        withTempo(
          <Routes>
            <Route path={ROUTES.player} element={<Player />} />
          </Routes>,
          tempoUrl,
        ),
        { route: playerPath('title', TITLE_ENRICHED) },
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
