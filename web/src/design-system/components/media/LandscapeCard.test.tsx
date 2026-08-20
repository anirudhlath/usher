import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { LandscapeCard, type RowCard } from './index'

const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'

function card(overrides: Partial<RowCard> = {}): RowCard {
  return { title_id: '1', name: 'The Return', year: 2019, kind: 'episode', ...overrides }
}

function cardIn(container: HTMLElement): HTMLElement {
  const found = container.querySelector('.u-card')
  if (!(found instanceof HTMLElement)) throw new Error('expected a card to be rendered')
  return found
}

describe('LandscapeCard', () => {
  describe('contract', () => {
    it('renders the 16:9 card classes the CSS expects', () => {
      const { container } = renderComponent(<LandscapeCard card={card()} />)
      const el = cardIn(container)

      expect(el).toHaveClass('u-card', 'u-card--landscape')
      expect(el).toHaveAttribute('type', 'button')
      expect(container.querySelector('.u-art')).toHaveClass('u-art--backdrop')
    })

    it('takes the square box when the aspect is square', () => {
      const { container } = renderComponent(<LandscapeCard card={card()} aspect="square" />)
      expect(container.querySelector('.u-art')).toHaveClass('u-art--square')
    })

    it('asks the proxy for the landscape rung', () => {
      const { container } = renderComponent(<LandscapeCard card={card({ artwork: 'abc' })} />)
      expect(container.querySelector('img')?.getAttribute('src')).toBe('/images/abc?w=780')
    })

    it('replaces the year line with a subtitle when given one', () => {
      const { container } = renderComponent(
        <LandscapeCard card={card()} subtitle="Aired 12 Mar 2019 · 50 min" />,
      )

      expect(container.querySelector('.u-card__sub')?.textContent).toBe('Aired 12 Mar 2019 · 50 min')
      expect(screen.queryByText('2019')).toBeNull()
    })

    it('falls back to the year, then to an em dash', () => {
      const { container: withYear } = renderComponent(<LandscapeCard card={card()} />)
      expect(withYear.querySelector('.u-card__sub')?.textContent).toBe('2019')

      const { container: without } = renderComponent(<LandscapeCard card={card({ year: null })} />)
      expect(without.querySelector('.u-card__sub')?.textContent).toBe('—')
    })

    it('carries continue-watching progress in the overlay, pinned open', () => {
      const { container } = renderComponent(
        <LandscapeCard card={card({ position_seconds: 1400, runtime_seconds: 3000 })} />,
      )
      const overlay = container.querySelector('.u-card__overlay')

      expect(overlay).toHaveClass('u-card__overlay--always')
      expect(overlay?.contains(screen.getByRole('progressbar'))).toBe(true)
    })

    it('renders no progress bar for an untouched title', () => {
      renderComponent(<LandscapeCard card={card()} />)
      expect(screen.queryByRole('progressbar')).toBeNull()
    })

    it('prints episode_label verbatim in the overlay', () => {
      const { container } = renderComponent(<LandscapeCard card={card({ episode_label: 'S01E02' })} />)
      expect(container.querySelector('.u-card__ep-line')?.textContent).toBe('S01E02')
    })

    it('renders a badge in its own corner slot', () => {
      const { container } = renderComponent(<LandscapeCard card={card()} badge={<span>4K</span>} />)
      expect(container.querySelector('.u-card__badge')?.textContent).toBe('4K')
    })

    it('draws the designed absent state when the payload carries no artwork', () => {
      renderComponent(<LandscapeCard card={card()} />)
      expect(screen.getByText('No artwork on record')).toBeInTheDocument()
    })
  })

  describe('behaviour', () => {
    it('opens on click and from the keyboard', async () => {
      const onOpen = vi.fn<() => void>()
      const { user } = renderComponent(<LandscapeCard card={card()} onOpen={onOpen} />)

      await user.click(screen.getByRole('button', { name: 'The Return' }))
      await user.keyboard('{Enter}')

      expect(onOpen).toHaveBeenCalledTimes(2)
    })
  })

  describe('accessibility (patterns.md §9)', () => {
    it('is exactly one focusable element, and one tab stop', async () => {
      const { container, user } = renderComponent(<LandscapeCard card={card()} badge={<span>4K</span>} />)

      expect(container.querySelectorAll(FOCUSABLE)).toHaveLength(1)

      await user.tab()
      expect(screen.getByRole('button', { name: 'The Return' })).toHaveFocus()
      await user.tab()
      expect(screen.getByRole('button', { name: 'The Return' })).not.toHaveFocus()
    })

    it.each([
      [{}, 'The Return'],
      [{ episode_label: 'S01E02' }, 'The Return, S01E02'],
      [
        { episode_label: 'S01E02', position_seconds: 1400, runtime_seconds: 3000 },
        'The Return, S01E02, partly watched',
      ],
      [{ played: true }, 'The Return, watched'],
    ])('composes the whole name onto the one button: %o', (overrides, name) => {
      renderComponent(<LandscapeCard card={card(overrides)} />)
      expect(screen.getByRole('button', { name })).toBeInTheDocument()
    })

    it('has no violations', async () => {
      const { container } = renderComponent(
        <LandscapeCard
          card={card({
            artwork: 'abc',
            episode_label: 'S01E02',
            position_seconds: 1400,
            runtime_seconds: 3000,
          })}
          subtitle="Aired 12 Mar 2019 · 50 min"
          badge={<span>4K</span>}
        />,
      )
      await expectNoViolations(container)
    })
  })

  describe('the live patch highlight (patterns.md §7)', () => {
    it('adds exactly one class and changes nothing else', () => {
      const { container, rerender } = renderComponent(<LandscapeCard card={card()} />)
      const el = cardIn(container)
      const before = [...el.classList]

      rerender(<LandscapeCard card={card()} patched />)

      const after = [...cardIn(container).classList]
      expect(after.filter((name) => !before.includes(name))).toEqual(['u-card--patched'])
      expect(before.filter((name) => !after.includes(name))).toEqual([])
      expect(cardIn(container)).toBe(el)
    })

    it('applies no positional style — the card must not move, resize or reorder', () => {
      const { container } = renderComponent(<LandscapeCard card={card()} patched />)
      const el = cardIn(container)

      expect(el.getAttribute('style')).toBeNull()
      for (const property of ['transform', 'position', 'top', 'left', 'width', 'height', 'margin', 'order']) {
        expect(el.style.getPropertyValue(property)).toBe('')
      }
    })
  })

  describe('anti-patterns', () => {
    it('never nests a play button inside the focusable card', () => {
      const { container } = renderComponent(
        <LandscapeCard
          card={card({ position_seconds: 1400, runtime_seconds: 3000 })}
          badge={<span>4K</span>}
        />,
      )

      expect(container.querySelectorAll('button')).toHaveLength(1)
      expect(screen.queryByRole('button', { name: /play/i })).toBeNull()
    })
  })
})
