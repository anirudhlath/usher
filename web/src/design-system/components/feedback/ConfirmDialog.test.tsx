import { useState, type ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { ConfirmDialog, NOT_MEASURED, type ConfirmFact } from './index'

const IMDB_FACTS: ConfirmFact[] = [
  { label: 'downloads', value: '~224 MB from IMDb (regenerated daily)' },
  { label: 'measured', value: '2 h 40 m on a cold run' },
  { label: 'writes', value: 'title skeletons, ~1.27M rows' },
  { label: 'resumable', value: 'yes — from the stored cursor' },
]

function bootstrapDialog(overrides: Partial<ComponentProps<typeof ConfirmDialog>> = {}) {
  return (
    <ConfirmDialog
      open
      title="Run the IMDb bootstrap phase?"
      facts={IMDB_FACTS}
      confirmLabel="Start import"
      onConfirm={() => {}}
      onCancel={() => {}}
      {...overrides}
    >
      This must run before credit-names, aliases, tmdb-ids, crosswalk and movielens. Later phases will refuse
      until it completes.
    </ConfirmDialog>
  )
}

/** A real trigger, so focus has somewhere honest to return to. */
function Harness({ requireTyped }: { requireTyped?: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Delete this source
      </button>
      <ConfirmDialog
        open={open}
        title="Delete Living Room?"
        destructive
        confirmLabel="Delete source"
        facts={[{ label: 'measured', value: NOT_MEASURED }]}
        onCancel={() => setOpen(false)}
        onConfirm={() => setOpen(false)}
        {...(requireTyped === undefined ? {} : { requireTyped })}
      >
        Watch state survives. Availability does not.
      </ConfirmDialog>
    </div>
  )
}

describe('ConfirmDialog — contract', () => {
  it('renders nothing when closed', () => {
    const { container } = renderComponent(
      <ConfirmDialog open={false} title="Run the IMDb bootstrap phase?" />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('renders the four facts as a key/value grid', () => {
    renderComponent(bootstrapDialog())
    for (const fact of IMDB_FACTS) {
      expect(screen.getByText(fact.label)).toBeInTheDocument()
      expect(screen.getByText(fact.value)).toBeInTheDocument()
    }
  })

  it('says a duration was not measured rather than inventing a range', () => {
    const { container } = renderComponent(
      bootstrapDialog({ facts: [{ label: 'measured', value: NOT_MEASURED }] }),
    )
    const value = screen.getByText(NOT_MEASURED)
    expect(value).toBeInTheDocument()
    // Prose, not a measurement: §14 keeps mono for identifiers and measurements only.
    expect(value).toHaveClass('u-dialog__v--unmeasured')
    expect(container.textContent).not.toMatch(/\d+\s*[–-]\s*\d+/)
  })

  it('uses the red solid confirm only for destruction', () => {
    const { container } = renderComponent(bootstrapDialog({ destructive: true }))
    expect(container.querySelector('.u-btn--danger-solid')).not.toBeNull()
  })

  it('keeps an expensive-but-safe action on primary — an import is not a deletion', () => {
    const { container } = renderComponent(bootstrapDialog())
    const confirm = screen.getByRole('button', { name: 'Start import' })
    expect(confirm).toHaveClass('u-btn--primary')
    expect(container.querySelector('.u-btn--danger-solid')).toBeNull()
  })

  it('disables the confirm while the action is in flight', () => {
    renderComponent(bootstrapDialog({ loading: true }))
    expect(screen.getByRole('button', { name: 'Start import' })).toBeDisabled()
  })
})

describe('ConfirmDialog — behaviour (§5)', () => {
  it('lands focus on the confirm button', () => {
    renderComponent(bootstrapDialog())
    expect(screen.getByRole('button', { name: 'Start import' })).toHaveFocus()
  })

  it('cancels on Esc', async () => {
    const onCancel = vi.fn<() => void>()
    const { user } = renderComponent(bootstrapDialog({ onCancel }))
    await user.keyboard('{Escape}')
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('cancels on a scrim click but not on a click inside the dialog', async () => {
    const onCancel = vi.fn<() => void>()
    const { container, user } = renderComponent(bootstrapDialog({ onCancel }))
    await user.click(screen.getByRole('heading', { name: 'Run the IMDb bootstrap phase?' }))
    expect(onCancel).not.toHaveBeenCalled()

    const scrim = container.querySelector('.u-scrim')
    expect(scrim).not.toBeNull()
    if (scrim) await user.click(scrim)
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('traps Tab inside the dialog', async () => {
    const { user } = renderComponent(bootstrapDialog())
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    const confirm = screen.getByRole('button', { name: 'Start import' })

    expect(confirm).toHaveFocus()
    await user.tab()
    expect(cancel).toHaveFocus()
    await user.tab({ shift: true })
    expect(confirm).toHaveFocus()
  })

  it('returns focus to the trigger when it closes', async () => {
    const { user } = renderComponent(<Harness />)
    const trigger = screen.getByRole('button', { name: 'Delete this source' })
    await user.click(trigger)

    const confirm = screen.getByRole('dialog').querySelector('.u-btn--danger-solid')
    expect(confirm).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })
})

describe('ConfirmDialog — requireTyped', () => {
  it('keeps the confirm disabled until the exact source name is typed', async () => {
    const { user } = renderComponent(<Harness requireTyped="Living Room" />)
    await user.click(screen.getByRole('button', { name: 'Delete this source' }))

    const confirm = screen.getByRole('dialog').querySelector('.u-btn--danger-solid')
    expect(confirm).toBeDisabled()

    const field = screen.getByRole('textbox', { name: /type living room to confirm/i })
    await user.type(field, 'Living')
    expect(confirm).toBeDisabled()

    await user.type(field, ' room')
    expect(confirm).toBeDisabled()

    await user.clear(field)
    await user.type(field, 'Living Room')
    expect(confirm).toBeEnabled()
  })
})

describe('ConfirmDialog — accessibility (§5, §12)', () => {
  it('is a modal dialog with a labelled heading', () => {
    renderComponent(bootstrapDialog())
    const dialog = screen.getByRole('dialog', { name: 'Run the IMDb bootstrap phase?' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })

  it('has no axe violations', async () => {
    const { container } = renderComponent(bootstrapDialog())
    await expectNoViolations(container)
  })

  it('has no axe violations with a type-to-confirm field (light, compact)', async () => {
    const { container, user } = renderComponent(<Harness requireTyped="Living Room" />, {
      theme: 'light',
      density: 'compact',
    })
    await user.click(screen.getByRole('button', { name: 'Delete this source' }))
    await expectNoViolations(container)
  })
})

describe('ConfirmDialog — anti-patterns', () => {
  it('never asks "Are you sure"', () => {
    const { container } = renderComponent(bootstrapDialog())
    expect(container.innerHTML.toLowerCase()).not.toContain('are you sure')
  })
})
