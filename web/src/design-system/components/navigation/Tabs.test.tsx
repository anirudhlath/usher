import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Icon } from '../icon'
import { Tabs, type TabItem } from '.'

const SEASONS: TabItem[] = [
  { value: '0', label: 'Specials', count: 6 },
  { value: '1', label: 'Season 1', count: 10 },
  { value: '2', label: 'Season 2', count: 10 },
  { value: '3', label: 'Season 3' },
]

/**
 * Tabs is controlled, so the keyboard model is only observable through a caller
 * that owns the value — the same shape the season switcher has.
 */
function SeasonTabs({ onChange, initial = '1' }: { onChange?: (value: string) => void; initial?: string }) {
  const [value, setValue] = useState(initial)
  return (
    <Tabs
      id="s"
      value={value}
      tabs={SEASONS}
      onChange={(next) => {
        setValue(next)
        onChange?.(next)
      }}
    >
      <p>Season {value} episodes</p>
    </Tabs>
  )
}

const tabNames = () => screen.getAllByRole('tab').map((tab) => tab.textContent)

describe('Tabs — contract', () => {
  it('renders a real tablist of real tabs and one panel', () => {
    renderComponent(
      <Tabs id="s" value="1" tabs={SEASONS}>
        <p>Season 1 episodes</p>
      </Tabs>,
    )
    expect(screen.getByRole('tablist')).toHaveClass('u-tabs')
    expect(screen.getAllByRole('tab')).toHaveLength(4)
    expect(screen.getByRole('tabpanel')).toHaveClass('u-tabpanel')
    for (const tab of screen.getAllByRole('tab')) {
      expect(tab.tagName).toBe('BUTTON')
      expect(tab).toHaveAttribute('type', 'button')
      expect(tab).toHaveClass('u-tab')
    }
  })

  it('marks exactly the tab whose value matches as selected', () => {
    renderComponent(
      <Tabs id="s" value="2" tabs={SEASONS}>
        <p>Season 2 episodes</p>
      </Tabs>,
    )
    expect(screen.getByRole('tab', { selected: true })).toHaveAccessibleName(/Season 2/)
    expect(screen.getAllByRole('tab', { selected: false })).toHaveLength(3)
  })

  it('renders a count only where one was given', () => {
    const { container } = renderComponent(
      <Tabs id="s" value="1" tabs={SEASONS}>
        <p>Season 1 episodes</p>
      </Tabs>,
    )
    // §14 / the prompt: a count is only ever a real, non-paginated one. Season 3
    // has none, so the tab shows none — no zero, no placeholder.
    expect(container.querySelectorAll('.u-tab__count')).toHaveLength(3)
    expect(screen.getByRole('tab', { name: /Season 3/ }).querySelector('.u-tab__count')).toBeNull()
    // A space between the label and the count, so the accessible name is a
    // phrase rather than one run-together word. Asserted on the *name*
    // rather than on `textContent` because the name is what a screen
    // reader announces and what a role query matches.
    expect(tabNames()).toEqual(['Specials 6', 'Season 1 10', 'Season 2 10', 'Season 3'])
  })

  it('renders a count of zero, which is a real count', () => {
    renderComponent(
      <Tabs id="s" value="a" tabs={[{ value: 'a', label: 'Parked', count: 0 }]}>
        <p>Nothing parked</p>
      </Tabs>,
    )
    expect(screen.getByRole('tab', { name: /Parked/ }).querySelector('.u-tab__count')).toHaveTextContent('0')
  })

  it('renders a tab icon', () => {
    const { container } = renderComponent(
      <Tabs id="dev" value="req" tabs={[{ value: 'req', label: 'Requests', icon: <Icon name="terminal" /> }]}>
        <p>Journal</p>
      </Tabs>,
    )
    expect(container.querySelector('[data-icon="terminal"]')).not.toBeNull()
  })

  it('renders the same markup in both densities — nothing branches on density', () => {
    const comfortable = renderComponent(
      <Tabs id="s" value="1" tabs={SEASONS}>
        <p>Season 1 episodes</p>
      </Tabs>,
    )
    const comfortableHtml = comfortable.container.innerHTML
    comfortable.unmount()

    const compact = renderComponent(
      <Tabs id="s" value="1" tabs={SEASONS}>
        <p>Season 1 episodes</p>
      </Tabs>,
      { density: 'compact' },
    )
    expect(compact.container.innerHTML).toBe(comfortableHtml)
  })
})

describe('Tabs — aria wiring (patterns.md §12)', () => {
  it('wires every tab to a panel id and the panel back to its tab', () => {
    renderComponent(
      <Tabs id="s" value="1" tabs={SEASONS}>
        <p>Season 1 episodes</p>
      </Tabs>,
    )
    const selected = screen.getByRole('tab', { selected: true })
    const panel = screen.getByRole('tabpanel')

    expect(selected).toHaveAttribute('id', 's-tab-1')
    expect(selected).toHaveAttribute('aria-controls', 's-panel-1')
    expect(panel).toHaveAttribute('id', 's-panel-1')
    expect(panel).toHaveAttribute('aria-labelledby', 's-tab-1')

    // Every tab, not just the selected one.
    for (const tab of screen.getAllByRole('tab')) {
      expect(tab.getAttribute('aria-controls')).toMatch(/^s-panel-/)
      expect(tab.id).toMatch(/^s-tab-/)
    }
  })

  it('has no axe violations', async () => {
    const { container } = renderComponent(<SeasonTabs />)
    // The premise first: axe over an empty container passes too.
    expect(screen.getAllByRole('tab')).toHaveLength(4)
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()
    await expectNoViolations(container)
  })
})

describe('Tabs — keyboard (patterns.md §9)', () => {
  it('puts exactly one tab in the tab order and moves it with the selection', async () => {
    const { user } = renderComponent(<SeasonTabs />)
    const inOrder = () => screen.getAllByRole('tab').filter((tab) => tab.tabIndex === 0)

    expect(inOrder()).toHaveLength(1)
    expect(inOrder()[0]).toHaveAccessibleName(/Season 1/)

    await user.keyboard('{Tab}')
    await user.keyboard('{ArrowRight}')

    expect(inOrder()).toHaveLength(1)
    expect(inOrder()[0]).toHaveAccessibleName(/Season 2/)
  })

  it('takes one Tab to enter the tablist and one more to leave it at the panel', async () => {
    const { user } = renderComponent(
      <div>
        <button type="button">before</button>
        <SeasonTabs />
      </div>,
    )
    await user.tab()
    expect(screen.getByRole('button', { name: 'before' })).toHaveFocus()

    await user.tab()
    expect(screen.getByRole('tab', { name: /Season 1/ })).toHaveFocus()

    // Not the next tab: three of the four are out of the tab order.
    await user.tab()
    expect(screen.getByRole('tabpanel')).toHaveFocus()
  })

  it('activates on move — ArrowRight changes the value with no Enter', async () => {
    const onChange = vi.fn<(value: string) => void>()
    const { user } = renderComponent(<SeasonTabs onChange={onChange} />)

    await user.tab()
    await user.keyboard('{ArrowRight}')

    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith('2')
    expect(screen.getByRole('tab', { selected: true })).toHaveAccessibleName(/Season 2/)
    expect(screen.getByRole('tab', { name: /Season 2/ })).toHaveFocus()
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Season 2 episodes')
  })

  it('does not need Enter, and Enter on the focused tab changes nothing further', async () => {
    const onChange = vi.fn<(value: string) => void>()
    const { user } = renderComponent(<SeasonTabs onChange={onChange} />)

    await user.tab()
    await user.keyboard('{ArrowRight}')
    expect(onChange).toHaveBeenCalledTimes(1)

    await user.keyboard('{Enter}')
    expect(onChange).toHaveBeenLastCalledWith('2')
    expect(screen.getByRole('tab', { selected: true })).toHaveAccessibleName(/Season 2/)
  })

  it('wraps at both ends with the arrow keys', async () => {
    const onChange = vi.fn<(value: string) => void>()
    const { user } = renderComponent(<SeasonTabs onChange={onChange} initial="0" />)

    await user.tab()
    await user.keyboard('{ArrowLeft}')
    expect(onChange).toHaveBeenLastCalledWith('3')
    expect(screen.getByRole('tab', { name: /Season 3/ })).toHaveFocus()

    await user.keyboard('{ArrowRight}')
    expect(onChange).toHaveBeenLastCalledWith('0')
    expect(screen.getByRole('tab', { name: /Specials/ })).toHaveFocus()
  })

  it('jumps to the first tab on Home and the last on End', async () => {
    const onChange = vi.fn<(value: string) => void>()
    const { user } = renderComponent(<SeasonTabs onChange={onChange} />)

    await user.tab()
    await user.keyboard('{End}')
    expect(onChange).toHaveBeenLastCalledWith('3')
    expect(screen.getByRole('tab', { name: /Season 3/ })).toHaveFocus()

    await user.keyboard('{Home}')
    expect(onChange).toHaveBeenLastCalledWith('0')
    expect(screen.getByRole('tab', { name: /Specials/ })).toHaveFocus()
  })

  it('leaves keys it does not own alone', async () => {
    const onChange = vi.fn<(value: string) => void>()
    const { user } = renderComponent(<SeasonTabs onChange={onChange} />)

    await user.tab()
    await user.keyboard('{ArrowDown}{ArrowUp}{Escape}')
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('Tabs — behaviour', () => {
  it('changes the value on click', async () => {
    const onChange = vi.fn<(value: string) => void>()
    const { user } = renderComponent(<SeasonTabs onChange={onChange} />)

    await user.click(screen.getByRole('tab', { name: /Specials/ }))
    expect(onChange).toHaveBeenCalledWith('0')
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Season 0 episodes')
  })

  it('is controlled: without onChange the selection does not move', async () => {
    const { user } = renderComponent(
      <Tabs id="s" value="1" tabs={SEASONS}>
        <p>Season 1 episodes</p>
      </Tabs>,
    )
    await user.click(screen.getByRole('tab', { name: /Specials/ }))
    expect(screen.getByRole('tab', { selected: true })).toHaveAccessibleName(/Season 1/)
  })

  it('keeps a tab reachable when the value matches no tab', () => {
    renderComponent(
      <Tabs id="s" value="none" tabs={SEASONS}>
        <p>Nothing</p>
      </Tabs>,
    )
    // Nothing is selected, but a tablist no key can reach would be worse.
    expect(screen.queryByRole('tab', { selected: true })).toBeNull()
    expect(screen.getAllByRole('tab').filter((tab) => tab.tabIndex === 0)).toHaveLength(1)
  })

  it('renders an empty tablist without a crash', () => {
    renderComponent(
      <Tabs id="s" value="" tabs={[]}>
        <p>Nothing</p>
      </Tabs>,
    )
    expect(screen.getByRole('tablist')).toBeEmptyDOMElement()
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Nothing')
  })
})
