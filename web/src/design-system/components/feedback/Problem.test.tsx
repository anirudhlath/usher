import { describe, expect, it, vi, afterEach } from 'vitest'
import { act, renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Problem, type ProblemDocument } from './index'

const SOURCE_DOWN: ProblemDocument = {
  code: 'source_unavailable',
  status: 503,
  title: "Couldn't reach your media server.",
  detail: 'Living Room did not answer within 5 s.',
  instance: '/admin/sources/0191f4c2/status',
  retry_after: 5,
}

afterEach(() => {
  vi.useRealTimers()
})

describe('Problem — contract', () => {
  it.each([
    ['inline', 'u-problem--inline'],
    ['panel', 'u-problem--panel'],
    ['page', 'u-problem--page'],
    ['toast', 'u-problem--toast'],
  ] as const)('renders the %s scale with the class the CSS expects', (scale, expected) => {
    const { container } = renderComponent(<Problem scale={scale} problem={SOURCE_DOWN} />)
    expect(container.querySelector(`.${expected}`)).not.toBeNull()
  })

  it('adds the warn modifier only at panel scale', () => {
    const { container } = renderComponent(<Problem scale="panel" tone="warn" problem={SOURCE_DOWN} />)
    expect(container.querySelector('.u-problem--panel-warn')).not.toBeNull()
  })

  it('defaults each code to the scale its recovery is designed at', () => {
    const { container: notFound } = renderComponent(<Problem problem={{ code: 'not_found', status: 404 }} />)
    expect(notFound.querySelector('.u-problem--page')).not.toBeNull()

    const { container: ticket } = renderComponent(
      <Problem problem={{ code: 'ticket_invalid', status: 404 }} />,
    )
    expect(ticket.querySelector('.u-problem--inline')).not.toBeNull()

    const { container: notAllowed } = renderComponent(
      <Problem problem={{ code: 'method_not_allowed', status: 405 }} />,
    )
    expect(notAllowed.querySelector('.u-problem--panel')).not.toBeNull()
  })

  it('prints code, status, instance and Retry-After in the mono meta row', () => {
    const { container } = renderComponent(<Problem problem={SOURCE_DOWN} />)
    const meta = container.querySelector('.u-problem__meta')
    expect(meta).not.toBeNull()
    expect(meta).toHaveTextContent('code source_unavailable')
    expect(meta).toHaveTextContent('HTTP 503')
    expect(meta).toHaveTextContent('/admin/sources/0191f4c2/status')
    expect(meta).toHaveTextContent('retry after 5s')
  })

  it('falls back to the code’s own sentence when the server sent no title', () => {
    renderComponent(<Problem problem={{ code: 'not_playable', status: 409 }} />)
    expect(screen.getByText("There's no playable file for this.")).toBeInTheDocument()
  })

  it('lists validation errors from errors[].loc and .msg', () => {
    renderComponent(
      <Problem
        scale="panel"
        problem={{
          code: 'validation_failed',
          status: 422,
          detail: 'year must be a four-digit integer.',
          errors: [{ loc: ['query', 'year'], msg: 'value is not a valid integer' }],
        }}
      />,
    )
    const item = screen.getByRole('listitem')
    expect(item).toHaveTextContent('query.year')
    expect(item).toHaveTextContent('value is not a valid integer')
  })
})

describe('Problem — the closed vocabulary drives recovery', () => {
  it('offers no retry for not_playable, even when onRetry is supplied', () => {
    const onRetry = vi.fn<() => void>()
    renderComponent(
      <Problem
        problem={{ code: 'not_playable', status: 409, detail: 'Every copy reported no media streams.' }}
        onRetry={onRetry}
        actions={
          <button type="button" className="u-btn u-btn--secondary u-btn--sm">
            See other copies
          </button>
        }
      />,
    )
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'See other copies' })).toBeInTheDocument()
  })

  it('offers no retry for not_found', () => {
    renderComponent(<Problem problem={{ code: 'not_found', status: 404 }} onRetry={vi.fn<() => void>()} />)
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
  })

  it('labels the ticket_invalid recovery "Play again"', () => {
    renderComponent(
      <Problem problem={{ code: 'ticket_invalid', status: 404 }} onRetry={vi.fn<() => void>()} />,
    )
    expect(screen.getByRole('button', { name: 'Play again' })).toBeInTheDocument()
  })

  it('renders nothing at all for invalid_cursor — the list restarts silently', () => {
    const { container } = renderComponent(
      <Problem problem={{ code: 'invalid_cursor', status: 400, detail: 'cursor is not decodable.' }} />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})

describe('Problem — retry behaviour', () => {
  it('honours Retry-After: the control is disabled and counts down before it can be pressed', () => {
    vi.useFakeTimers()
    renderComponent(<Problem problem={SOURCE_DOWN} onRetry={vi.fn<() => void>()} />)

    const button = screen.getByRole('button', { name: 'Try again in 5 s' })
    expect(button).toBeDisabled()

    // One second per act: each tick reschedules from an effect, which only runs when React flushes.
    for (let second = 0; second < 5; second += 1) {
      act(() => {
        vi.advanceTimersByTime(1000)
      })
    }

    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
  })

  it('disables itself while a retry is in flight', async () => {
    let release = (): void => {}
    const onRetry = vi.fn<() => Promise<void>>(
      () =>
        new Promise<void>((resolve) => {
          release = resolve
        }),
    )
    const { user } = renderComponent(
      <Problem
        problem={{ code: 'source_unavailable', status: 503, detail: 'No answer.' }}
        onRetry={onRetry}
      />,
    )

    const button = screen.getByRole('button', { name: 'Try again' })
    await user.click(button)
    expect(onRetry).toHaveBeenCalledTimes(1)
    expect(button).toBeDisabled()

    // A second press while the first is in flight must not reach the server.
    await user.click(button)
    expect(onRetry).toHaveBeenCalledTimes(1)

    await act(async () => {
      release()
    })
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
  })
})

describe('Problem — trace link', () => {
  it('renders "Open trace" as a link when the trace URL is known', async () => {
    const onOpenTrace = vi.fn<() => void>()
    const { user } = renderComponent(
      <Problem
        problem={SOURCE_DOWN}
        traceId="4f1c9e7a2b8d"
        traceHref="https://tempo.example/trace/4f1c9e7a2b8d"
        onOpenTrace={onOpenTrace}
      />,
    )
    const link = screen.getByRole('link', { name: /open trace/i })
    expect(link).toHaveAttribute('href', 'https://tempo.example/trace/4f1c9e7a2b8d')
    expect(link).toHaveAttribute('rel', 'noreferrer noopener')
    await user.click(link)
    expect(onOpenTrace).toHaveBeenCalledTimes(1)
  })

  it('renders "Open trace" as a button when only a callback is supplied', async () => {
    const onOpenTrace = vi.fn<() => void>()
    const { user } = renderComponent(
      <Problem problem={SOURCE_DOWN} traceId="4f1c9e7a2b8d" onOpenTrace={onOpenTrace} />,
    )
    await user.click(screen.getByRole('button', { name: /open trace/i }))
    expect(onOpenTrace).toHaveBeenCalledTimes(1)
  })

  it('shows no trace control when the response carried no trace id', () => {
    renderComponent(<Problem problem={SOURCE_DOWN} onOpenTrace={vi.fn<() => void>()} />)
    expect(screen.queryByText(/open trace/i)).not.toBeInTheDocument()
  })

  it('renders no anchor at all when there is a trace id and no Tempo configured', () => {
    // `useTraceUrl()` answers `null` on a deployment with no `tempoUrl`, and this is what the
    // component has to do with that: **absent, never dead**. An `<a href="">` navigates to the
    // current page, so it costs a click to discover it does nothing — the same reason
    // `console/config.json` makes `tempoUrl` nullable rather than defaulting it to something.
    //
    // Asserted as "no anchor anywhere in the tree" rather than as "no link named Open trace",
    // because the second passes on an anchor whose accessible name went missing for an unrelated
    // reason, which is a different bug wearing this test's green.
    const { container } = renderComponent(
      <Problem problem={SOURCE_DOWN} traceId="0af7651916cd43dd8448eb211c80319c" traceHref={null} />,
    )
    expect(container.querySelectorAll('a')).toHaveLength(0)
    expect(screen.queryByText(/open trace/i)).not.toBeInTheDocument()
  })

  it('renders the link when a trace id meets a configured Tempo', () => {
    // The positive control for the case above: same component, same trace id, the one input that
    // differs is whether `useTraceUrl()` had a `tempoUrl` to build a URL from. Without it, "no
    // anchor" would also be what a component that never renders one produces.
    const traceId = '0af7651916cd43dd8448eb211c80319c'
    renderComponent(
      <Problem
        problem={SOURCE_DOWN}
        traceId={traceId}
        traceHref={`https://tempo.lan/explore?traceId=${traceId}`}
      />,
    )
    const link = screen.getByRole('link', { name: /open trace/i })
    expect(link).toHaveAttribute('href', `https://tempo.lan/explore?traceId=${traceId}`)
    // The abbreviation the component shows is a prefix of the real id, never a re-derived one:
    // an operator who cannot paste this into Tempo has a formatter, not a link.
    expect(link).toHaveTextContent(traceId.slice(0, 8))
  })

  it.each([['validation_failed', 422] as const, ['ticket_invalid', 404] as const])(
    'renders the link at inline scale too, for %s',
    (code, status) => {
      // **The scale the link used to be missing from, and the two codes it is the
      // whole treatment for.** `showTrace` and the anchor were declared *below*
      // the `inline` early return, so a 422 naming a field and a 404 on an expired
      // ticket rendered no link at all — silently dropping patterns.md §3's MUST
      // for exactly the two failures an operator is most likely to be chasing.
      //
      // Parametrised over both because the treatment table is what routes a code
      // to `inline`: a change there that moved only one of them would otherwise
      // leave this passing on the other.
      const traceId = '0af7651916cd43dd8448eb211c80319c'
      renderComponent(
        <Problem
          problem={{ ...SOURCE_DOWN, code, status, title: 'Refused' }}
          traceId={traceId}
          traceHref={`https://tempo.lan/explore?traceId=${traceId}`}
        />,
      )
      expect(screen.getByRole('link', { name: /open trace/i })).toHaveAttribute(
        'href',
        `https://tempo.lan/explore?traceId=${traceId}`,
      )
    },
  )

  it('still renders no anchor at inline scale when Tempo is unconfigured', () => {
    // The control for the case above. Without it, "the inline scale renders a
    // link" would be satisfied by one that ignores `traceHref` entirely.
    const { container } = renderComponent(
      <Problem
        problem={{ ...SOURCE_DOWN, code: 'validation_failed', status: 422, title: 'Refused' }}
        traceId="0af7651916cd43dd8448eb211c80319c"
        traceHref={null}
      />,
    )
    expect(container.querySelectorAll('a')).toHaveLength(0)
  })
})

describe('Problem — accessibility (§3, §12)', () => {
  it('moves focus to the heading at page scale and leaves the app chrome intact', () => {
    renderComponent(
      <div>
        <header>
          <a href="/console/">usher.</a>
        </header>
        <main>
          <Problem
            scale="page"
            problem={{
              code: 'not_found',
              status: 404,
              title: "We couldn't find that.",
              detail: 'No title exists with that id.',
            }}
          />
        </main>
      </div>,
    )
    expect(screen.getByRole('heading', { name: "We couldn't find that." })).toHaveFocus()
    expect(screen.getByRole('link', { name: 'usher.' })).toBeInTheDocument()
    expect(document.querySelector('.u-scrim')).toBeNull()
  })

  it.each(['toast', 'inline', 'panel'] as const)(
    'announces on arrival at %s scale, through a role that is implicitly polite',
    (scale) => {
      const { container } = renderComponent(<Problem scale={scale} problem={SOURCE_DOWN} />)
      // `role="status"` carries `aria-live: polite` implicitly. Stating it again is redundant;
      // `role="alert"` would carry `assertive` and need contradicting, which §12 forbids.
      expect(screen.getByRole('status')).toBeInTheDocument()
      expect(container.querySelector('[role="alert"]')).toBeNull()
      expect(container.querySelector('[aria-live]')).toBeNull()
    },
  )

  it('makes no live region at page scale — the focused heading is the announcement', () => {
    const { container } = renderComponent(<Problem scale="page" problem={SOURCE_DOWN} />)
    // §3 moves focus to the heading, and a focus move is announced. A live region around the
    // same words would read them a second time.
    expect(screen.getByRole('heading', { name: SOURCE_DOWN.title ?? '' })).toHaveFocus()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(container.querySelector('[aria-live]')).toBeNull()
  })

  it.each(['inline', 'panel', 'page', 'toast'] as const)(
    'has nothing assertive anywhere in the tree at %s scale',
    (scale) => {
      const { container } = renderComponent(<Problem scale={scale} problem={SOURCE_DOWN} />)
      expect(container.innerHTML).not.toContain('assertive')
      expect(container.querySelectorAll('[aria-live="assertive"]')).toHaveLength(0)
      expect(container.querySelectorAll('[role="alert"]')).toHaveLength(0)
    },
  )

  it('has no axe violations at panel scale', async () => {
    const { container } = renderComponent(
      <Problem
        problem={SOURCE_DOWN}
        onRetry={vi.fn<() => void>()}
        traceId="4f1c9e7a"
        onOpenTrace={vi.fn<() => void>()}
      />,
    )
    await expectNoViolations(container)
  })

  it('has no axe violations at page scale', async () => {
    const { container } = renderComponent(
      <Problem
        scale="page"
        problem={{ code: 'not_found', status: 404, detail: 'No title exists with that id.' }}
      />,
    )
    await expectNoViolations(container)
  })

  it('has no axe violations on the operator side (light, compact)', async () => {
    const { container } = renderComponent(
      <Problem
        problem={{
          code: 'method_not_allowed',
          status: 405,
          detail: 'POST /admin/jobs/j1/release is not a route on this server.',
        }}
      />,
      { theme: 'light', density: 'compact' },
    )
    await expectNoViolations(container)
  })
})

describe('Problem — anti-patterns', () => {
  it('shows detail verbatim and does not parse it', () => {
    const detail = 'database: connection pool exhausted after 20 attempts (base_url=http://10.0.0.4:8096).'
    const { container } = renderComponent(
      <Problem problem={{ code: 'source_unavailable', status: 503, detail }} />,
    )
    expect(screen.getByText(detail)).toBeInTheDocument()
    // Not linkified, not split, not summarised: the server's prose is reproduced as-is.
    expect(container.querySelectorAll('a')).toHaveLength(0)
    expect(container.querySelector('.u-problem__detail')?.textContent).toBe(detail)
  })
})
