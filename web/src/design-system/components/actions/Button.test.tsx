import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Icon } from '../icon'
import { Button } from '.'

const VARIANTS = ['primary', 'secondary', 'ghost', 'danger', 'danger-solid'] as const

describe('Button — contract', () => {
  it.each(VARIANTS)('renders exactly the classes actions.css styles for %s', (variant) => {
    renderComponent(<Button variant={variant}>Play</Button>)
    // The whole class list, not a containment check: an extra class is how a teal
    // button, or a variant the contract does not have, would arrive unnoticed.
    expect(screen.getByRole('button', { name: 'Play' }).className.split(' ')).toEqual([
      'u-btn',
      `u-btn--${variant}`,
    ])
  })

  it('defaults to the outlined secondary treatment', () => {
    renderComponent(<Button>Probe source</Button>)
    expect(screen.getByRole('button', { name: 'Probe source' })).toHaveClass('u-btn--secondary')
  })

  it.each([
    ['sm', 'u-btn--sm'],
    ['lg', 'u-btn--lg'],
  ] as const)('renders the %s size modifier', (size, expected) => {
    renderComponent(<Button size={size}>Small</Button>)
    expect(screen.getByRole('button', { name: 'Small' })).toHaveClass(expected)
  })

  it('emits no size modifier at md, which is the CSS default', () => {
    renderComponent(<Button size="md">Medium</Button>)
    const button = screen.getByRole('button', { name: 'Medium' })
    expect(button.className).not.toMatch(/u-btn--(sm|md|lg)/)
  })

  it('renders the block modifier', () => {
    renderComponent(<Button block>Full width</Button>)
    expect(screen.getByRole('button', { name: 'Full width' })).toHaveClass('u-btn--block')
  })

  it('renders a leading and a trailing icon', () => {
    const { container } = renderComponent(
      <Button iconLeft={<Icon name="refresh-cw" />} iconRight={<Icon name="chevron-right" />}>
        Probe source
      </Button>,
    )
    expect(container.querySelector('[data-icon="refresh-cw"]')).not.toBeNull()
    expect(container.querySelector('[data-icon="chevron-right"]')).not.toBeNull()
  })

  it('spreads the rest of the native props onto the root and merges className', () => {
    renderComponent(
      <Button id="play" data-testid="play" aria-describedby="hint" className="mt-2" type="submit">
        Play
      </Button>,
    )
    const button = screen.getByRole('button', { name: 'Play' })
    expect(button).toHaveAttribute('id', 'play')
    expect(button).toHaveAttribute('data-testid', 'play')
    expect(button).toHaveAttribute('aria-describedby', 'hint')
    expect(button).toHaveAttribute('type', 'submit')
    expect(button).toHaveClass('u-btn', 'mt-2')
  })

  it('renders the same markup in both densities — nothing branches on density', () => {
    const comfortable = renderComponent(<Button variant="primary">Play</Button>)
    const comfortableHtml = comfortable.container.innerHTML
    comfortable.unmount()

    const compact = renderComponent(<Button variant="primary">Play</Button>, { density: 'compact' })
    expect(compact.container.innerHTML).toBe(comfortableHtml)
  })
})

describe('Button — loading (patterns.md §1)', () => {
  it('shows a spinner, sets aria-busy and marks the label pending', () => {
    const { container } = renderComponent(
      <Button variant="primary" loading loadingLabel="Finding copies…">
        Play
      </Button>,
    )
    const button = screen.getByRole('button', { name: 'Finding copies…' })
    expect(button).toHaveAttribute('aria-busy', 'true')
    const spinner = container.querySelector('.u-btn__spinner')
    expect(spinner).not.toBeNull()
    expect(spinner).toHaveAttribute('aria-hidden', 'true')
    expect(button.querySelector('.u-btn__pending')).toHaveTextContent('Finding copies…')
  })

  it('replaces the label rather than appending to it', () => {
    renderComponent(
      <Button loading loadingLabel="Finding copies…">
        Play
      </Button>,
    )
    // One pending action says so where it was clicked; "Play Finding copies…" is
    // two labels for one control.
    expect(screen.getByRole('button')).toHaveAccessibleName('Finding copies…')
    expect(screen.queryByRole('button', { name: 'Play' })).toBeNull()
  })

  it('keeps the resting label when no loadingLabel is given', () => {
    renderComponent(<Button loading>Play</Button>)
    const button = screen.getByRole('button', { name: 'Play' })
    expect(button).toHaveAttribute('aria-busy', 'true')
  })

  it('keeps the control in its box: the label slot survives and the spinner takes the icon slot', () => {
    const { container } = renderComponent(
      <Button loading iconLeft={<Icon name="play" />} iconRight={<Icon name="chevron-right" />}>
        Play
      </Button>,
    )
    // Not a bare spinner: the label is still rendered, so the button keeps its
    // width and the row it sits in does not reflow while the action is pending.
    expect(container.querySelector('.u-btn__pending')).toHaveTextContent('Play')
    expect(container.querySelector('.u-btn__spinner')).not.toBeNull()
    expect(container.querySelector('[data-icon="play"]')).toBeNull()
    expect(container.querySelector('[data-icon="chevron-right"]')).toBeNull()
  })

  it('blocks the click while loading', async () => {
    const onClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <Button loading loadingLabel="Finding copies…" onClick={onClick}>
        Play
      </Button>,
    )
    await user.click(screen.getByRole('button'))
    expect(onClick).not.toHaveBeenCalled()
  })

  it('carries no aria-busy when it is not loading', async () => {
    const onClick = vi.fn<() => void>()
    const { user } = renderComponent(<Button onClick={onClick}>Play</Button>)
    const button = screen.getByRole('button', { name: 'Play' })
    expect(button).not.toHaveAttribute('aria-busy')
    await user.click(button)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('blocks the click while disabled', async () => {
    const onClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <Button disabled onClick={onClick}>
        Sync (source disabled)
      </Button>,
    )
    const button = screen.getByRole('button', { name: 'Sync (source disabled)' })
    expect(button).toBeDisabled()
    await user.click(button)
    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('Button — as="a"', () => {
  it('renders a real anchor with an href, not a button', () => {
    renderComponent(
      <Button as="a" href="/console/search" variant="secondary">
        Search instead
      </Button>,
    )
    const link = screen.getByRole('link', { name: 'Search instead' })
    expect(link.tagName).toBe('A')
    expect(link).toHaveAttribute('href', '/console/search')
    expect(link).toHaveClass('u-btn', 'u-btn--secondary')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('passes target and rel through, which the anchor form exists for', () => {
    renderComponent(
      <Button as="a" href="/grafana/" target="_blank" rel="noreferrer noopener" size="sm">
        Open Grafana
      </Button>,
    )
    const link = screen.getByRole('link', { name: 'Open Grafana' })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer noopener')
  })

  it('never carries the disabled attribute — an anchor cannot be disabled', async () => {
    const onClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <Button as="a" href="#nowhere" disabled onClick={onClick}>
        Documentation
      </Button>,
    )
    const link = screen.getByRole('link', { name: 'Documentation' })
    expect(link).not.toHaveAttribute('disabled')
    expect(link).toHaveAttribute('aria-disabled', 'true')
    await user.click(link)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('suppresses the navigation itself while inert, not just the handler', async () => {
    const defaultPrevented: boolean[] = []
    const spy = (event: MouseEvent) => defaultPrevented.push(event.defaultPrevented)
    document.addEventListener('click', spy)
    try {
      const { user } = renderComponent(
        <Button as="a" href="#nowhere" loading loadingLabel="Finding copies…">
          Play
        </Button>,
      )
      const link = screen.getByRole('link', { name: 'Finding copies…' })
      expect(link).toHaveAttribute('aria-busy', 'true')
      expect(link).toHaveAttribute('aria-disabled', 'true')
      await user.click(link)
      expect(defaultPrevented).toEqual([true])
    } finally {
      document.removeEventListener('click', spy)
    }
  })

  it('runs the handler and lets the navigation stand when it is not inert', async () => {
    const onClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <Button as="a" href="#somewhere" onClick={onClick}>
        Developer drawer
      </Button>,
    )
    await user.click(screen.getByRole('link', { name: 'Developer drawer' }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})

describe('Button — accessibility (patterns.md §12)', () => {
  it('has no axe violations across the specimen sheet', async () => {
    const { container } = renderComponent(
      <div>
        <Button variant="primary">Play</Button>
        <Button variant="secondary" iconLeft={<Icon name="refresh-cw" />}>
          Probe source
        </Button>
        <Button variant="ghost">Skip</Button>
        <Button variant="danger">Release job</Button>
        <Button variant="danger-solid">Delete source</Button>
        <Button size="lg" variant="primary" loading loadingLabel="Finding copies…">
          Play
        </Button>
        <Button variant="secondary" disabled>
          Sync (source disabled)
        </Button>
        <Button as="a" href="/console/about" variant="ghost">
          Documentation
        </Button>
      </div>,
    )
    // The premise first: axe over an empty container passes too.
    expect(screen.getAllByRole('button')).toHaveLength(7)
    expect(screen.getAllByRole('link')).toHaveLength(1)
    await expectNoViolations(container)
  })

  it('keeps a disabled control named and in the accessibility tree', () => {
    renderComponent(<Button disabled>Sync (source disabled)</Button>)
    // §12: --text-disabled may only appear on a control that is also disabled to
    // assistive tech, and the reason lives in the label — not in the colour.
    expect(screen.getByRole('button', { name: 'Sync (source disabled)' })).toBeDisabled()
  })
})
