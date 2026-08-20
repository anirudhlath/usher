import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { LOAD_MORE_END_MESSAGE, LOAD_MORE_ROOT_MARGIN, LoadMore } from './index'

const CURSOR = 'eyJvIjoyNDAwfQ'

/**
 * jsdom has no real IntersectionObserver and the suite's setup stubs an inert one, so the
 * sentinel is verified two ways: that it is rendered and handed to `observe`, and — with this
 * recording double — that the callback path calls `onLoad` exactly once on approach.
 */
class RecordingObserver implements IntersectionObserver {
  static latest: RecordingObserver | null = null
  readonly root = null
  readonly rootMargin: string
  readonly scrollMargin = ''
  readonly thresholds: ReadonlyArray<number> = []
  readonly observed: Element[] = []
  disconnected = false
  private readonly callback: IntersectionObserverCallback

  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.callback = callback
    this.rootMargin = options?.rootMargin ?? ''
    RecordingObserver.latest = this
  }

  observe(target: Element): void {
    this.observed.push(target)
  }

  unobserve(): void {}

  disconnect(): void {
    this.disconnected = true
  }

  takeRecords(): IntersectionObserverEntry[] {
    return []
  }

  /** Drive the callback the way a real observer would on approach. */
  approach(isIntersecting: boolean): void {
    const target = this.observed[0]
    if (!target) throw new Error('the sentinel was never observed')
    const rect = target.getBoundingClientRect()
    this.callback(
      [
        {
          boundingClientRect: rect,
          intersectionRatio: isIntersecting ? 1 : 0,
          intersectionRect: rect,
          isIntersecting,
          rootBounds: null,
          target,
          time: 0,
        },
      ],
      this,
    )
  }
}

describe('LoadMore — the end of a list is a sentence (§4)', () => {
  it('says so when next_cursor is null', () => {
    renderComponent(<LoadMore nextCursor={null} />)
    expect(screen.getByText(LOAD_MORE_END_MESSAGE)).toHaveClass('u-more__end')
    expect(LOAD_MORE_END_MESSAGE).toBe('That is everything we have for this filter.')
  })

  it('says so when next_cursor is simply absent', () => {
    renderComponent(<LoadMore />)
    expect(screen.getByText(LOAD_MORE_END_MESSAGE)).toBeInTheDocument()
  })

  it('lets the surface word its own ending', () => {
    renderComponent(<LoadMore nextCursor={null} endMessage="That is every unmatched file on this source." />)
    expect(screen.getByText('That is every unmatched file on this source.')).toBeInTheDocument()
    expect(screen.queryByText(LOAD_MORE_END_MESSAGE)).toBeNull()
  })

  it('offers nothing to click once the list is exhausted', () => {
    renderComponent(<LoadMore nextCursor={null} onLoad={vi.fn<() => void>()} />)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('never stops silently: something is always rendered at the end', () => {
    const { container } = renderComponent(<LoadMore nextCursor={null} />)
    expect(container.textContent?.trim()).not.toBe('')
  })
})

describe('LoadMore — the explicit button (operator tables)', () => {
  it('offers a button while a cursor remains', () => {
    renderComponent(<LoadMore nextCursor={CURSOR} onLoad={vi.fn<() => void>()} />)
    expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument()
  })

  it('loads on click', async () => {
    const onLoad = vi.fn<() => void>()
    const { user } = renderComponent(<LoadMore nextCursor={CURSOR} onLoad={onLoad} />)
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    expect(onLoad).toHaveBeenCalledExactlyOnceWith()
  })

  it('states that one thing is pending, and blocks a second click', async () => {
    const onLoad = vi.fn<() => void>()
    const { user } = renderComponent(<LoadMore nextCursor={CURSOR} loading onLoad={onLoad} />)
    const button = screen.getByRole('button', { name: 'Loading…' })
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button).toBeDisabled()
    await user.click(button)
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('works in the operator default (light, compact)', async () => {
    const onLoad = vi.fn<() => void>()
    const { user } = renderComponent(
      <LoadMore nextCursor={CURSOR} onLoad={onLoad} loadedLabel="200 loaded so far" />,
      { theme: 'light', density: 'compact' },
    )
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    expect(onLoad).toHaveBeenCalledOnce()
    expect(screen.getByText('200 loaded so far')).toBeInTheDocument()
  })
})

describe('LoadMore — the sentinel (viewer grids and rails)', () => {
  let previous: typeof globalThis.IntersectionObserver

  beforeEach(() => {
    previous = globalThis.IntersectionObserver
    RecordingObserver.latest = null
    vi.stubGlobal('IntersectionObserver', RecordingObserver)
  })

  afterEach(() => {
    vi.stubGlobal('IntersectionObserver', previous)
  })

  it('renders a sentinel and observes it at 600 px of approach', () => {
    const { container } = renderComponent(
      <LoadMore nextCursor={CURSOR} autoLoad onLoad={vi.fn<() => void>()} />,
    )
    const sentinel = container.querySelector('.u-more__sentinel')
    expect(sentinel).not.toBeNull()
    expect(RecordingObserver.latest?.observed).toEqual([sentinel])
    expect(RecordingObserver.latest?.rootMargin).toBe(LOAD_MORE_ROOT_MARGIN)
    expect(LOAD_MORE_ROOT_MARGIN).toBe('600px')
  })

  it('loads when the sentinel is approached', () => {
    const onLoad = vi.fn<() => void>()
    renderComponent(<LoadMore nextCursor={CURSOR} autoLoad onLoad={onLoad} />)
    RecordingObserver.latest?.approach(true)
    expect(onLoad).toHaveBeenCalledOnce()
  })

  it('does not load while the sentinel is out of range', () => {
    const onLoad = vi.fn<() => void>()
    renderComponent(<LoadMore nextCursor={CURSOR} autoLoad onLoad={onLoad} />)
    RecordingObserver.latest?.approach(false)
    expect(onLoad).not.toHaveBeenCalled()
  })

  it('is not the last row: it sits ahead of the footer’s own control', () => {
    const { container } = renderComponent(
      <LoadMore nextCursor={CURSOR} autoLoad onLoad={vi.fn<() => void>()} />,
    )
    const footer = container.querySelector('.u-more')
    expect(footer?.firstElementChild).toHaveClass('u-more__sentinel')
    expect(footer?.lastElementChild).not.toHaveClass('u-more__sentinel')
  })

  it('keeps the button in auto-load mode, so the final page stays reachable by keyboard', () => {
    renderComponent(<LoadMore nextCursor={CURSOR} autoLoad onLoad={vi.fn<() => void>()} />)
    expect(screen.getByRole('button', { name: 'Load more' })).toBeInTheDocument()
  })

  it('keeps the sentinel out of the accessibility tree', () => {
    const { container } = renderComponent(
      <LoadMore nextCursor={CURSOR} autoLoad onLoad={vi.fn<() => void>()} />,
    )
    expect(container.querySelector('.u-more__sentinel')).toHaveAttribute('aria-hidden', 'true')
  })

  it('observes nothing for an operator table', () => {
    const { container } = renderComponent(<LoadMore nextCursor={CURSOR} onLoad={vi.fn<() => void>()} />)
    expect(container.querySelector('.u-more__sentinel')).toBeNull()
    expect(RecordingObserver.latest).toBeNull()
  })

  it('observes nothing while a page is already in flight', () => {
    renderComponent(<LoadMore nextCursor={CURSOR} autoLoad loading onLoad={vi.fn<() => void>()} />)
    expect(RecordingObserver.latest).toBeNull()
  })

  it('observes nothing once the list is exhausted', () => {
    const { container } = renderComponent(
      <LoadMore nextCursor={null} autoLoad onLoad={vi.fn<() => void>()} />,
    )
    expect(container.querySelector('.u-more__sentinel')).toBeNull()
    expect(RecordingObserver.latest).toBeNull()
  })

  it('disconnects when the footer goes away', () => {
    const { unmount } = renderComponent(
      <LoadMore nextCursor={CURSOR} autoLoad onLoad={vi.fn<() => void>()} />,
    )
    const observer = RecordingObserver.latest
    unmount()
    expect(observer?.disconnected).toBe(true)
  })
})

describe('LoadMore — progress counts loaded, never remaining (§4, §14)', () => {
  it('renders the loaded label as given', () => {
    renderComponent(
      <LoadMore nextCursor={CURSOR} onLoad={vi.fn<() => void>()} loadedLabel="72 loaded so far" />,
    )
    expect(screen.getByText('72 loaded so far')).toHaveClass('u-more__note')
  })

  it('renders no denominator, no total and no page number', () => {
    const { container } = renderComponent(
      <LoadMore nextCursor={CURSOR} onLoad={vi.fn<() => void>()} loadedLabel="72 loaded so far" />,
    )
    expect(container.textContent).not.toMatch(/\d+\s*(of|\/)\s*\d+/i)
    expect(container.textContent).not.toMatch(/total|results?|page \d+|remaining/i)
    expect(screen.queryByRole('navigation')).toBeNull()
  })

  it('has no progressbar, because there is no denominator to fill', () => {
    renderComponent(
      <LoadMore nextCursor={CURSOR} onLoad={vi.fn<() => void>()} loadedLabel="72 loaded so far" />,
    )
    expect(screen.queryByRole('progressbar')).toBeNull()
  })

  it('omits the note entirely when the surface has no honest count to give', () => {
    const { container } = renderComponent(<LoadMore nextCursor={CURSOR} onLoad={vi.fn<() => void>()} />)
    expect(container.querySelector('.u-more__note')).toBeNull()
  })
})

describe('LoadMore — accessibility', () => {
  it('has no axe violations with a cursor', async () => {
    const { container } = renderComponent(
      <LoadMore nextCursor={CURSOR} onLoad={vi.fn<() => void>()} loadedLabel="72 loaded so far" />,
    )
    await expectNoViolations(container)
  })

  it('has no axe violations while loading', async () => {
    const { container } = renderComponent(
      <LoadMore nextCursor={CURSOR} loading onLoad={vi.fn<() => void>()} />,
    )
    await expectNoViolations(container)
  })

  it('has no axe violations at the end of the list', async () => {
    const { container } = renderComponent(<LoadMore nextCursor={null} />, {
      theme: 'light',
      density: 'compact',
    })
    await expectNoViolations(container)
  })
})
