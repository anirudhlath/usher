import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { TextLink } from '.'

describe('TextLink — contract', () => {
  it('renders exactly the classes actions.css styles by default', () => {
    renderComponent(<TextLink href="/titles/0191f4c2">Stalker</TextLink>)
    const link = screen.getByRole('link', { name: 'Stalker' })
    expect(link.className.split(' ')).toEqual(['u-link'])
    expect(link).toHaveAttribute('href', '/titles/0191f4c2')
  })

  it('renders the quiet modifier alongside the base class, never instead of it', () => {
    renderComponent(
      <TextLink href="/titles/0191f4c2" quiet>
        emby:4412
      </TextLink>,
    )
    // `u-link--quiet` only softens the resting colour; the underline rules live on
    // `u-link`, and colour alone is not a link affordance.
    expect(screen.getByRole('link', { name: 'emby:4412' }).className.split(' ')).toEqual([
      'u-link',
      'u-link--quiet',
    ])
  })

  it('carries no target or rel when it stays inside the console', () => {
    const { container } = renderComponent(<TextLink href="/titles/0191f4c2">Stalker</TextLink>)
    const link = screen.getByRole('link', { name: 'Stalker' })
    expect(link).not.toHaveAttribute('target')
    expect(link).not.toHaveAttribute('rel')
    expect(container.querySelector('[data-icon="external-link"]')).toBeNull()
    expect(link).toHaveAccessibleName('Stalker')
  })

  it('spreads the rest of the native props onto the root and merges className', () => {
    renderComponent(
      <TextLink href="#x" id="attribution" data-testid="attribution" aria-describedby="hint" className="ml-1">
        Attribution and licensing
      </TextLink>,
    )
    const link = screen.getByRole('link', { name: 'Attribution and licensing' })
    expect(link).toHaveAttribute('id', 'attribution')
    expect(link).toHaveAttribute('data-testid', 'attribution')
    expect(link).toHaveAttribute('aria-describedby', 'hint')
    expect(link).toHaveClass('u-link', 'ml-1')
  })

  it('renders the same markup in both densities — nothing branches on density', () => {
    const comfortable = renderComponent(
      <TextLink href="/grafana/" external>
        Open in Grafana
      </TextLink>,
    )
    const comfortableHtml = comfortable.container.innerHTML
    comfortable.unmount()

    const compact = renderComponent(
      <TextLink href="/grafana/" external>
        Open in Grafana
      </TextLink>,
      { density: 'compact' },
    )
    expect(compact.container.innerHTML).toBe(comfortableHtml)
  })
})

describe('TextLink — external', () => {
  it('adds target, the usual rel, the external-link glyph and the hidden sentence', () => {
    const { container } = renderComponent(
      <TextLink href="/grafana/d/pipeline" external>
        Open in Grafana
      </TextLink>,
    )
    const link = screen.getByRole('link', { name: /Open in Grafana/ })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer noopener')
    expect(container.querySelector('[data-icon="external-link"]')).not.toBeNull()
    // The handoff's copy is verbatim, leading space and all; `dom-accessibility-api`
    // trims each node's contribution before joining, which a real browser does not.
    expect(link).toHaveAccessibleName(/^Open in Grafana\s*\(opens in a new tab\)$/)
  })

  it('keeps the glyph out of the accessible name — the sentence is the announcement', () => {
    const { container } = renderComponent(
      <TextLink href="/grafana/" external>
        Open in Grafana
      </TextLink>,
    )
    expect(container.querySelector('[data-icon="external-link"]')).toHaveAttribute('aria-hidden', 'true')
    expect(container.querySelector('.u-visually-hidden')).toHaveTextContent('(opens in a new tab)')
  })

  it('cannot have noopener dropped by a rel of the consumer’s own', () => {
    renderComponent(
      <TextLink href="/grafana/" external rel="nofollow">
        Open in Grafana
      </TextLink>,
    )
    expect(screen.getByRole('link', { name: /Open in Grafana/ })).toHaveAttribute(
      'rel',
      'noreferrer noopener',
    )
  })

  it('leaves a target the consumer set on an internal link alone', () => {
    renderComponent(
      <TextLink href="/titles/0191f4c2" target="_top">
        Stalker
      </TextLink>,
    )
    expect(screen.getByRole('link', { name: 'Stalker' })).toHaveAttribute('target', '_top')
  })
})

describe('TextLink — behaviour', () => {
  it('calls onClick, which is how a features/ screen hands the click to its router', async () => {
    const onClick = vi.fn<(event: { preventDefault: () => void }) => void>((event) => {
      event.preventDefault()
    })
    const { user } = renderComponent(
      <TextLink href="/titles/0191f4c2" onClick={onClick}>
        Stalker
      </TextLink>,
    )
    await user.click(screen.getByRole('link', { name: 'Stalker' }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })
})

describe('TextLink — accessibility (patterns.md §12)', () => {
  it('has no axe violations across the specimen sheet', async () => {
    const { container } = renderComponent(
      <p>
        <TextLink href="/titles/0191f4c2">Stalker</TextLink>{' '}
        <TextLink href="/sources/1" quiet>
          emby:4412
        </TextLink>{' '}
        <TextLink href="/grafana/" external>
          Open in Grafana
        </TextLink>
      </p>,
    )
    // The premise first: axe over an empty container passes too.
    expect(screen.getAllByRole('link')).toHaveLength(3)
    await expectNoViolations(container)
  })
})
