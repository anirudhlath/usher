import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { FilterChip } from './index'

/** /browse's tri-state owned filter, wired the way the screen wires it. */
function OwnedFilter({ initial }: { initial?: boolean }) {
  const [value, setValue] = useState<boolean | undefined>(initial)
  return <FilterChip label="Owned" tri value={value} onToggle={setValue} />
}

describe('FilterChip', () => {
  describe('contract', () => {
    it('is a toggle button with aria-pressed, not a checkbox', () => {
      renderComponent(<FilterChip label="Science Fiction" />)

      const chip = screen.getByRole('button', { name: /Science Fiction/ })
      expect(chip).toHaveClass('u-chip')
      expect(chip).toHaveAttribute('type', 'button')
      expect(chip).toHaveAttribute('aria-pressed', 'false')
      expect(screen.queryByRole('checkbox')).toBeNull()
    })

    it('presses when active', () => {
      renderComponent(<FilterChip label="Science Fiction" active />)

      expect(screen.getByRole('button', { name: /Science Fiction/ })).toHaveAttribute('aria-pressed', 'true')
    })

    it('shows the remove glyph only when a removable chip is set', () => {
      const { container, rerender } = renderComponent(<FilterChip label="Science Fiction" removable />)
      expect(container.querySelector('.u-chip__x')).toBeNull()

      rerender(<FilterChip label="Science Fiction" removable active />)
      const remove = container.querySelector('.u-chip__x')
      expect(remove).toHaveAttribute('aria-hidden', 'true')
      expect(remove?.querySelector('[data-icon="x"]')).not.toBeNull()
    })

    it('renders the tri variant with its own class', () => {
      renderComponent(<OwnedFilter />)

      expect(screen.getByRole('button', { name: /Owned/ })).toHaveClass('u-chip', 'u-chip--tri')
    })

    it('does not branch on density', () => {
      renderComponent(<FilterChip label="Science Fiction" active />, { density: 'compact' })

      expect(screen.getByRole('button', { name: /Science Fiction/ })).toHaveClass('u-chip')
    })
  })

  describe('the selected state carries more than colour (§12)', () => {
    it('adds the check glyph and a word when a plain chip is set', () => {
      const { container, rerender } = renderComponent(<FilterChip label="Science Fiction" />)
      expect(container.querySelector('[data-icon="check"]')).toBeNull()
      expect(screen.queryByText('Selected')).toBeNull()

      rerender(<FilterChip label="Science Fiction" active />)
      expect(container.querySelector('[data-icon="check"]')).not.toBeNull()
      expect(screen.getByText('Selected')).toHaveClass('u-visually-hidden')
      const chip = screen.getByRole('button', { name: /Science Fiction/ })
      expect(chip).toHaveAccessibleName(/Selected/)
      expect(chip).toHaveAttribute('aria-pressed', 'true')
    })

    it('prints the tri state as a word, and gives each state its own glyph', () => {
      const { container, rerender } = renderComponent(
        <FilterChip label="Owned" tri value={undefined} onToggle={vi.fn<() => void>()} />,
      )
      expect(screen.getByText('Either')).toHaveClass('u-chip__state')
      expect(container.querySelector('[data-icon]')).toBeNull()

      rerender(<FilterChip label="Owned" tri value onToggle={vi.fn<() => void>()} />)
      expect(screen.getByText('Owned', { selector: '.u-chip__state' })).toBeInTheDocument()
      expect(container.querySelector('[data-icon="check"]')).not.toBeNull()

      rerender(<FilterChip label="Owned" tri value={false} onToggle={vi.fn<() => void>()} />)
      expect(screen.getByText('Not owned')).toHaveClass('u-chip__state')
      expect(container.querySelector('[data-icon="x"]')).not.toBeNull()
    })

    it('presses the tri chip only when it constrains the query', () => {
      const { rerender } = renderComponent(
        <FilterChip label="Owned" tri value={undefined} onToggle={vi.fn<() => void>()} />,
      )
      // "Either" is the absence of a filter, so the chip is not pressed.
      expect(screen.getByRole('button', { name: /Owned/ })).toHaveAttribute('aria-pressed', 'false')

      rerender(<FilterChip label="Owned" tri value={false} onToggle={vi.fn<() => void>()} />)
      expect(screen.getByRole('button', { name: /Owned/ })).toHaveAttribute('aria-pressed', 'true')
    })
  })

  describe('behaviour', () => {
    it('reports the next boolean when toggled', async () => {
      const onToggle = vi.fn<(next: boolean | undefined) => void>()
      const { user } = renderComponent(<FilterChip label="Science Fiction" onToggle={onToggle} />)

      await user.click(screen.getByRole('button', { name: /Science Fiction/ }))

      expect(onToggle).toHaveBeenCalledExactlyOnceWith(true)
    })

    it('clears an active chip on the next click', async () => {
      const onToggle = vi.fn<(next: boolean | undefined) => void>()
      const { user } = renderComponent(
        <FilterChip label="Science Fiction" active removable onToggle={onToggle} />,
      )

      await user.click(screen.getByRole('button', { name: /Science Fiction/ }))

      expect(onToggle).toHaveBeenCalledExactlyOnceWith(false)
    })

    it('cycles either → owned → not owned → either', async () => {
      const { user } = renderComponent(<OwnedFilter />)

      const chip = () => screen.getByRole('button', { name: /Owned/ })
      // The state word, read off its own element: the chip's whole text contains the label too,
      // so a substring assertion over the button would pass on every state.
      const state = () => chip().querySelector('.u-chip__state')?.textContent
      expect(state()).toBe('Either')

      await user.click(chip())
      expect(state()).toBe('Owned')
      expect(chip()).toHaveAttribute('aria-pressed', 'true')

      await user.click(chip())
      expect(state()).toBe('Not owned')
      expect(chip()).toHaveAttribute('aria-pressed', 'true')

      await user.click(chip())
      expect(state()).toBe('Either')
      expect(chip()).toHaveAttribute('aria-pressed', 'false')
    })

    it('is operable from the keyboard as a button', async () => {
      const onToggle = vi.fn<(next: boolean | undefined) => void>()
      const { user } = renderComponent(<FilterChip label="Science Fiction" onToggle={onToggle} />)

      await user.tab()
      expect(screen.getByRole('button', { name: /Science Fiction/ })).toHaveFocus()
      await user.keyboard('{Enter}')

      expect(onToggle).toHaveBeenCalledExactlyOnceWith(true)
    })
  })

  describe('anti-patterns', () => {
    it('never nests a second focusable control inside the chip', async () => {
      // The remove glyph is decoration on the chip's own button. A nested button would put two
      // tab stops in one pill and make "which one did I just activate?" a real question.
      const { container, user } = renderComponent(
        <FilterChip label="Science Fiction" active removable onToggle={vi.fn<() => void>()} />,
      )

      expect(container.querySelectorAll('button')).toHaveLength(1)
      await user.tab()
      await user.tab()
      expect(document.body).toHaveFocus()
    })
  })

  describe('accessibility', () => {
    it('has no violations across the plain and tri variants', async () => {
      const { container } = renderComponent(
        <>
          <FilterChip label="Science Fiction" active removable onToggle={vi.fn<() => void>()} />
          <FilterChip label="Drama" onToggle={vi.fn<() => void>()} />
          <OwnedFilter initial />
          <FilterChip label="Owned" tri value={false} onToggle={vi.fn<() => void>()} />
          <FilterChip label="Owned" tri onToggle={vi.fn<() => void>()} />
        </>,
      )

      await expectNoViolations(container)
    })
  })
})
