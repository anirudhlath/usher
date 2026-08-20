import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Icon } from '../icon'
import { Toast, ToastStack } from './index'

const JOB_KEY = 'sync:full:0191f4c2-8a7e-7c31-b0d9-2f6a1e4c8b55'

function receipt() {
  return (
    <ToastStack>
      <Toast
        tone="info"
        icon={<Icon name="git-branch" size={16} />}
        title="Queued a full sync of Living Room"
        jobKey={JOB_KEY}
        coalesced
        action={
          <a className="u-link" href="/console/pipeline">
            Watch it on Pipeline
          </a>
        }
        onDismiss={() => {}}
      >
        A full walk of the library. 41 minutes last time.
      </Toast>
    </ToastStack>
  )
}

afterEach(() => {
  vi.useRealTimers()
})

describe('Toast — contract', () => {
  it('renders the title, the body, the key, the coalescing sentence and the destination', () => {
    renderComponent(receipt())
    expect(screen.getByText('Queued a full sync of Living Room')).toBeInTheDocument()
    expect(screen.getByText('A full walk of the library. 41 minutes last time.')).toBeInTheDocument()
    expect(screen.getByText(`key ${JOB_KEY}`)).toBeInTheDocument()
    expect(
      screen.getByText('It coalesced with a job already running — nothing new was started.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Watch it on Pipeline' })).toHaveAttribute(
      'href',
      '/console/pipeline',
    )
  })

  it.each([
    ['info', 'u-toast__icon--info'],
    ['good', 'u-toast__icon--good'],
    ['warn', 'u-toast__icon--warn'],
    ['bad', 'u-toast__icon--bad'],
  ] as const)('tones the icon for %s', (tone, expected) => {
    const { container } = renderComponent(
      <Toast tone={tone} title="Queued the aliases phase" icon={<Icon name="database" size={16} />} />,
    )
    expect(container.querySelector(`.${expected}`)).not.toBeNull()
  })

  it('says nothing about coalescing when the server did not say', () => {
    renderComponent(<Toast title="Queued the aliases phase" jobKey="bootstrap:aliases" />)
    expect(screen.queryByText(/coalesced/i)).not.toBeInTheDocument()
  })

  it('renders the stack as a labelled region', () => {
    renderComponent(receipt())
    expect(screen.getByRole('region', { name: 'Notifications' })).toBeInTheDocument()
  })
})

describe('Toast — behaviour', () => {
  it('dismisses from a labelled control', async () => {
    const onDismiss = vi.fn<() => void>()
    const { user } = renderComponent(
      <Toast title="Queued a full sync of Living Room" onDismiss={onDismiss} />,
    )
    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('has no dismiss control when the caller supplied no handler', () => {
    renderComponent(<Toast title="Queued a full sync of Living Room" />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})

describe('Toast — accessibility (§6, §12)', () => {
  it('announces politely, never assertively', () => {
    const { container } = renderComponent(receipt())
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(container.innerHTML).not.toContain('assertive')
  })

  it('has no axe violations', async () => {
    const { container } = renderComponent(receipt())
    await expectNoViolations(container)
  })

  it('has no axe violations on the operator side (light, compact)', async () => {
    const { container } = renderComponent(receipt(), { theme: 'light', density: 'compact' })
    await expectNoViolations(container)
  })
})

describe('Toast — the 202 idiom', () => {
  it('says "Queued", not "Done" and not "Saved"', () => {
    const { container } = renderComponent(receipt())
    expect(screen.getByText(/^Queued /)).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/\bDone\b/)
    expect(container.textContent).not.toMatch(/\bSaved\b/)
  })

  it('does not auto-dismiss — a receipt persists until it is dismissed', () => {
    vi.useFakeTimers()
    const onDismiss = vi.fn<() => void>()
    renderComponent(receipt())

    act(() => {
      vi.advanceTimersByTime(60_000)
    })

    expect(screen.getByText('Queued a full sync of Living Room')).toBeInTheDocument()
    expect(screen.getByText(`key ${JOB_KEY}`)).toBeInTheDocument()
    expect(onDismiss).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('prints the key as selectable text — nothing can look it up, so it must be pasteable', () => {
    renderComponent(receipt())
    const key = screen.getByText(`key ${JOB_KEY}`)

    // Selectable means: real text, not inside a control, not hidden, and nothing on the way up
    // to the root turns selection off.
    expect(key.textContent).toContain(JOB_KEY)
    expect(key.closest('button')).toBeNull()
    for (let node: HTMLElement | null = key; node; node = node.parentElement) {
      expect(node.getAttribute('aria-hidden')).not.toBe('true')
      expect(node.style.userSelect).not.toBe('none')
    }
  })
})
