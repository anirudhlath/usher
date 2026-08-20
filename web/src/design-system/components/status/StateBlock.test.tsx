import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Icon } from '../icon'
import { StateBlock } from './index'

/** The four facts of patterns.md §2, each rendered by the same component with a different treatment. */
const FOUR = (
  <>
    <StateBlock kind="never" meta="computed_at: null">
      We have never computed similar titles for this one.
    </StateBlock>
    <StateBlock kind="empty" meta="neighbors: [] · computed 3 days ago">
      Nothing scored close enough to show.
    </StateBlock>
    <StateBlock kind="stale" meta="stale: true">
      Computed before the scoring blend changed. Shown as they were.
    </StateBlock>
    <StateBlock kind="na">Collections are films only.</StateBlock>
  </>
)

describe('StateBlock — contract', () => {
  it('defaults to the computed-and-empty kind', () => {
    const { container } = renderComponent(<StateBlock>Nothing scored close enough to show.</StateBlock>)
    expect(container.querySelector('.u-state')).toHaveClass('u-state--empty')
  })

  it.each([
    ['never', 'u-state--never', 'Never computed'],
    ['empty', 'u-state--empty', 'Computed, and empty'],
    ['stale', 'u-state--stale', 'Stale'],
  ] as const)('renders kind %s with %s and its own heading', (kind, className, heading) => {
    const { container } = renderComponent(<StateBlock kind={kind}>body</StateBlock>)
    const block = container.querySelector('.u-state')
    expect(block).toHaveClass(className)
    expect(block).toHaveTextContent(heading)
  })

  it('lets `title` override the default heading', () => {
    renderComponent(
      <StateBlock kind="never" title="Facets were not requested.">
        Counts are only computed once a filter is set.
      </StateBlock>,
    )
    expect(screen.getByText('Facets were not requested.')).toBeInTheDocument()
  })

  it('renders `meta` — the field that proves the claim — in the mono line', () => {
    renderComponent(
      <StateBlock kind="never" meta="computed_at: null">
        body
      </StateBlock>,
    )
    const meta = screen.getByText('computed_at: null')
    expect(meta).toHaveClass('u-state__meta')
  })

  it('renders a caller-supplied icon and action', () => {
    const { container } = renderComponent(
      <StateBlock
        kind="stale"
        icon={<Icon name="database" />}
        action={<button type="button">Recompute</button>}
      >
        body
      </StateBlock>,
    )
    expect(container.querySelector('[data-icon="database"]')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Recompute' })).toBeInTheDocument()
  })
})

describe('StateBlock — the four kinds are distinguishable', () => {
  it('gives each kind its own class, so the four treatments cannot collapse into one', () => {
    const { container } = renderComponent(FOUR)
    const classes = [...container.querySelectorAll('.u-state')].map((element) =>
      [...element.classList].find((name) => name.startsWith('u-state--')),
    )
    expect(classes).toEqual(['u-state--never', 'u-state--empty', 'u-state--stale', 'u-state--na'])
    expect(new Set(classes).size).toBe(4)
  })

  it('says a different sentence for each kind', () => {
    renderComponent(FOUR)
    expect(screen.getByText('Never computed')).toBeInTheDocument()
    expect(screen.getByText('Computed, and empty')).toBeInTheDocument()
    expect(screen.getByText('Stale')).toBeInTheDocument()
    expect(screen.getByText(/Collections are films only\./)).toBeInTheDocument()
  })

  it('renders `na` as the inline em-dash form, not a block', () => {
    const { container } = renderComponent(<StateBlock kind="na">Collections are films only.</StateBlock>)
    const block = container.querySelector('.u-state')
    expect(block?.tagName).toBe('SPAN')
    expect(block).toHaveClass('u-state--na')
    expect(block).toHaveTextContent('— Collections are films only.')
    // No heading, no border treatment, and not announced: it is one clause, not a block.
    expect(screen.queryByRole('status')).toBeNull()
    expect(block?.querySelector('.u-state__head')).toBeNull()
  })

  it.each(['never', 'empty', 'stale'] as const)('renders kind %s as an announced block', (kind) => {
    renderComponent(<StateBlock kind={kind}>body</StateBlock>)
    expect(screen.getByRole('status')).toHaveClass(`u-state--${kind}`)
  })

  it('shows stale content rather than suppressing it', () => {
    renderComponent(
      <StateBlock kind="stale" meta="stale: true">
        Computed before the scoring blend changed. Shown as they were.
      </StateBlock>,
    )
    expect(screen.getByText(/Shown as they were\./)).toBeInTheDocument()
    expect(screen.getByText('stale: true')).toBeInTheDocument()
  })
})

describe('StateBlock — accessibility (§12: no colour-only encoding)', () => {
  it('supplies the fixed circle-dashed glyph for never', () => {
    const { container } = renderComponent(<StateBlock kind="never">body</StateBlock>)
    expect(container.querySelector('[data-icon="circle-dashed"]')).not.toBeNull()
  })

  it('supplies the fixed history glyph for stale', () => {
    const { container } = renderComponent(<StateBlock kind="stale">body</StateBlock>)
    expect(container.querySelector('[data-icon="history"]')).not.toBeNull()
  })

  it('has no axe violations across all four kinds', async () => {
    const { container } = renderComponent(FOUR)
    await expectNoViolations(container)
  })

  it('has no axe violations in compact density', async () => {
    const { container } = renderComponent(FOUR, { theme: 'light', density: 'compact' })
    await expectNoViolations(container)
  })
})

describe('StateBlock — anti-patterns', () => {
  it('never substitutes a bare grey dash for a computed state', () => {
    const { container } = renderComponent(FOUR)
    const blocks = [...container.querySelectorAll('.u-state')]
    const blockKinds = blocks.filter((element) => !element.classList.contains('u-state--na'))
    for (const block of blockKinds) {
      expect(block.textContent?.trim()).not.toBe('—')
      expect(block.textContent?.trim().length).toBeGreaterThan(1)
    }
  })

  it('keeps `meta` when it is given — it is not droppable to reduce clutter', () => {
    const { rerender } = renderComponent(
      <StateBlock kind="empty" meta="neighbors: [] · computed 3 days ago">
        Nothing scored close enough to show.
      </StateBlock>,
    )
    expect(screen.getByText('neighbors: [] · computed 3 days ago')).toBeInTheDocument()
    rerender(
      <StateBlock kind="empty" meta="neighbors: [] · computed 3 days ago" action={<span>anything</span>}>
        Nothing scored close enough to show.
      </StateBlock>,
    )
    expect(screen.getByText('neighbors: [] · computed 3 days ago')).toBeInTheDocument()
  })

  it('has no code path that renders a kind it was not given', () => {
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container } = renderComponent(<StateBlock kind="never">body</StateBlock>)
    const block = container.querySelector('.u-state')
    expect(block).not.toHaveClass('u-state--empty')
    expect(block).not.toHaveClass('u-state--stale')
    expect(block).not.toHaveClass('u-state--na')
    warn.mockRestore()
  })
})
