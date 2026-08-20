import { describe, expect, it } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { LiveIndicator } from './index'

describe('LiveIndicator — contract', () => {
  it.each([
    ['connected', 'u-live--connected', 'Live'],
    ['idle', 'u-live--idle', 'Live · quiet'],
    ['reconnecting', 'u-live--reconnecting', 'Reconnecting…'],
    ['off', 'u-live--off', 'Not connected'],
  ] as const)('renders state %s as %s with its own word', (state, className, word) => {
    const { container } = renderComponent(<LiveIndicator state={state} />)
    const root = container.querySelector('.u-live')
    expect(root).toHaveClass(className)
    expect(container.querySelector('.u-live__label')).toHaveTextContent(word)
  })

  it('defaults to idle, the normal steady state', () => {
    const { container } = renderComponent(<LiveIndicator />)
    expect(container.querySelector('.u-live')).toHaveClass('u-live--idle')
  })

  it('names the last event time in idle', () => {
    renderComponent(<LiveIndicator state="idle" lastEventAt="14:22" />)
    expect(screen.getByText('nothing has changed since 14:22')).toBeInTheDocument()
  })

  it('omits the "since" clause when no time is known', () => {
    renderComponent(<LiveIndicator state="idle" />)
    expect(screen.getByText('nothing has changed')).toBeInTheDocument()
  })

  it('lets `detail` override the trailing clause', () => {
    renderComponent(<LiveIndicator state="connected" detail="3 frames in the last minute" />)
    expect(screen.getByText('3 frames in the last minute')).toHaveClass('u-live__detail')
  })

  it('replaces the idle clause when `detail` is given', () => {
    renderComponent(<LiveIndicator state="idle" lastEventAt="14:22" detail="resync_required — refetching" />)
    expect(screen.queryByText(/nothing has changed/)).toBeNull()
    expect(screen.getByText('resync_required — refetching')).toBeInTheDocument()
  })
})

describe('LiveIndicator — idle is healthy, not a warning (§7)', () => {
  it('carries no warn treatment in idle', () => {
    const { container } = renderComponent(<LiveIndicator state="idle" lastEventAt="14:22" />)
    const root = container.querySelector('.u-live')
    expect(root?.className).not.toMatch(/warn|error|bad|alert/)
    expect(container.querySelector('[data-icon="alert-triangle"]')).toBeNull()
    expect(container.querySelector('[data-icon="x-circle"]')).toBeNull()
  })

  it('says the same first word as connected, because a quiet stream is a live stream', () => {
    const idle = renderComponent(<LiveIndicator state="idle" />)
    expect(idle.container.textContent).toContain('Live · quiet')
    idle.unmount()
    const connected = renderComponent(<LiveIndicator state="connected" />)
    expect(connected.container.textContent).toContain('Live')
  })

  it('draws idle exactly as the dot token intends, with no reconnecting or off class', () => {
    const { container } = renderComponent(<LiveIndicator state="idle" />)
    const root = container.querySelector('.u-live')
    expect(root).not.toHaveClass('u-live--reconnecting')
    expect(root).not.toHaveClass('u-live--off')
  })
})

describe('LiveIndicator — accessibility (§7/§12: announce only reconnecting and resync)', () => {
  it('announces reconnecting politely', () => {
    renderComponent(<LiveIndicator state="reconnecting" />)
    const region = screen.getByRole('status')
    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region).toHaveTextContent('Reconnecting…')
  })

  it('announces a resync through the detail clause', () => {
    renderComponent(<LiveIndicator state="reconnecting" detail="resync_required — refetching" />)
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting… resync_required — refetching')
  })

  it.each(['connected', 'idle', 'off'] as const)('announces nothing in state %s', (state) => {
    renderComponent(<LiveIndicator state={state} detail="3 frames in the last minute" />)
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('never announces an individual frame', () => {
    renderComponent(<LiveIndicator state="connected" detail="3 frames in the last minute" />)
    const region = screen.getByRole('status')
    expect(region.textContent).toBe('')
    expect(screen.getByText('3 frames in the last minute')).not.toBe(region)
  })

  it('keeps the coloured dot out of the accessible name', () => {
    const { container } = renderComponent(<LiveIndicator state="connected" />)
    expect(container.querySelector('.u-live__dot')).toHaveAttribute('aria-hidden', 'true')
  })

  it('has no axe violations across all four states', async () => {
    const { container } = renderComponent(
      <>
        <LiveIndicator state="connected" detail="3 frames in the last minute" />
        <LiveIndicator state="idle" lastEventAt="14:22" />
        <LiveIndicator state="reconnecting" />
        <LiveIndicator state="off" />
      </>,
    )
    await expectNoViolations(container)
  })

  it('has no axe violations in compact density', async () => {
    const { container } = renderComponent(<LiveIndicator state="idle" lastEventAt="14:22" />, {
      theme: 'light',
      density: 'compact',
    })
    await expectNoViolations(container)
  })
})

describe('LiveIndicator — anti-patterns', () => {
  it('has no counter that ticks and no frame log', () => {
    const { container } = renderComponent(<LiveIndicator state="connected" />)
    expect(container.textContent).toBe('Live')
  })

  it('shows no raw event name unless the caller passes one as detail', () => {
    const { container } = renderComponent(<LiveIndicator state="reconnecting" />)
    expect(container.textContent).not.toContain('resync_required')
  })
})
