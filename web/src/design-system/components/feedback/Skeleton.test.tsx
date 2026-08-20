import { describe, expect, it } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Skeleton, SkeletonRegion } from './index'

describe('Skeleton — contract', () => {
  it('renders the rail shape as a poster row', () => {
    const { container } = renderComponent(<Skeleton shape="rail" count={6} />)
    expect(container.querySelector('.u-skel-rail')).not.toBeNull()
    expect(container.querySelectorAll('.u-skel--poster')).toHaveLength(6)
  })

  it('renders the table shape as operator rows', () => {
    const { container } = renderComponent(<Skeleton shape="table" count={8} />)
    expect(container.querySelector('.u-skel-table')).not.toBeNull()
    expect(container.querySelectorAll('.u-skel-table__row')).toHaveLength(8)
  })

  it('renders the hero shape as a poster beside stacked lines', () => {
    const { container } = renderComponent(<Skeleton shape="hero" />)
    expect(container.querySelector('.u-skel-hero')).not.toBeNull()
    expect(container.querySelector('.u-skel-hero__poster')).not.toBeNull()
  })

  it('renders the text shape with the last line short', () => {
    const { container } = renderComponent(<Skeleton shape="text" lines={4} />)
    const lines = container.querySelectorAll<HTMLElement>('.u-skel--text')
    expect(lines).toHaveLength(4)
    expect(lines[3]?.style.width).toBe('62%')
    expect(lines[0]?.style.width).toBe('100%')
  })

  it('renders the block shape at the size it was given', () => {
    const { container } = renderComponent(<Skeleton shape="block" width={240} height={34} />)
    const block = container.querySelector<HTMLElement>('.u-skel')
    expect(block?.style.width).toBe('240px')
    expect(block?.style.height).toBe('34px')
  })

  it('takes the minimum height when the real height is unknown', () => {
    // Never the maximum: a skeleton that shrinks when content lands moves the page under the reader.
    const { container } = renderComponent(<Skeleton shape="block" />)
    expect(container.querySelector<HTMLElement>('.u-skel')?.style.height).toBe('16px')
  })

  it('carries the shared sweep class on every shape', () => {
    for (const shape of ['text', 'rail', 'table', 'hero', 'block'] as const) {
      const { container, unmount } = renderComponent(<Skeleton shape={shape} />)
      // `.u-skel` owns the one 1400 ms `--dur-shimmer` sweep, dropped by prefers-reduced-motion.
      expect(container.querySelectorAll('.u-skel').length).toBeGreaterThan(0)
      unmount()
    }
  })
})

describe('Skeleton — accessibility (§1, §12)', () => {
  it.each(['text', 'rail', 'table', 'hero', 'block'] as const)(
    'hides the %s shape from assistive tech',
    (shape) => {
      const { container } = renderComponent(<Skeleton shape={shape} />)
      expect(container.firstElementChild).toHaveAttribute('aria-hidden', 'true')
    },
  )

  it('gives the owning region aria-busy and a visually-hidden label', () => {
    const { container } = renderComponent(
      <SkeletonRegion busy label="Loading browse results …">
        <Skeleton shape="table" count={8} />
      </SkeletonRegion>,
    )
    expect(container.firstElementChild).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByText('Loading browse results …')).toHaveClass('u-visually-hidden')
  })

  it('defaults the label to "Loading …"', () => {
    renderComponent(
      <SkeletonRegion busy>
        <Skeleton shape="rail" />
      </SkeletonRegion>,
    )
    expect(screen.getByText('Loading …')).toBeInTheDocument()
  })

  it('drops both the busy flag and the label once the content lands', () => {
    const { container } = renderComponent(
      <SkeletonRegion busy={false}>
        <p>Stalker, 1979</p>
      </SkeletonRegion>,
    )
    expect(container.firstElementChild).not.toHaveAttribute('aria-busy')
    expect(screen.queryByText('Loading …')).not.toBeInTheDocument()
  })

  it('has no axe violations', async () => {
    const { container } = renderComponent(
      <SkeletonRegion busy label="Loading the queue …">
        <Skeleton shape="table" count={4} />
      </SkeletonRegion>,
      { theme: 'light', density: 'compact' },
    )
    await expectNoViolations(container)
  })
})

describe('Skeleton — anti-patterns', () => {
  it('is never a spinner: no progressbar, no status role', () => {
    const { container } = renderComponent(<Skeleton shape="rail" count={6} />)
    expect(container.querySelector('[role="progressbar"]')).toBeNull()
    expect(container.querySelector('[role="status"]')).toBeNull()
  })
})
