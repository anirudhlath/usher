import { describe, expect, it } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Icon } from '../icon'
import { Badge } from './index'

describe('Badge — contract', () => {
  it.each([
    ['neutral', 'u-badge--neutral'],
    ['good', 'u-badge--good'],
    ['warn', 'u-badge--warn'],
    ['bad', 'u-badge--bad'],
    ['info', 'u-badge--info'],
  ] as const)('renders tone %s as %s', (tone, expected) => {
    renderComponent(<Badge tone={tone}>{tone}</Badge>)
    expect(screen.getByText(tone)).toHaveClass('u-badge', expected)
  })

  it.each([
    ['skeleton', 'u-badge--skeleton'],
    ['stub', 'u-badge--stub'],
    ['enriched', 'u-badge--enriched'],
    ['failed', 'u-badge--failed'],
  ] as const)('renders tier %s as %s', (tier, expected) => {
    renderComponent(<Badge tier={tier}>{tier}</Badge>)
    expect(screen.getByText(tier)).toHaveClass('u-badge', expected)
  })

  it('lets tier override tone, as the contract says', () => {
    renderComponent(
      <Badge tone="good" tier="failed">
        failed
      </Badge>,
    )
    const badge = screen.getByText('failed')
    expect(badge).toHaveClass('u-badge--failed')
    expect(badge).not.toHaveClass('u-badge--good')
  })

  it('carries a composed technical fact in mono, outlined', () => {
    renderComponent(
      <Badge mono outline>
        2160p · HDR10 · HEVC · MKV
      </Badge>,
    )
    const badge = screen.getByText('2160p · HDR10 · HEVC · MKV')
    expect(badge).toHaveClass('u-badge', 'u-badge--neutral', 'u-badge--mono', 'u-badge--outline')
  })

  it('renders a caller-supplied icon rather than the default one', () => {
    const { container } = renderComponent(
      <Badge tone="good" icon={<Icon name="database" />}>
        owned
      </Badge>,
    )
    expect(container.querySelector('[data-icon="database"]')).not.toBeNull()
    expect(container.querySelector('[data-icon="check-circle"]')).toBeNull()
  })
})

describe('Badge — accessibility (§12: no colour-only encoding)', () => {
  it.each([
    ['good', 'check-circle'],
    ['warn', 'alert-triangle'],
    ['bad', 'x-circle'],
    ['info', 'info'],
  ] as const)('gives tone %s the fixed %s glyph even when the call site omits it', (tone, glyph) => {
    const { container } = renderComponent(<Badge tone={tone}>{tone}</Badge>)
    expect(container.querySelector(`[data-icon="${glyph}"]`)).not.toBeNull()
  })

  it('has a word as well as a hue in every state', () => {
    const { container } = renderComponent(
      <>
        <Badge tone="good">owned</Badge>
        <Badge tone="warn">missing</Badge>
        <Badge tone="bad">parked</Badge>
        <Badge tone="info">fused</Badge>
        <Badge tier="skeleton">skeleton</Badge>
      </>,
    )
    for (const badge of container.querySelectorAll('.u-badge')) {
      expect(badge.textContent?.trim()).not.toBe('')
    }
  })

  it('does not put a state glyph on a tier, which carries its own word', () => {
    const { container } = renderComponent(<Badge tier="failed">failed</Badge>)
    expect(container.querySelector('[data-icon]')).toBeNull()
  })

  it('keeps its glyph out of the accessible name — the word is the carrier', () => {
    const { container } = renderComponent(<Badge tone="warn">missing</Badge>)
    const icon = container.querySelector('[data-icon="alert-triangle"]')
    expect(icon).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByText('missing')).toHaveTextContent(/^missing$/)
  })

  it('has no axe violations in the operator default (light, compact)', async () => {
    const { container } = renderComponent(
      <>
        <Badge tier="skeleton">skeleton</Badge>
        <Badge tone="warn">missing</Badge>
      </>,
      { theme: 'light', density: 'compact' },
    )
    expect(container.querySelector('[data-icon="alert-triangle"]')).not.toBeNull()
    await expectNoViolations(container)
  })

  it('has no axe violations', async () => {
    const { container } = renderComponent(
      <>
        <Badge tone="good">owned</Badge>
        <Badge mono outline>
          2160p · HDR10 · HEVC · MKV
        </Badge>
      </>,
    )
    await expectNoViolations(container)
  })
})

describe('Badge — anti-patterns', () => {
  it('is never a bare coloured dot: a tone badge always renders its children', () => {
    renderComponent(<Badge tone="bad">parked</Badge>)
    expect(screen.getByText('parked')).toBeInTheDocument()
  })
})
