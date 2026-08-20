import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { PosterCard, type RowCard } from './index'

const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'

function card(overrides: Partial<RowCard> = {}): RowCard {
  return {
    title_id: '1',
    name: 'Stalker',
    year: 1979,
    kind: 'movie',
    enrichment_state: 'enriched',
    ...overrides,
  }
}

function cardIn(container: HTMLElement): HTMLElement {
  const found = container.querySelector('.u-card')
  if (!(found instanceof HTMLElement)) throw new Error('expected a card to be rendered')
  return found
}

describe('PosterCard', () => {
  describe('contract', () => {
    it('renders the portrait card classes the CSS expects', () => {
      const { container } = renderComponent(<PosterCard card={card()} />)
      const el = cardIn(container)

      expect(el).toHaveClass('u-card', 'u-card--poster')
      expect(el).not.toHaveClass('u-card--unowned')
      expect(el).not.toHaveClass('u-card--patched')
      expect(el).toHaveAttribute('type', 'button')
    })

    it('dims an un-owned collection member', () => {
      const { container } = renderComponent(<PosterCard card={card()} unowned />)
      expect(cardIn(container)).toHaveClass('u-card--unowned')
    })

    it('pins the overlay open once a title is started or played, and hides it otherwise', () => {
      const { container: idle } = renderComponent(<PosterCard card={card()} />)
      expect(idle.querySelector('.u-card__overlay')).not.toHaveClass('u-card__overlay--always')

      const { container: started } = renderComponent(
        <PosterCard card={card({ position_seconds: 4100, runtime_seconds: 9660 })} />,
      )
      expect(started.querySelector('.u-card__overlay')).toHaveClass('u-card__overlay--always')

      const { container: watched } = renderComponent(<PosterCard card={card({ played: true })} />)
      expect(watched.querySelector('.u-card__overlay')).toHaveClass('u-card__overlay--always')
    })

    it('renders no progress bar for a title nobody has touched', () => {
      renderComponent(<PosterCard card={card()} />)
      expect(screen.queryByRole('progressbar')).toBeNull()
    })

    it('renders the 3 px bar on the artwork once there is watch state', () => {
      const { container } = renderComponent(
        <PosterCard card={card({ position_seconds: 4100, runtime_seconds: 9660 })} />,
      )
      const bar = screen.getByRole('progressbar')

      expect(bar).toHaveAttribute('aria-valuetext', '68 of 161 min watched')
      expect(container.querySelector('.u-card__shot')?.contains(bar)).toBe(true)
    })

    it('prints episode_label verbatim, never recomposed', () => {
      renderComponent(<PosterCard card={card({ kind: 'series', episode_label: 'S02E05' })} />)
      expect(screen.getByText('S02E05')).toBeInTheDocument()
    })

    it('prints the tier for a skeleton title by default and drops it when asked', () => {
      const { unmount } = renderComponent(<PosterCard card={card({ enrichment_state: 'skeleton' })} />)
      expect(screen.getByText('· skeleton')).toHaveClass('u-card__tier')
      unmount()

      renderComponent(<PosterCard card={card({ enrichment_state: 'skeleton' })} showTier={false} />)
      expect(screen.queryByText('· skeleton')).toBeNull()
    })

    it('does not print a tier for an enriched title', () => {
      renderComponent(<PosterCard card={card()} />)
      expect(screen.queryByText('· skeleton')).toBeNull()
    })

    it('marks a series in the meta line', () => {
      renderComponent(<PosterCard card={card({ kind: 'series' })} />)
      expect(screen.getByText('· series')).toBeInTheDocument()
    })

    it('shows an em dash rather than a blank where there is no year', () => {
      renderComponent(<PosterCard card={card({ year: null })} />)
      expect(screen.getByText('—')).toBeInTheDocument()
    })

    it('renders a badge in its own corner slot', () => {
      const { container } = renderComponent(<PosterCard card={card()} badge={<span>4K</span>} />)
      expect(container.querySelector('.u-card__badge')?.textContent).toBe('4K')
    })

    it('draws the designed absent state when the payload carries no artwork', () => {
      renderComponent(<PosterCard card={card()} />)
      expect(screen.getByText('No artwork on record')).toBeInTheDocument()
    })

    it('asks the proxy for the poster rung', () => {
      const { container } = renderComponent(<PosterCard card={card({ artwork: 'abc' })} />)
      expect(container.querySelector('img')?.getAttribute('src')).toBe('/images/abc?w=342')
    })
  })

  describe('behaviour', () => {
    it('opens on click', async () => {
      const onOpen = vi.fn<() => void>()
      const { user } = renderComponent(<PosterCard card={card()} onOpen={onOpen} />)

      await user.click(screen.getByRole('button', { name: 'Stalker, 1979' }))

      expect(onOpen).toHaveBeenCalledTimes(1)
    })

    it('opens from the keyboard on Enter and on Space', async () => {
      const onOpen = vi.fn<() => void>()
      const { user } = renderComponent(<PosterCard card={card()} onOpen={onOpen} />)

      await user.tab()
      await user.keyboard('{Enter}')
      await user.keyboard(' ')

      expect(onOpen).toHaveBeenCalledTimes(2)
    })

    it('is inert without an onOpen rather than throwing', async () => {
      const { user } = renderComponent(<PosterCard card={card()} />)
      await user.click(screen.getByRole('button', { name: 'Stalker, 1979' }))
      expect(screen.getByRole('button', { name: 'Stalker, 1979' })).toBeInTheDocument()
    })
  })

  describe('accessibility (patterns.md §9)', () => {
    it('is exactly one focusable element, and one tab stop', async () => {
      const { container, user } = renderComponent(<PosterCard card={card()} badge={<span>4K</span>} />)

      expect(container.querySelectorAll(FOCUSABLE)).toHaveLength(1)

      await user.tab()
      expect(screen.getByRole('button', { name: 'Stalker, 1979' })).toHaveFocus()
      await user.tab()
      expect(screen.getByRole('button', { name: 'Stalker, 1979' })).not.toHaveFocus()
    })

    it.each([
      [{}, 'Stalker, 1979'],
      [{ position_seconds: 4100, runtime_seconds: 9660 }, 'Stalker, 1979, partly watched'],
      [{ played: true }, 'Stalker, 1979, watched'],
      [{ year: null }, 'Stalker'],
    ])('composes the whole name onto the one button: %o', (overrides, name) => {
      renderComponent(<PosterCard card={card(overrides)} />)
      expect(screen.getByRole('button', { name })).toBeInTheDocument()
    })

    it('has no violations', async () => {
      const { container } = renderComponent(
        <PosterCard
          card={card({
            artwork: 'abc',
            episode_label: 'S02E05',
            position_seconds: 600,
            runtime_seconds: 2700,
          })}
          badge={<span>4K</span>}
        />,
      )
      await expectNoViolations(container)
    })
  })

  describe('the live patch highlight (patterns.md §7)', () => {
    it('adds exactly one class and changes nothing else', () => {
      const { container, rerender } = renderComponent(
        <PosterCard card={card({ position_seconds: 4100, runtime_seconds: 9660 })} />,
      )
      const el = cardIn(container)
      const before = [...el.classList]
      const textBefore = el.textContent

      rerender(<PosterCard card={card({ position_seconds: 4100, runtime_seconds: 9660 })} patched />)

      const after = [...cardIn(container).classList]
      expect(after.filter((name) => !before.includes(name))).toEqual(['u-card--patched'])
      expect(before.filter((name) => !after.includes(name))).toEqual([])
      // Same node: a patch never remounts, never reorders.
      expect(cardIn(container)).toBe(el)
      expect(cardIn(container).textContent).toBe(textBefore)
    })

    it('applies no positional style — the card must not move, resize or reorder', () => {
      const { container } = renderComponent(<PosterCard card={card()} patched />)
      const el = cardIn(container)

      expect(el.getAttribute('style')).toBeNull()
      for (const property of ['transform', 'position', 'top', 'left', 'width', 'height', 'margin', 'order']) {
        expect(el.style.getPropertyValue(property)).toBe('')
      }
    })

    it('announces nothing — individual SSE frames are never read out', () => {
      const { container } = renderComponent(<PosterCard card={card()} patched />)

      expect(container.querySelector('[aria-live]')).toBeNull()
      expect(container.querySelector('[role="status"]')).toBeNull()
      expect(container.querySelector('[role="alert"]')).toBeNull()
    })
  })

  describe('anti-patterns', () => {
    it('never nests a play button inside the focusable card', () => {
      const { container } = renderComponent(
        <PosterCard card={card({ position_seconds: 4100, runtime_seconds: 9660 })} badge={<span>4K</span>} />,
      )

      expect(container.querySelectorAll('button')).toHaveLength(1)
      expect(screen.queryByRole('button', { name: /play/i })).toBeNull()
      expect(container.querySelector('button')?.querySelector(FOCUSABLE)).toBeNull()
    })

    it('never invents a percentage in the card meta', () => {
      const { container } = renderComponent(
        <PosterCard card={card({ position_seconds: 4100, runtime_seconds: 9660 })} />,
      )
      expect(container.textContent).not.toMatch(/%/)
    })
  })
})
