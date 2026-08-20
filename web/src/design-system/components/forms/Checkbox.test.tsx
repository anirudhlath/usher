import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Checkbox } from './index'

describe('Checkbox', () => {
  describe('contract', () => {
    it('renders a checkbox inside the label the CSS expects', () => {
      const { container } = renderComponent(<Checkbox id="owned" label="Owned only" />)

      const box = screen.getByRole('checkbox', { name: 'Owned only' })
      expect(box).toHaveAttribute('type', 'checkbox')
      expect(container.querySelector('label')).toHaveClass('u-check')
      expect(container.querySelector('label')).toHaveAttribute('for', 'owned')
    })

    it('renders a radio when asked', () => {
      renderComponent(<Checkbox id="mode-fused" name="mode" label="Fused" radio />)

      const radio = screen.getByRole('radio', { name: 'Fused' })
      expect(radio).toHaveAttribute('type', 'radio')
    })

    it('carries the disabled class on the label as well as the control', () => {
      const { container } = renderComponent(<Checkbox id="d" label="Disabled" disabled />)

      expect(container.querySelector('label')).toHaveClass('u-check', 'u-check--disabled')
      expect(screen.getByRole('checkbox', { name: 'Disabled' })).toBeDisabled()
    })

    it('renders the hint as a second line, bound as a description', () => {
      renderComponent(
        <Checkbox id="owned" label="Owned only" hint="Titles with at least one copy on a source." />,
      )

      const box = screen.getByRole('checkbox', { name: 'Owned only' })
      const describedBy = box.getAttribute('aria-describedby') ?? ''
      const hint = document.getElementById(describedBy)
      expect(hint).toHaveClass('u-field__hint', 'u-field__hint--block')
      expect(hint).toHaveTextContent('Titles with at least one copy on a source.')
    })

    it('spreads the rest of the native props onto the control', () => {
      renderComponent(<Checkbox id="owned" label="Owned only" data-testid="owned" defaultChecked />)

      expect(screen.getByTestId('owned')).toBeChecked()
    })

    it('does not branch on density', () => {
      const { container } = renderComponent(<Checkbox id="owned" label="Owned only" />, {
        density: 'compact',
      })

      expect(container.querySelector('label')).toHaveClass('u-check')
    })
  })

  describe('indeterminate', () => {
    it('sets the DOM property, which has no HTML attribute', () => {
      renderComponent(<Checkbox id="all" label="Select all on this page" indeterminate />)

      const box = screen.getByRole<HTMLInputElement>('checkbox', { name: 'Select all on this page' })
      expect(box.indeterminate).toBe(true)
      expect(box).toBePartiallyChecked()
      // There is no `indeterminate=""` in HTML — a component that "sets" it as an attribute
      // renders a control that looks unchecked and reports unchecked.
      expect(box).not.toHaveAttribute('indeterminate')
    })

    it('follows the prop when the select-all resolves to all or none', () => {
      const { rerender } = renderComponent(
        <Checkbox id="all" label="Select all on this page" indeterminate />,
      )
      const box = screen.getByRole<HTMLInputElement>('checkbox', { name: 'Select all on this page' })
      expect(box.indeterminate).toBe(true)

      rerender(<Checkbox id="all" label="Select all on this page" checked readOnly />)
      expect(box.indeterminate).toBe(false)
      expect(box).toBeChecked()
    })

    it('is a display state, not a third value — the control still reports a boolean', async () => {
      // The tri-state `owned` filter belongs to FilterChip, which prints its state as a word.
      const onChange = vi.fn<() => void>()
      const { user } = renderComponent(
        <Checkbox id="all" label="Select all on this page" indeterminate onChange={onChange} />,
      )

      const box = screen.getByRole<HTMLInputElement>('checkbox', { name: 'Select all on this page' })
      expect(box.checked).toBe(false)
      expect(box).not.toHaveAttribute('aria-checked', 'mixed')

      await user.click(box)
      expect(onChange).toHaveBeenCalledTimes(1)
      expect(box.checked).toBe(true)
    })
  })

  describe('behaviour', () => {
    it('toggles when the user clicks the label text', async () => {
      const onChange = vi.fn<() => void>()
      const { user } = renderComponent(<Checkbox id="owned" label="Owned only" onChange={onChange} />)

      await user.click(screen.getByText('Owned only'))

      expect(screen.getByRole('checkbox', { name: 'Owned only' })).toBeChecked()
      expect(onChange).toHaveBeenCalledTimes(1)
    })

    it('toggles from the keyboard', async () => {
      const { user } = renderComponent(<Checkbox id="owned" label="Owned only" />)

      await user.tab()
      const box = screen.getByRole('checkbox', { name: 'Owned only' })
      expect(box).toHaveFocus()
      await user.keyboard(' ')

      expect(box).toBeChecked()
    })

    it('does not toggle while disabled', async () => {
      const onChange = vi.fn<() => void>()
      const { user } = renderComponent(<Checkbox id="d" label="Disabled" disabled onChange={onChange} />)

      await user.click(screen.getByText('Disabled'))

      expect(onChange).not.toHaveBeenCalled()
      expect(screen.getByRole('checkbox', { name: 'Disabled' })).not.toBeChecked()
    })
  })

  describe('accessibility', () => {
    it('names the control with the label alone, leaving the hint a description', () => {
      renderComponent(
        <Checkbox id="owned" label="Owned only" hint="Titles with at least one copy on a source." />,
      )

      // The accessible name is what a screen reader announces first and what the operator was
      // told to look for; folding a sentence of hint into it buries the name.
      expect(screen.getByRole('checkbox', { name: 'Owned only' })).toBeInTheDocument()
    })

    it('has no violations across checkbox, radio, hint, disabled and indeterminate', async () => {
      const { container } = renderComponent(
        <>
          <Checkbox id="a" label="Owned only" hint="Titles with at least one copy on a source." />
          <Checkbox id="b" label="Select all on this page" indeterminate />
          <Checkbox id="c" label="Disabled" disabled />
          <Checkbox id="d" name="mode" label="Fused" radio />
        </>,
      )

      await expectNoViolations(container)
    })
  })
})
