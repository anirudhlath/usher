import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { TitleRow, type TitleRowProps } from './index'

const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'

function title(overrides: Partial<TitleRowProps['title']> = {}): TitleRowProps['title'] {
  return {
    title_id: '1',
    name: 'Stalker',
    year: 1979,
    kind: 'movie',
    enrichment_state: 'enriched',
    ...overrides,
  }
}

function rowIn(container: HTMLElement): HTMLElement {
  const found = container.querySelector('.u-row')
  if (!(found instanceof HTMLElement)) throw new Error('expected a row to be rendered')
  return found
}

describe('TitleRow', () => {
  describe('contract', () => {
    it('renders the row classes the CSS expects', () => {
      const { container } = renderComponent(<TitleRow title={title()} />)
      const el = rowIn(container)

      expect(el).toHaveClass('u-row')
      expect(el).not.toHaveClass('u-row--patched')
      expect(el).toHaveAttribute('type', 'button')
      expect(container.querySelector('.u-row__title')?.textContent).toBe('Stalker')
    })

    it('prints the year and kind in the sub line', () => {
      const { container } = renderComponent(<TitleRow title={title()} />)
      const sub = container.querySelector('.u-row__sub')

      expect(sub?.textContent).toContain('1979')
      expect(sub?.textContent).toContain('movie')
    })

    it('caps genres at three and separates them with a middle dot', () => {
      const { container } = renderComponent(
        <TitleRow title={title({ genres: ['Science Fiction', 'Drama', 'Mystery', 'Thriller'] })} />,
      )

      expect(container.querySelector('.u-row__genres')?.textContent).toBe('Science Fiction · Drama · Mystery')
      expect(container.textContent).not.toContain('Thriller')
    })

    it('renders no genre node at all when the payload carries none', () => {
      const { container } = renderComponent(<TitleRow title={title()} />)
      expect(container.querySelector('.u-row__genres')).toBeNull()
    })

    it('renders no genre node for a computed-and-empty list either', () => {
      const { container } = renderComponent(<TitleRow title={title({ genres: [] })} />)
      expect(container.querySelector('.u-row__genres')).toBeNull()
    })

    it('renders trailing and meta slots', () => {
      const { container } = renderComponent(
        <TitleRow title={title()} trailing={<span>owned</span>} meta={<span>1.27M</span>} />,
      )

      expect(container.querySelector('.u-row__trail')?.textContent).toBe('owned')
      expect(container.querySelector('.u-row__sub')?.textContent).toContain('1.27M')
    })
  })

  describe('/browse carries no artwork', () => {
    it('renders no image and requests none by default', () => {
      const { container } = renderComponent(<TitleRow title={title()} />)

      expect(container.querySelector('.u-row__thumb')).toBeNull()
      expect(container.querySelector('img')).toBeNull()
      expect(container.querySelector('.u-art')).toBeNull()
    })

    it('renders a thumbnail at the smallest rung only when asked and the payload has one', () => {
      const { container } = renderComponent(<TitleRow title={title({ artwork: 'abc' })} thumb />)

      expect(container.querySelector('.u-row__thumb')).toBeInTheDocument()
      expect(container.querySelector('img')?.getAttribute('src')).toBe('/images/abc?w=154')
    })

    it('draws the designed absent state rather than a broken image when thumb is on but artwork is not', () => {
      const { container } = renderComponent(<TitleRow title={title()} thumb />)

      expect(screen.getByText('No artwork on record')).toBeInTheDocument()
      expect(container.querySelector('img')).toBeNull()
    })
  })

  describe('a skeleton-tier title is sparse, not broken', () => {
    it('states the tier plainly and still reads as a legitimate row', () => {
      const { container } = renderComponent(
        <TitleRow title={{ name: 'Solaris', enrichment_state: 'skeleton' }} />,
      )
      const el = rowIn(container)

      expect(el).toHaveClass('u-row')
      expect(screen.getByText('skeleton')).toHaveClass('u-row__tier')
      expect(container.querySelector('.u-row__title')?.textContent).toBe('Solaris')
      // Nothing in the sparse row is drawn as a failure.
      expect(container.textContent).not.toMatch(/error|missing|unknown|failed|broken/i)
    })

    it('shows an em dash for the year rather than a blank or a zero', () => {
      const { container } = renderComponent(
        <TitleRow title={{ name: 'Solaris', enrichment_state: 'skeleton' }} />,
      )
      const sub = container.querySelector('.u-row__sub')

      expect(sub?.textContent).toContain('—')
      expect(sub?.textContent).not.toContain('0')
    })

    it('keeps the whole title in the accessible name of a name-only row', () => {
      renderComponent(<TitleRow title={{ name: 'Solaris' }} />)
      const row = screen.getByRole('button', { name: /Solaris/ })

      expect(row).toHaveAccessibleName(/Solaris/)
      expect(row).toHaveAccessibleName(/—/)
    })

    it('does not print a tier for an enriched title', () => {
      renderComponent(<TitleRow title={title()} />)
      expect(screen.queryByText('skeleton')).toBeNull()
    })
  })

  describe('behaviour', () => {
    it('opens on click and from the keyboard', async () => {
      const onOpen = vi.fn<() => void>()
      const { user } = renderComponent(<TitleRow title={title()} onOpen={onOpen} />)

      await user.click(screen.getByRole('button', { name: /Stalker/ }))
      await user.keyboard('{Enter}')

      expect(onOpen).toHaveBeenCalledTimes(2)
    })
  })

  describe('accessibility (patterns.md §9)', () => {
    it('is exactly one focusable element, and one tab stop', async () => {
      const { container, user } = renderComponent(<TitleRow title={title()} trailing={<span>owned</span>} />)

      expect(container.querySelectorAll(FOCUSABLE)).toHaveLength(1)

      await user.tab()
      expect(screen.getByRole('button', { name: /Stalker/ })).toHaveFocus()
      await user.tab()
      expect(screen.getByRole('button', { name: /Stalker/ })).not.toHaveFocus()
    })

    /**
     * The row's name is composed from its own content rather than an `aria-label`, so a trailing
     * badge ("owned") stays part of it. Asserted fact by fact: the separators between the parts
     * come from the flex boxes in `media.css`, and this environment loads no CSS (`css: false`).
     */
    it('composes every fact in the row into the one button name', () => {
      renderComponent(
        <TitleRow title={title({ genres: ['Science Fiction'] })} trailing={<span>owned</span>} />,
      )
      const row = screen.getByRole('button', { name: /Stalker/ })

      for (const fact of ['Stalker', '1979', 'movie', 'Science Fiction', 'owned']) {
        expect(row).toHaveAccessibleName(new RegExp(fact))
      }
    })

    it('has no violations', async () => {
      const { container } = renderComponent(
        <>
          <TitleRow title={title({ genres: ['Science Fiction', 'Drama'] })} trailing={<span>owned</span>} />
          <TitleRow title={{ name: 'Solaris', enrichment_state: 'skeleton' }} />
          <TitleRow title={title({ artwork: 'abc' })} thumb />
        </>,
      )
      await expectNoViolations(container)
    })
  })

  describe('the live patch highlight (patterns.md §7)', () => {
    it('adds exactly one class and changes nothing else', () => {
      const { container, rerender } = renderComponent(<TitleRow title={title()} />)
      const el = rowIn(container)
      const before = [...el.classList]
      const textBefore = el.textContent

      rerender(<TitleRow title={title()} patched />)

      const after = [...rowIn(container).classList]
      expect(after.filter((name) => !before.includes(name))).toEqual(['u-row--patched'])
      expect(before.filter((name) => !after.includes(name))).toEqual([])
      expect(rowIn(container)).toBe(el)
      expect(rowIn(container).textContent).toBe(textBefore)
    })

    it('applies no positional style and announces nothing', () => {
      const { container } = renderComponent(<TitleRow title={title()} patched />)
      const el = rowIn(container)

      expect(el.getAttribute('style')).toBeNull()
      for (const property of ['transform', 'position', 'top', 'left', 'width', 'height', 'margin', 'order']) {
        expect(el.style.getPropertyValue(property)).toBe('')
      }
      expect(container.querySelector('[aria-live]')).toBeNull()
    })
  })

  describe('anti-patterns', () => {
    it('never fetches an image per row for a payload that has none', () => {
      const { container } = renderComponent(<TitleRow title={title()} thumb={false} />)
      expect(container.querySelectorAll('img')).toHaveLength(0)
    })

    it('never nests an interactive control inside the row button', () => {
      const { container } = renderComponent(<TitleRow title={title()} trailing={<span>owned</span>} />)
      expect(container.querySelector('button')?.querySelector(FOCUSABLE)).toBeNull()
    })
  })
})
