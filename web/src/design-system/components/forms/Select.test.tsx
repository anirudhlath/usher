import type { ChangeEvent } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Select, type SelectOption } from './index'

/** Browse sort — the option set the contract names. */
const SORT: SelectOption[] = [
  { value: 'popularity', label: 'Popularity' },
  { value: 'name', label: 'Name' },
  { value: 'year', label: 'Year' },
  { value: 'vote_count', label: 'Vote count' },
]

function describedBy(el: HTMLElement): HTMLElement[] {
  const ids = (el.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
  return ids.map((id) => {
    const node = el.ownerDocument.getElementById(id)
    if (!node) throw new Error(`aria-describedby points at "${id}", which is not in the document`)
    return node
  })
}

describe('Select', () => {
  describe('contract', () => {
    it('renders a real native select with the classes the CSS expects', () => {
      renderComponent(<Select id="sort" label="Sort" options={SORT} />)

      const select = screen.getByRole('combobox', { name: 'Sort' })
      expect(select.tagName).toBe('SELECT')
      expect(select).toHaveClass('u-input', 'u-select')
    })

    it('renders every option in order, value and label', () => {
      renderComponent(<Select id="sort" label="Sort" options={SORT} />)

      const options = screen.getAllByRole<HTMLOptionElement>('option')
      expect(options.map((o) => o.value)).toEqual(['popularity', 'name', 'year', 'vote_count'])
      expect(options.map((o) => o.textContent)).toEqual(['Popularity', 'Name', 'Year', 'Vote count'])
    })

    it('renders the chevron as decoration, out of the accessibility tree', () => {
      const { container } = renderComponent(<Select id="sort" label="Sort" options={SORT} />)

      const chev = container.querySelector('.u-select__chev')
      expect(chev).toHaveAttribute('aria-hidden', 'true')
      expect(chev?.querySelector('[data-icon="chevron-down"]')).not.toBeNull()
    })

    it('binds the hint and prints an error with hue, icon and word', () => {
      const { container } = renderComponent(
        <Select
          id="kind"
          label="Kind"
          options={[{ value: 'emby', label: 'Emby' }]}
          error="Field required."
        />,
      )

      const select = screen.getByRole('combobox', { name: 'Kind' })
      expect(select).toHaveAttribute('aria-invalid', 'true')
      expect(describedBy(select)[0]).toHaveTextContent('Field required.')
      expect(screen.getByRole('status')).toHaveClass('u-field__error')
      expect(container.querySelector('.u-field__error [data-icon="x-circle"]')).not.toBeNull()
    })

    it('replaces the hint with the error and leaves no dangling description', () => {
      renderComponent(
        <Select
          id="kind"
          label="Kind"
          options={[{ value: 'emby', label: 'Emby' }]}
          hint="Emby is the only source type today."
          error="Field required."
        />,
      )

      expect(screen.queryByText('Emby is the only source type today.')).toBeNull()
      expect(describedBy(screen.getByRole('combobox', { name: 'Kind' }))).toHaveLength(1)
    })

    it('spreads the rest of the native props, so an uncontrolled default still works', () => {
      renderComponent(
        <Select
          id="mode"
          label="Mode"
          data-testid="mode"
          defaultValue="full_text"
          options={[
            { value: 'fused', label: 'Fused (default)' },
            { value: 'full_text', label: 'Lexical' },
            { value: 'semantic', label: 'Semantic' },
          ]}
        />,
      )

      expect(screen.getByTestId<HTMLSelectElement>('mode').value).toBe('full_text')
    })

    it('disables the control when asked', () => {
      renderComponent(<Select id="sort" label="Sort" options={SORT} disabled />)

      expect(screen.getByRole('combobox', { name: 'Sort' })).toBeDisabled()
    })

    it('does not branch on density', () => {
      renderComponent(<Select id="sort" label="Sort" options={SORT} />, { density: 'compact' })

      expect(screen.getByRole('combobox', { name: 'Sort' })).toHaveClass('u-input', 'u-select')
    })
  })

  describe('behaviour', () => {
    it('reports the chosen value', async () => {
      // Read the value inside the handler: a controlled select is reset on the next render, so
      // reading it afterwards would assert React's reset rather than the user's choice.
      const seen: string[] = []
      const onChange = vi.fn<(e: ChangeEvent<HTMLSelectElement>) => void>((e) => {
        seen.push(e.target.value)
      })
      const { user } = renderComponent(
        <Select id="sort" label="Sort" options={SORT} value="popularity" onChange={onChange} />,
      )

      await user.selectOptions(screen.getByRole('combobox', { name: 'Sort' }), 'year')

      expect(onChange).toHaveBeenCalledTimes(1)
      expect(seen).toEqual(['year'])
    })

    it('honours a controlled value', () => {
      renderComponent(
        <Select id="sort" label="Sort" options={SORT} value="vote_count" onChange={vi.fn<() => void>()} />,
      )

      expect(screen.getByRole<HTMLSelectElement>('combobox', { name: 'Sort' }).value).toBe('vote_count')
    })
  })

  describe('anti-patterns', () => {
    it('never builds a custom listbox for a short fixed option set', () => {
      const { container } = renderComponent(<Select id="sort" label="Sort" options={SORT} />)

      expect(container.querySelectorAll('select')).toHaveLength(1)
      expect(container.querySelector('[role="listbox"]')).toBeNull()
      expect(container.querySelector('[role="option"]')).toBeNull()
    })

    it('names the control with a bound label, never a placeholder option', () => {
      renderComponent(<Select id="sort" label="Sort" options={SORT} />)

      const label = screen.getByText('Sort')
      expect(label.tagName).toBe('LABEL')
      expect(label).toHaveAttribute('for', 'sort')
      // A placeholder-as-label is an option with an empty value that vanishes on first choice.
      expect(screen.getAllByRole<HTMLOptionElement>('option').every((o) => o.value !== '')).toBe(true)
      expect(screen.getByLabelText('Sort')).toBe(screen.getByRole('combobox', { name: 'Sort' }))
    })
  })

  describe('accessibility', () => {
    it('has no violations with a label and a hint', async () => {
      const { container } = renderComponent(
        <Select id="sort" label="Sort" options={SORT} hint="Changing sort restarts the list from the top." />,
      )

      await expectNoViolations(container)
    })

    it('has no violations in the error state', async () => {
      const { container } = renderComponent(
        <Select id="sort" label="Sort" options={SORT} error="Field required." />,
      )

      await expectNoViolations(container)
    })
  })
})
