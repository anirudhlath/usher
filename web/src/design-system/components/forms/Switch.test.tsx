import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Switch } from './index'

/** The row-providers screen: the label is a database slug, the description is the translation. */
const SLUG = 'franchise-completion'
const PLAIN_LANGUAGE =
  'Finishes a series or a franchise you have already started — the missing entries of something you own part of, not new recommendations.'

function Stateful({ initial = false, disabled = false }: { initial?: boolean; disabled?: boolean }) {
  const [checked, setChecked] = useState(initial)
  return (
    <Switch
      id="p-franchise"
      checked={checked}
      onChange={setChecked}
      label={SLUG}
      description={PLAIN_LANGUAGE}
      disabled={disabled}
    />
  )
}

describe('Switch', () => {
  describe('contract', () => {
    it('renders a real switch with the classes the CSS expects', () => {
      const { container } = renderComponent(
        <Switch id="p-curated" checked label="curated" onChange={vi.fn<() => void>()} />,
      )

      const control = screen.getByRole('switch', { name: 'curated' })
      expect(control).toHaveClass('u-switch')
      expect(container.querySelector('.u-switch__track .u-switch__knob')).not.toBeNull()
    })

    it('carries the state on aria-checked, which the CSS keys the track off', () => {
      const { rerender } = renderComponent(
        <Switch id="p-curated" checked={false} label="curated" onChange={vi.fn<() => void>()} />,
      )
      expect(screen.getByRole('switch', { name: 'curated' })).toHaveAttribute('aria-checked', 'false')

      rerender(<Switch id="p-curated" checked label="curated" onChange={vi.fn<() => void>()} />)
      expect(screen.getByRole('switch', { name: 'curated' })).toHaveAttribute('aria-checked', 'true')
    })

    it('is focusable, and drops out of the tab order when disabled', () => {
      const { rerender } = renderComponent(
        <Switch id="p-curated" checked label="curated" onChange={vi.fn<() => void>()} />,
      )
      expect(screen.getByRole('switch', { name: 'curated' })).toHaveAttribute('tabindex', '0')

      rerender(<Switch id="p-curated" checked label="curated" onChange={vi.fn<() => void>()} disabled />)
      const control = screen.getByRole('switch', { name: 'curated' })
      expect(control).toHaveAttribute('tabindex', '-1')
      expect(control).toHaveAttribute('aria-disabled', 'true')
    })

    it('does not branch on density', () => {
      renderComponent(<Switch id="p-curated" checked label="curated" onChange={vi.fn<() => void>()} />, {
        density: 'compact',
      })

      expect(screen.getByRole('switch', { name: 'curated' })).toHaveClass('u-switch')
    })
  })

  describe('the slug is explained in plain language (§12)', () => {
    it('names the switch with the label and describes it with the translation', () => {
      renderComponent(<Stateful />)

      const control = screen.getByRole('switch', { name: SLUG })
      const descId = control.getAttribute('aria-describedby') ?? ''
      expect(descId).not.toBe('')
      const description = document.getElementById(descId)
      expect(description).toHaveTextContent(PLAIN_LANGUAGE)
      expect(description).toHaveClass('u-field__hint')
    })

    it('binds the label by id rather than leaving the switch unnamed', () => {
      renderComponent(<Stateful />)

      const control = screen.getByRole('switch', { name: SLUG })
      const labelId = control.getAttribute('aria-labelledby') ?? ''
      expect(document.getElementById(labelId)).toHaveTextContent(SLUG)
      // An unnamed switch was one of the reference client's measured accessibility failures.
      expect(control).toHaveAccessibleName(SLUG)
      expect(control).toHaveAccessibleDescription(PLAIN_LANGUAGE)
    })

    it('omits aria-describedby entirely when there is nothing to describe', () => {
      renderComponent(<Switch id="p-curated" checked label="curated" onChange={vi.fn<() => void>()} />)

      expect(screen.getByRole('switch', { name: 'curated' })).not.toHaveAttribute('aria-describedby')
    })
  })

  describe('behaviour', () => {
    it('toggles on click', async () => {
      const { user } = renderComponent(<Stateful />)

      const control = screen.getByRole('switch', { name: SLUG })
      expect(control).toHaveAttribute('aria-checked', 'false')

      await user.click(control)
      expect(screen.getByRole('switch', { name: SLUG })).toHaveAttribute('aria-checked', 'true')
    })

    it('toggles on Space and on Enter, with focus on the switch', async () => {
      const { user } = renderComponent(<Stateful />)

      await user.tab()
      const control = screen.getByRole('switch', { name: SLUG })
      expect(control).toHaveFocus()

      await user.keyboard(' ')
      expect(screen.getByRole('switch', { name: SLUG })).toHaveAttribute('aria-checked', 'true')

      await user.keyboard('{Enter}')
      expect(screen.getByRole('switch', { name: SLUG })).toHaveAttribute('aria-checked', 'false')
    })

    it('reports the next state, not the current one', async () => {
      const onChange = vi.fn<(next: boolean) => void>()
      const { user } = renderComponent(
        <Switch id="p-curated" checked={false} label="curated" onChange={onChange} />,
      )

      await user.click(screen.getByRole('switch', { name: 'curated' }))

      expect(onChange).toHaveBeenCalledExactlyOnceWith(true)
    })

    it('does nothing at all while disabled', async () => {
      const onChange = vi.fn<() => void>()
      const { user } = renderComponent(
        <Switch id="p-curated" checked label="curated" onChange={onChange} disabled />,
      )

      const control = screen.getByRole('switch', { name: 'curated' })
      await user.click(control)
      control.focus()
      await user.keyboard(' ')
      await user.keyboard('{Enter}')

      expect(onChange).not.toHaveBeenCalled()
      expect(control).toHaveAttribute('aria-checked', 'true')
    })
  })

  describe('accessibility', () => {
    it('has no violations, on or off, described or bare', async () => {
      const { container } = renderComponent(
        <>
          <Stateful initial />
          <Switch id="p-curated" checked={false} label="curated" onChange={vi.fn<() => void>()} />
          <Switch
            id="p-disabled"
            checked
            label="because-you-watched-*"
            onChange={vi.fn<() => void>()}
            disabled
          />
        </>,
      )

      await expectNoViolations(container)
    })
  })
})
