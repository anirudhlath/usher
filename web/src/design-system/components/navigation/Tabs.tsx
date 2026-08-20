import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { useRef } from 'react'

/**
 * Real tabs — roving tabindex, arrow keys, Home/End, aria-controls/aria-labelledby wiring. The
 * reference client used clickable divs; this replaces them.
 *
 * `count` is for things the API actually counts (seasons, sources). Never put a count on a tab over
 * a keyset list — there are no totals.
 */
export interface TabItem {
  value: string
  label: string
  icon?: ReactNode
  /** Only when a real, non-paginated count exists. */
  count?: number
}
export interface TabsProps {
  /** Prefix for generated tab/panel ids. */
  id: string
  tabs: TabItem[]
  value: string
  onChange?: (value: string) => void
  children?: ReactNode
}

const tabId = (id: string, value: string) => `${id}-tab-${value}`
const panelId = (id: string, value: string) => `${id}-panel-${value}`

/** Real tabs: roving tabindex, arrow keys, Home/End, aria-controls wiring. */
export function Tabs({ id, tabs, value, onChange, children }: TabsProps) {
  const refs = useRef<Array<HTMLButtonElement | null>>([])

  const selectedIndex = tabs.findIndex((tab) => tab.value === value)
  /**
   * patterns.md §9/§12: exactly one tab is in the tab order, so `Tab` from outside
   * enters the tablist once and leaves it at the panel. When `value` matches no tab
   * nothing is selected — the first tab holds the tab stop, because a tablist no
   * key can reach is worse than a tablist whose stop is not the selected tab.
   */
  const rovingIndex = selectedIndex === -1 ? 0 : selectedIndex

  /**
   * Activate on move, not on Enter: the arrow key both changes the value and moves
   * focus. The key is handled on the tab that received it rather than on the
   * tablist, so it is the *focused* tab that moves — which stays correct when a
   * caller wires no `onChange` and focus and selection come apart.
   */
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, from: number) => {
    let next: number | null = null
    if (event.key === 'ArrowRight') next = (from + 1) % tabs.length
    else if (event.key === 'ArrowLeft') next = (from - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    if (next === null) return

    const nextTab = tabs[next]
    if (!nextTab) return
    event.preventDefault()
    onChange?.(nextTab.value)
    refs.current[next]?.focus()
  }

  return (
    <div>
      <div className="u-tabs" role="tablist">
        {tabs.map((tab, index) => (
          <button
            key={tab.value}
            ref={(node) => {
              refs.current[index] = node
            }}
            type="button"
            role="tab"
            id={tabId(id, tab.value)}
            className="u-tab"
            aria-selected={tab.value === value}
            aria-controls={panelId(id, tab.value)}
            tabIndex={index === rovingIndex ? 0 : -1}
            onClick={() => onChange?.(tab.value)}
            onKeyDown={(event) => handleKeyDown(event, index)}
          >
            {tab.icon}
            {tab.label}
            {/*
              The space is load-bearing, not formatting. Accessible-name
              computation concatenates adjacent text with no separator of its
              own, so `Specials` beside a count of `6` composed as the single
              word `Specials6` — which is what a screen reader said and what a
              `getByRole('tab', { name })` query had to match.

              A literal space rather than a visually-hidden separator, because
              `.u-tab` is `inline-flex` and a whitespace-only text node is not
              rendered as a flex item — the visible gap still comes from
              `gap: var(--space-2x)`. So the visible text and the accessible
              name end up identical, which is the property a voice-control user
              needs: what they say is what they see.
            */}
            {tab.count != null && (
              <>
                {' '}
                <span className="u-tab__count">{tab.count}</span>
              </>
            )}
          </button>
        ))}
      </div>
      <div
        className="u-tabpanel"
        role="tabpanel"
        id={panelId(id, value)}
        aria-labelledby={tabId(id, value)}
        tabIndex={0}
      >
        {children}
      </div>
    </div>
  )
}
