import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { SearchCombobox, type SuggestGroup, type SuggestItem } from './index'

/**
 * The two tiers as /search/suggest answers them: prefix is a btree scan that answers every
 * keystroke at ≥4 characters, fuzzy is trigram + Levenshtein and is debounced in the client.
 * They are separate queries, so both are on screen at once.
 */
const SOLARIS_1972: SuggestItem = { title_id: '1', name: 'Solaris', year: 1972, tier: 'enriched' }
const SOLARIS_2002: SuggestItem = { title_id: '2', name: 'Solaris', year: 2002, tier: 'skeleton' }
const SOLAR_OPPOSITES: SuggestItem = {
  title_id: '3',
  name: 'Solar Opposites',
  year: 2020,
  tier: 'stub',
}

const GROUPS: SuggestGroup[] = [
  { tier: 'prefix', label: 'Starts with', items: [SOLARIS_1972, SOLARIS_2002] },
  { tier: 'fuzzy', label: 'Close matches', items: [SOLAR_OPPOSITES] },
]

interface HarnessProps {
  groups?: SuggestGroup[]
  initialValue?: string
  initialOpen?: boolean
  initialActive?: number
  loading?: boolean
  onSubmit?: (item: SuggestItem | { free: string }) => void
}

/** The combobox is fully controlled; this is the state the consuming screen owns. */
function Harness({
  groups = GROUPS,
  initialValue = 'sol',
  initialOpen = false,
  initialActive = -1,
  loading = false,
  onSubmit = () => {},
}: HarnessProps) {
  const [value, setValue] = useState(initialValue)
  const [open, setOpen] = useState(initialOpen)
  const [active, setActive] = useState(initialActive)
  return (
    <SearchCombobox
      id="q"
      value={value}
      onChange={setValue}
      open={open}
      onOpenChange={setOpen}
      activeIndex={active}
      onActiveIndexChange={setActive}
      groups={groups}
      loading={loading}
      onSubmit={onSubmit}
    />
  )
}

const input = () => screen.getByRole('combobox')

function option(index: number): HTMLElement {
  const found = screen.getAllByRole('option')[index]
  if (!found) throw new Error(`no option at index ${index}`)
  return found
}

/** The active option, resolved the way a screen reader resolves it. */
function activeDescendant(): HTMLElement | null {
  const id = input().getAttribute('aria-activedescendant')
  if (!id) return null
  const node = document.getElementById(id)
  if (!node) throw new Error(`aria-activedescendant points at "${id}", which is not in the document`)
  return node
}

describe('SearchCombobox', () => {
  describe('contract', () => {
    it('renders the combobox furniture the CSS expects', () => {
      const { container } = renderComponent(<Harness />)

      expect(container.querySelector('.u-combo')).not.toBeNull()
      expect(input()).toHaveClass('u-input')
      expect(container.querySelector('.u-inputwrap__lead [data-icon="search"]')).not.toBeNull()
      expect(container.querySelector('.u-inputwrap__lead')).toHaveAttribute('aria-hidden', 'true')
    })

    it('renders the placeholder it was given, and a default otherwise', () => {
      const { rerender } = renderComponent(<Harness />)
      expect(input()).toHaveAttribute('placeholder', 'Search the catalog')

      rerender(<SearchCombobox id="q" value="" placeholder="Search the catalog  ·  press / anywhere" />)
      expect(input()).toHaveAttribute('placeholder', 'Search the catalog  ·  press / anywhere')
    })

    it('renders each suggestion with its tier chip and its year', () => {
      const { container } = renderComponent(<Harness initialOpen />)

      expect(screen.getAllByRole('option')).toHaveLength(3)
      // A skeleton-tier hit is a real catalog row, so the tier is shown rather than hidden.
      expect(container.querySelectorAll('.u-combo__tier')).toHaveLength(3)
      expect(option(1)).toHaveTextContent('skeleton')
      expect(option(0)).toHaveTextContent('1972')
    })

    it('prints an em dash where a suggestion has no year', () => {
      const { container } = renderComponent(
        <Harness
          groups={[
            {
              tier: 'prefix',
              label: 'Starts with',
              items: [{ title_id: '9', name: 'Solaris', year: null }],
            },
          ]}
          initialOpen
        />,
      )

      expect(container.querySelector('.u-combo__meta')).toHaveTextContent('—')
    })

    it('does not branch on density', () => {
      renderComponent(<Harness initialOpen />, { density: 'compact' })

      expect(input()).toHaveClass('u-input')
      expect(screen.getAllByRole('option')).toHaveLength(3)
    })
  })

  describe('the ARIA 1.2 combobox pattern (§12)', () => {
    it('carries role, autocomplete and expanded state', () => {
      renderComponent(<Harness />)

      expect(input()).toHaveAttribute('role', 'combobox')
      expect(input()).toHaveAttribute('aria-autocomplete', 'list')
      expect(input()).toHaveAttribute('aria-expanded', 'false')
      expect(input()).toHaveAttribute('autocomplete', 'off')
      expect(screen.queryByRole('listbox')).toBeNull()
    })

    it('points aria-controls at the listbox it opens', () => {
      renderComponent(<Harness initialOpen />)

      expect(input()).toHaveAttribute('aria-expanded', 'true')
      const listbox = screen.getByRole('listbox', { name: 'Suggestions' })
      expect(input().getAttribute('aria-controls')).toBe(listbox.id)
    })

    it('gives every option a stable id and marks only the active one selected', () => {
      renderComponent(<Harness initialOpen initialActive={1} />)

      const options = screen.getAllByRole('option')
      expect(options.map((o) => o.id)).toEqual(['q-opt-0', 'q-opt-1', 'q-opt-2'])
      expect(options.map((o) => o.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false'])
      expect(activeDescendant()).toBe(option(1))
    })

    it('omits aria-activedescendant when nothing is active', () => {
      renderComponent(<Harness initialOpen />)

      expect(input()).not.toHaveAttribute('aria-activedescendant')
    })
  })

  describe('the two suggest tiers are separate queries, not a fallback chain', () => {
    it('labels each tier with its own group', () => {
      renderComponent(<Harness initialOpen />)

      const groups = screen.getAllByRole('group')
      expect(groups).toHaveLength(2)
      expect(screen.getByRole('group', { name: 'Starts with' })).toBeInTheDocument()
      expect(screen.getByRole('group', { name: 'Close matches' })).toBeInTheDocument()
      // Both tiers answer at once — the second is not what you get when the first fails.
      expect(
        screen.getByRole('group', { name: 'Starts with' }).querySelectorAll('[role="option"]'),
      ).toHaveLength(2)
      expect(
        screen.getByRole('group', { name: 'Close matches' }).querySelectorAll('[role="option"]'),
      ).toHaveLength(1)
    })

    it('keeps the group headers out of the option list', () => {
      renderComponent(<Harness initialOpen />)

      expect(screen.getAllByRole('option').map((o) => o.textContent)).not.toContain('Starts with')
      expect(screen.getByText('Starts with')).toHaveClass('u-combo__group')
    })

    it('numbers option ids across both tiers, so one index addresses the whole list', async () => {
      const { user } = renderComponent(<Harness initialOpen />)

      await user.click(input())
      await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}')

      // The third press crosses the tier boundary without restarting the count.
      expect(activeDescendant()).toBe(
        screen.getByRole('group', { name: 'Close matches' }).querySelector('[role="option"]'),
      )
      expect(activeDescendant()?.id).toBe('q-opt-2')
    })
  })

  describe('the keyboard model (§9)', () => {
    it('moves the active descendant down and up, one real option at a time', async () => {
      const { user } = renderComponent(<Harness initialOpen />)

      await user.click(input())
      await user.keyboard('{ArrowDown}')
      expect(activeDescendant()).toBe(option(0))

      await user.keyboard('{ArrowDown}')
      expect(activeDescendant()).toBe(option(1))

      await user.keyboard('{ArrowUp}')
      expect(activeDescendant()).toBe(option(0))
    })

    it('clamps at both ends rather than wrapping', async () => {
      const { user } = renderComponent(<Harness initialOpen />)

      await user.click(input())
      await user.keyboard('{ArrowUp}{ArrowUp}')
      expect(activeDescendant()).toBe(option(0))

      await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}{ArrowDown}')
      expect(activeDescendant()).toBe(option(2))
    })

    it('opens the listbox on ArrowDown when it was closed', async () => {
      const { user } = renderComponent(<Harness />)

      await user.click(input())
      expect(input()).toHaveAttribute('aria-expanded', 'false')

      await user.keyboard('{ArrowDown}')
      expect(input()).toHaveAttribute('aria-expanded', 'true')
      expect(activeDescendant()).toBe(option(0))
    })

    it('keeps focus in the input — the active option is never focused', async () => {
      const { user } = renderComponent(<Harness initialOpen />)

      await user.click(input())
      await user.keyboard('{ArrowDown}{ArrowDown}')

      expect(input()).toHaveFocus()
      expect(document.activeElement).toBe(input())
      for (const opt of screen.getAllByRole('option')) {
        expect(opt).not.toHaveAttribute('tabindex')
        expect(opt).not.toHaveFocus()
      }
    })

    it('closes the listbox on Esc, and clears the field on a second Esc', async () => {
      const { user } = renderComponent(<Harness initialOpen initialActive={1} />)

      await user.click(input())
      await user.keyboard('{Escape}')
      expect(input()).toHaveAttribute('aria-expanded', 'false')
      expect(screen.queryByRole('listbox')).toBeNull()
      expect(input()).not.toHaveAttribute('aria-activedescendant')
      expect(input()).toHaveValue('sol')

      await user.keyboard('{Escape}')
      expect(input()).toHaveValue('')
    })

    it('closes exactly one layer — a consumed Esc does not reach the layer behind it', async () => {
      // patterns.md §9: Esc closes listbox → popover → dialog → drawer → sheet, innermost first.
      const outer = vi.fn<() => void>()
      const { user } = renderComponent(
        // A bare wrapper on purpose: what is under test is whether the keystroke propagates,
        // and a wrapper with a role of its own would change what bubbles to it.
        // eslint-disable-next-line jsx-a11y/no-static-element-interactions
        <div onKeyDown={outer}>
          <Harness initialOpen />
        </div>,
      )

      await user.click(input())
      await user.keyboard('{Escape}')
      expect(outer).not.toHaveBeenCalled()

      await user.keyboard('{Escape}')
      expect(outer).not.toHaveBeenCalled()
      expect(input()).toHaveValue('')

      // Nothing left to close: the keystroke belongs to whatever is outside.
      await user.keyboard('{Escape}')
      expect(outer).toHaveBeenCalledTimes(1)
    })

    it('submits the active suggestion on Enter', async () => {
      const onSubmit = vi.fn<(item: SuggestItem | { free: string }) => void>()
      const { user } = renderComponent(<Harness initialOpen onSubmit={onSubmit} />)

      await user.click(input())
      await user.keyboard('{ArrowDown}{ArrowDown}{Enter}')

      expect(onSubmit).toHaveBeenCalledExactlyOnceWith(SOLARIS_2002)
    })

    it('submits the free text on Enter when no option is active', async () => {
      const onSubmit = vi.fn<(item: SuggestItem | { free: string }) => void>()
      const { user } = renderComponent(<Harness initialOpen onSubmit={onSubmit} />)

      await user.click(input())
      await user.keyboard('{Enter}')

      expect(onSubmit).toHaveBeenCalledExactlyOnceWith({ free: 'sol' })
    })

    it('opens the listbox as soon as the user types', async () => {
      const { user } = renderComponent(<Harness initialValue="" />)

      await user.type(input(), 'sol')

      expect(input()).toHaveValue('sol')
      expect(input()).toHaveAttribute('aria-expanded', 'true')
    })
  })

  describe('pointer', () => {
    it('makes the hovered option the active one', async () => {
      const { user } = renderComponent(<Harness initialOpen />)

      await user.hover(option(2))

      expect(activeDescendant()).toBe(option(2))
    })

    it('submits the option that was clicked', async () => {
      const onSubmit = vi.fn<(item: SuggestItem | { free: string }) => void>()
      const { user } = renderComponent(<Harness initialOpen onSubmit={onSubmit} />)

      await user.click(option(2))

      expect(onSubmit).toHaveBeenCalledExactlyOnceWith(SOLAR_OPPOSITES)
    })
  })

  describe('empty and loading', () => {
    it('says what it is doing while the tiers are in flight', () => {
      renderComponent(<Harness groups={[]} initialOpen loading />)

      expect(screen.getByText('Looking…')).toHaveClass('u-combo__empty')
    })

    it('offers the full catalog when nothing matched', () => {
      renderComponent(<Harness groups={[]} initialOpen />)

      expect(screen.getByText('Nothing matched. Press Enter to search the full catalog.')).toHaveClass(
        'u-combo__empty',
      )
    })

    it('prefers an empty message the caller supplied', () => {
      renderComponent(
        <SearchCombobox
          id="q"
          value="qqxzz"
          open
          groups={[]}
          emptyMessage="Both lanes answered and neither found a match."
        />,
      )

      expect(screen.getByText('Both lanes answered and neither found a match.')).toBeInTheDocument()
    })

    it('submits free text from an empty listbox', async () => {
      const onSubmit = vi.fn<(item: SuggestItem | { free: string }) => void>()
      const { user } = renderComponent(<Harness groups={[]} initialOpen onSubmit={onSubmit} />)

      await user.click(input())
      await user.keyboard('{Enter}')

      expect(onSubmit).toHaveBeenCalledExactlyOnceWith({ free: 'sol' })
    })
  })

  describe('accessibility', () => {
    it('has no violations closed', async () => {
      const { container } = renderComponent(<Harness />)

      await expectNoViolations(container)
    })

    it('has no violations open, with both tiers and an active option', async () => {
      const { container } = renderComponent(<Harness initialOpen initialActive={1} />)

      await expectNoViolations(container)
    })

    it('has no violations with an empty listbox', async () => {
      const { container } = renderComponent(<Harness groups={[]} initialOpen />)

      await expectNoViolations(container)
    })
  })
})
