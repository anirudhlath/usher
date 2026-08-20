import { describe, expect, it } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { ProgressBar } from './index'

function fillIn(container: HTMLElement): HTMLElement {
  const found = container.querySelector('.u-progress__fill')
  if (!(found instanceof HTMLElement)) throw new Error('expected a progress fill to be rendered')
  return found
}

describe('ProgressBar', () => {
  describe('with a denominator', () => {
    it('fills to the measured share and names both numbers', () => {
      const { container } = renderComponent(<ProgressBar positionSeconds={4100} runtimeSeconds={9660} />)
      const bar = screen.getByRole('progressbar')

      expect(bar).toHaveAttribute('aria-valuenow', '42')
      expect(bar).toHaveAttribute('aria-valuemin', '0')
      expect(bar).toHaveAttribute('aria-valuemax', '100')
      expect(bar).toHaveAttribute('aria-valuetext', '68 of 161 min watched')
      expect(fillIn(container).style.width).toMatch(/^42\.44/)
    })

    it('clamps a position past the runtime rather than overflowing the track', () => {
      const { container } = renderComponent(<ProgressBar positionSeconds={9999} runtimeSeconds={100} />)

      expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
      expect(fillIn(container).style.width).toBe('100%')
    })

    it('is green and reads "Watched" once played', () => {
      const { container } = renderComponent(
        <ProgressBar positionSeconds={9660} runtimeSeconds={9660} played />,
      )

      expect(container.querySelector('.u-progress')).toHaveClass('u-progress--played')
      expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', 'Watched')
      expect(fillIn(container).style.width).toBe('100%')
    })
  })

  describe('without a denominator (patterns.md §12)', () => {
    it('omits aria-valuenow and puts the reason in words', () => {
      const bar = renderAndGet(<ProgressBar positionSeconds={600} runtimeSeconds={null} />)

      expect(bar).not.toHaveAttribute('aria-valuenow')
      expect(bar).toHaveAttribute('aria-valuetext', 'Progress unknown — no runtime on record')
    })

    it('treats an absent runtime the same as an explicit null', () => {
      const bar = renderAndGet(<ProgressBar positionSeconds={600} />)

      expect(bar).not.toHaveAttribute('aria-valuenow')
      expect(bar).toHaveAttribute('aria-valuetext', 'Progress unknown — no runtime on record')
    })

    it('treats a zero runtime as no denominator, never as a division', () => {
      const { container } = renderComponent(<ProgressBar positionSeconds={600} runtimeSeconds={0} />)

      expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow')
      expect(fillIn(container).style.width).toBe('0%')
    })

    it('still omits aria-valuenow when played is the only fact on record', () => {
      const { container } = renderComponent(<ProgressBar played />)
      const bar = screen.getByRole('progressbar')

      expect(bar).not.toHaveAttribute('aria-valuenow')
      expect(bar).toHaveAttribute('aria-valuetext', 'Watched')
      expect(fillIn(container).style.width).toBe('100%')
    })
  })

  it('takes a caller-supplied label verbatim', () => {
    renderAndGet(<ProgressBar positionSeconds={60} runtimeSeconds={120} label="Halfway through the pilot" />)
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuetext', 'Halfway through the pilot')
  })

  describe('accessibility', () => {
    it('has no violations, denominated or not', async () => {
      const { container } = renderComponent(
        <>
          <ProgressBar positionSeconds={4100} runtimeSeconds={9660} />
          <ProgressBar positionSeconds={600} />
          <ProgressBar played />
        </>,
      )
      await expectNoViolations(container)
    })

    it('carries an accessible name, not only a value', () => {
      renderComponent(<ProgressBar positionSeconds={4100} runtimeSeconds={9660} />)
      expect(screen.getByRole('progressbar', { name: '68 of 161 min watched' })).toBeInTheDocument()
    })
  })

  describe('anti-patterns', () => {
    it('never prints a percentage label on the card — the bar is the whole idiom', () => {
      const { container } = renderComponent(<ProgressBar positionSeconds={4100} runtimeSeconds={9660} />)
      expect(container.textContent).toBe('')
    })

    it('does not animate its width — a bar that reports state does not ease', () => {
      const { container } = renderComponent(<ProgressBar positionSeconds={4100} runtimeSeconds={9660} />)
      const fill = fillIn(container)

      expect(fill.style.transition).toBe('')
      expect(fill.style.animation).toBe('')
    })
  })
})

function renderAndGet(ui: Parameters<typeof renderComponent>[0]): HTMLElement {
  renderComponent(ui)
  return screen.getByRole('progressbar')
}
