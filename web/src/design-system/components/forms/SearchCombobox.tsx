import type { ChangeEvent, KeyboardEvent } from 'react'
import { Icon } from '../icon'

export interface SuggestItem {
  title_id: string
  name: string
  year?: number | null
  tier?: 'skeleton' | 'stub' | 'enriched'
}

export interface SuggestGroup {
  tier: 'prefix' | 'fuzzy'
  label: string
  items: SuggestItem[]
}

/**
 * The search type-ahead. A real ARIA 1.2 combobox: aria-expanded, aria-controls, aria-autocomplete,
 * aria-activedescendant, roving option ids, Escape to close, Enter to submit the free text.
 * Suggest has two tiers that are NOT fallbacks for each other and must be labelled separately:
 * prefix (btree, ≥4 chars, answers every keystroke) and fuzzy (trigram + Levenshtein, ≥1 char,
 * p50 33.6 ms — the client debounces; the server does not).
 */
export interface SearchComboboxProps {
  id: string
  value: string
  onChange?: (v: string) => void
  onSubmit?: (item: SuggestItem | { free: string }) => void
  groups?: SuggestGroup[]
  loading?: boolean
  placeholder?: string
  open?: boolean
  onOpenChange?: (open: boolean) => void
  activeIndex?: number
  onActiveIndexChange?: (i: number) => void
  emptyMessage?: string
}

/**
 * ARIA 1.2 combobox for /search/suggest. Two tiers, labelled, never presented as fallbacks for
 * each other: prefix answers every keystroke at ≥4 chars; fuzzy is debounced (p50 33.6 ms).
 *
 * **The two tiers get their own `role="group"` headers and that is a correctness rule, not
 * decoration.** They are two different queries against two different indexes. Merging them into
 * one list would tell the user that "Solar Opposites" is a worse "Solaris", which is not what
 * happened: one query matched a prefix and the other matched a trigram, and the user is entitled
 * to see which.
 *
 * **The keyboard model** (patterns.md §9, §12):
 *
 * - `↓` opens the listbox if closed and moves the active descendant down; `↑` moves it up. Both
 *   clamp at the ends — no wrap-around, so holding a key never cycles past the boundary.
 * - **Focus never leaves the input.** The active option is named by `aria-activedescendant`
 *   pointing at that option's id; options carry no `tabIndex` and are never focused. This is the
 *   whole reason the pattern exists — typing must keep working while an option is active.
 * - `Esc` closes the listbox; a second `Esc` clears the field. Whichever of the two it did, the
 *   event stops there: §9 requires `Esc` to close exactly one layer, innermost first, so a
 *   combobox inside a dialog must not close the dialog on the same keystroke.
 * - `Enter` submits the active suggestion, or the free text when no option is active. Free text
 *   is a first-class outcome — the catalog is far larger than the suggest tiers.
 */
export function SearchCombobox({
  id,
  value,
  onChange,
  onSubmit,
  groups = [],
  loading = false,
  placeholder = 'Search the catalog',
  open = false,
  onOpenChange,
  activeIndex = -1,
  onActiveIndexChange,
  emptyMessage,
}: SearchComboboxProps) {
  const listId = `${id}-listbox`
  const optionId = (index: number) => `${id}-opt-${index}`

  // Option ids are assigned across the groups in render order, so `activeIndex` addresses one
  // flat sequence while the listbox still shows two labelled tiers.
  let next = 0
  const sections = groups.map((group) => ({
    group,
    entries: group.items.map((item) => ({ item, index: next++ })),
  }))
  const flat = sections.flatMap((section) => section.entries.map((entry) => entry.item))

  const move = (delta: number) => {
    if (flat.length === 0) return
    onActiveIndexChange?.(Math.max(0, Math.min(flat.length - 1, activeIndex + delta)))
  }

  const activeOptionId =
    open && activeIndex >= 0 && activeIndex < flat.length ? optionId(activeIndex) : undefined

  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    onChange?.(e.target.value)
    onOpenChange?.(true)
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      onOpenChange?.(true)
      move(1)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      move(-1)
    } else if (e.key === 'Escape') {
      if (open) {
        e.stopPropagation()
        onOpenChange?.(false)
        onActiveIndexChange?.(-1)
      } else if (value !== '') {
        e.stopPropagation()
        onChange?.('')
      }
    } else if (e.key === 'Enter') {
      const item = activeIndex >= 0 ? flat[activeIndex] : undefined
      onSubmit?.(item ?? { free: value })
    }
  }

  return (
    <div className="u-combo">
      <div className="u-inputwrap u-inputwrap--lead">
        <span className="u-inputwrap__lead" aria-hidden="true">
          <Icon name="search" size={16} />
        </span>
        <input
          id={id}
          className="u-input"
          type="text"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeOptionId}
          autoComplete="off"
          placeholder={placeholder}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
        />
      </div>
      {open && (
        // The popup is the styled surface; the listbox is the thing inside it that holds
        // options. Keeping them separate is what lets the "nothing matched" sentence be a
        // status message rather than a fake option in a list of real ones.
        <div className="u-combo__list">
          <div role="listbox" id={listId} aria-label="Suggestions">
            {sections.map(({ group, entries }) => (
              <div key={group.tier} role="group" aria-labelledby={`${id}-group-${group.tier}`}>
                <div className="u-combo__group" id={`${id}-group-${group.tier}`}>
                  {group.label}
                </div>
                {entries.map(({ item, index }) => (
                  // The keyboard path for an option is the combobox input's own handler plus
                  // `aria-activedescendant`; an option that took focus or listened for keys
                  // would break the pattern this component exists to implement.
                  // eslint-disable-next-line jsx-a11y/click-events-have-key-events
                  <div
                    key={item.title_id}
                    id={optionId(index)}
                    role="option"
                    aria-selected={index === activeIndex}
                    className="u-combo__opt"
                    onMouseEnter={() => onActiveIndexChange?.(index)}
                    onClick={() => onSubmit?.(item)}
                  >
                    <span>{item.name}</span>
                    {/* A skeleton-tier hit is a real catalog row, so the tier is printed rather
                        than used to hide or grey the suggestion. */}
                    {item.tier && <span className="u-combo__tier">{item.tier}</span>}
                    <span className="u-combo__meta">{item.year || '—'}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
          {flat.length === 0 && (
            <div className="u-combo__empty" role="status">
              {loading
                ? 'Looking…'
                : (emptyMessage ?? 'Nothing matched. Press Enter to search the full catalog.')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
