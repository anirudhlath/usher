import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { Icon } from '../icon'
import { IconButton } from '.'

describe('IconButton — contract', () => {
  it('renders exactly the classes actions.css styles by default', () => {
    renderComponent(<IconButton label="Search" icon={<Icon name="search" size={20} />} />)
    const button = screen.getByRole('button', { name: 'Search' })
    expect(button.className.split(' ')).toEqual(['u-iconbtn'])
    expect(button).toHaveAttribute('type', 'button')
  })

  it('renders the icon it was given', () => {
    const { container } = renderComponent(
      <IconButton label="Developer drawer" icon={<Icon name="terminal" size={20} />} />,
    )
    expect(container.querySelector('[data-icon="terminal"]')).not.toBeNull()
  })

  it('renders the sm size modifier and nothing at md', () => {
    const small = renderComponent(<IconButton size="sm" label="Search" icon={<Icon name="search" />} />)
    expect(screen.getByRole('button', { name: 'Search' })).toHaveClass('u-iconbtn--sm')
    small.unmount()

    renderComponent(<IconButton size="md" label="Search" icon={<Icon name="search" />} />)
    expect(screen.getByRole('button', { name: 'Search' }).className).not.toMatch(/u-iconbtn--(sm|md)/)
  })

  it('renders the outlined modifier, which is the 3:1 boundary over artwork', () => {
    renderComponent(
      <IconButton outlined label="Next episode" icon={<Icon name="chevron-right" size={20} />} />,
    )
    expect(screen.getByRole('button', { name: 'Next episode' })).toHaveClass('u-iconbtn--outlined')
  })

  it('spreads the rest of the native props onto the root and merges className', () => {
    renderComponent(
      <IconButton
        label="Search"
        icon={<Icon name="search" />}
        id="search-trigger"
        data-testid="search"
        aria-describedby="hint"
        className="ml-auto"
      />,
    )
    const button = screen.getByRole('button', { name: 'Search' })
    expect(button).toHaveAttribute('id', 'search-trigger')
    expect(button).toHaveAttribute('data-testid', 'search')
    expect(button).toHaveAttribute('aria-describedby', 'hint')
    expect(button).toHaveClass('u-iconbtn', 'ml-auto')
  })
})

describe('IconButton — behaviour', () => {
  it('calls onClick', async () => {
    const onClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <IconButton label="Open developer drawer" icon={<Icon name="terminal" />} onClick={onClick} />,
    )
    await user.click(screen.getByRole('button', { name: 'Open developer drawer' }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('blocks the click while disabled and stays named', async () => {
    const onClick = vi.fn<() => void>()
    const { user } = renderComponent(
      <IconButton label="Delete source" icon={<Icon name="trash-2" />} disabled onClick={onClick} />,
    )
    const button = screen.getByRole('button', { name: 'Delete source' })
    expect(button).toBeDisabled()
    await user.click(button)
    expect(onClick).not.toHaveBeenCalled()
  })
})

describe('IconButton — density (patterns.md §10)', () => {
  it.each(['comfortable', 'compact'] as const)(
    'keeps the 44 px touch target in %s density — touch overrides density',
    (density) => {
      renderComponent(
        <IconButton touch label="Next episode" icon={<Icon name="chevron-right" size={20} />} />,
        { density },
      )
      // `u-iconbtn--touch` is `--target-touch` (44 px) and lands after the size
      // rule, so compact cannot shrink it. The component reads no attribute to
      // decide this — the same class list is emitted either way.
      expect(screen.getByRole('button', { name: 'Next episode' }).className.split(' ')).toEqual([
        'u-iconbtn',
        'u-iconbtn--touch',
      ])
    },
  )

  it('keeps touch winning over the sm size in compact density', () => {
    renderComponent(
      <IconButton touch size="sm" label="Next episode" icon={<Icon name="chevron-right" />} />,
      { density: 'compact' },
    )
    expect(screen.getByRole('button', { name: 'Next episode' })).toHaveClass(
      'u-iconbtn--sm',
      'u-iconbtn--touch',
    )
  })
})

describe('IconButton — accessibility (patterns.md §12)', () => {
  it('makes label both the accessible name and the tooltip, in the same words', () => {
    renderComponent(<IconButton label="Open developer drawer" icon={<Icon name="terminal" />} />)
    const button = screen.getByRole('button', { name: 'Open developer drawer' })
    expect(button).toHaveAccessibleName('Open developer drawer')
    expect(button).toHaveAttribute('title', 'Open developer drawer')
  })

  it('hides the glyph from assistive tech — the label is the name', () => {
    const { container } = renderComponent(
      <IconButton label="Search" icon={<Icon name="search" size={20} />} />,
    )
    expect(container.querySelector('[data-icon="search"]')).toHaveAttribute('aria-hidden', 'true')
  })

  it('has no axe violations across the specimen sheet', async () => {
    const { container } = renderComponent(
      <div>
        <IconButton label="Search" icon={<Icon name="search" size={20} />} />
        <IconButton label="Developer drawer" icon={<Icon name="terminal" size={20} />} outlined />
        <IconButton label="Next" icon={<Icon name="chevron-right" size={20} />} outlined touch />
        <IconButton label="Disabled" icon={<Icon name="trash-2" size={20} />} disabled />
      </div>,
    )
    // The premise first: axe over an empty container passes too.
    expect(screen.getAllByRole('button')).toHaveLength(4)
    await expectNoViolations(container)
  })
})

describe('IconButton — anti-patterns', () => {
  it('cannot be constructed without a label', () => {
    // The reference client shipped unnamed icon buttons; §12 says this component
    // "cannot be constructed without label", so the type is where that is enforced
    // rather than a review comment. `npm run typecheck` fails if this ever compiles.
    // @ts-expect-error `label` is required.
    const unnamed = <IconButton icon={<Icon name="search" />} />
    expect(unnamed).toBeTruthy()
  })

  it('cannot have its accessible name overridden away from its tooltip', () => {
    // `aria-label` is Omitted from the inherited attributes, but TypeScript exempts
    // hyphenated JSX attribute names from excess-property checking, so the type
    // alone cannot stop this one — the component applies `aria-label` and `title`
    // after `...rest` so the label always wins. Both halves are needed.
    const element = <IconButton label="Search" aria-label="Something else" icon={<Icon name="search" />} />
    renderComponent(element)
    const button = screen.getByRole('button', { name: 'Search' })
    expect(button).toHaveAccessibleName('Search')
    expect(button).toHaveAttribute('title', 'Search')
  })

  it('never renders an unnamed control, whatever else it is given', () => {
    renderComponent(<IconButton label="Retry" icon={<Icon name="rotate-cw" />} title="" className="" touch />)
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveAccessibleName('Retry')
  })
})
