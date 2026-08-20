import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { TargetPicker, type PlayTarget } from './index'
import type { DisplayTarget } from './TargetPicker'

/** A ticket good for 300 s. Nothing in this file may ever find it in the DOM. */
const TICKET = 'http://10.0.0.4:8100/play/t/0191f4c2-8a7e-7c31-b0d9-2f6a1e4c8b55?sig=9c1d7f'

function target(overrides: Partial<PlayTarget> = {}): PlayTarget {
  return {
    kind: 'direct',
    url: TICKET,
    source: { id: '1', name: 'Living Room' },
    container: 'MKV',
    video_codec: 'HEVC',
    audio: 'TrueHD 7.1',
    hdr_format: 'HDR10',
    resolution: '2160p',
    runtime_seconds: 9660,
    resume_position_seconds: 0,
    ...overrides,
  }
}

const TARGETS: PlayTarget[] = [
  target(),
  target({
    resolution: '1080p',
    video_codec: 'H264',
    container: 'MP4',
    audio: 'AAC 5.1',
    resume_position_seconds: 4100,
  }),
  target({
    kind: 'deep_link',
    scheme: 'infuse',
    source: { id: '2', name: 'Attic' },
    hdr_format: 'Dolby Vision',
  }),
  target({ source: { id: '2', name: 'Attic' }, hdr_format: 'HDR10+', video_codec: 'AV1', audio: 'Opus 5.1' }),
]

const canDecode = (t: PlayTarget): boolean => t.kind === 'direct' && t.video_codec !== 'AV1'

/** Every attribute of every element, plus all text. The ticket may appear in none of them. */
function everythingRendered(container: HTMLElement): string[] {
  const found = [container.innerHTML]
  for (const element of Array.from(container.querySelectorAll('*'))) {
    for (const attribute of Array.from(element.attributes)) found.push(attribute.value)
  }
  return found
}

describe('TargetPicker — contract', () => {
  it('lists one option per copy, in the order the server returned them', () => {
    renderComponent(<TargetPicker targets={TARGETS} canDecode={canDecode} onPlay={vi.fn<() => void>()} />)
    const options = screen.getAllByRole('button')
    expect(options).toHaveLength(4)
    expect(options[0]).toHaveAccessibleName('Play 2160p · HDR10 · HEVC · MKV · TrueHD 7.1 from Living Room')
  })

  it('composes the spec string from the target’s fields — there is no quality string on the wire', () => {
    const { container } = renderComponent(<TargetPicker targets={[target()]} />)
    const specs = container.querySelector('.u-target__specs')
    expect(specs).toHaveTextContent('2160p')
    expect(specs).toHaveTextContent('HDR10')
    expect(specs).toHaveTextContent('HEVC')
    expect(specs).toHaveTextContent('MKV')
  })

  it('marks the first decodable copy as the best one', () => {
    const { container } = renderComponent(<TargetPicker targets={TARGETS} canDecode={canDecode} />)
    expect(container.querySelectorAll('.u-target--best')).toHaveLength(1)
    expect(container.querySelectorAll('.u-target')[0]).toHaveClass('u-target--best')
  })

  it('badges a deep link with its scheme and labels the action "Hand off"', () => {
    renderComponent(<TargetPicker targets={TARGETS} canDecode={canDecode} />)
    expect(screen.getByText('infuse')).toHaveClass('u-badge')
    expect(screen.getByRole('button', { name: /^Hand off/ })).toBeInTheDocument()
  })

  it('offers a resume, with the position, when the source reported one', () => {
    renderComponent(<TargetPicker targets={TARGETS} canDecode={canDecode} />)
    expect(screen.getByRole('button', { name: /^Resume 1080p/ })).toBeInTheDocument()
    expect(screen.getByText('resume 68m')).toBeInTheDocument()
  })

  it('keeps an undecodable copy visible and dimmed, with the reason in words', () => {
    const { container } = renderComponent(<TargetPicker targets={TARGETS} canDecode={canDecode} />)
    // The AV1 copy and the hand-off copy: neither plays in this browser, both stay on screen.
    expect(container.querySelectorAll('.u-target--undecodable')).toHaveLength(2)
    expect(screen.getAllByText("your browser can't decode this")).toHaveLength(2)
  })

  it('defaults the decode probe to direct targets only', () => {
    const { container } = renderComponent(
      <TargetPicker targets={[target({ kind: 'deep_link', scheme: 'vlc' })]} />,
    )
    expect(container.querySelector('.u-target--undecodable')).not.toBeNull()
  })

  it('collapses one obvious copy into a single Play button', () => {
    const { container } = renderComponent(<TargetPicker targets={[target()]} compact />)
    expect(container.querySelector('.u-targets')).toBeNull()
    expect(screen.getByRole('button', { name: /Play/ })).toHaveClass('u-btn--primary')
  })

  it('says Resume on the collapsed button when there is a position to resume from', () => {
    renderComponent(<TargetPicker targets={[target({ resume_position_seconds: 4100 })]} compact />)
    expect(screen.getByRole('button', { name: /Resume/ })).toBeInTheDocument()
  })
})

describe('TargetPicker — behaviour', () => {
  it('hands the chosen target back to the caller, ticket and all', async () => {
    const onPlay = vi.fn<() => void>()
    const { user } = renderComponent(<TargetPicker targets={TARGETS} canDecode={canDecode} onPlay={onPlay} />)
    await user.click(screen.getByRole('button', { name: /^Resume 1080p/ }))
    expect(onPlay).toHaveBeenCalledTimes(1)
    expect(onPlay).toHaveBeenCalledWith(TARGETS[1])
  })

  it('turns an expired ticket into a one-tap recovery, not an error page', async () => {
    const onRetryTicket = vi.fn<() => void>()
    const { user } = renderComponent(<TargetPicker targets={TARGETS} expired onRetryTicket={onRetryTicket} />)
    expect(screen.getByText('That link expired.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Play again' }))
    expect(onRetryTicket).toHaveBeenCalledTimes(1)
  })
})

describe('TargetPicker — accessibility (§12)', () => {
  it('is one labelled group of one focusable button per copy', () => {
    renderComponent(<TargetPicker targets={TARGETS} canDecode={canDecode} />)
    const group = screen.getByRole('group', { name: 'Playback options' })
    expect(group).toBeInTheDocument()
    // One focusable control per copy — no play button nested inside a focusable card.
    expect(screen.getAllByRole('button')).toHaveLength(4)
  })

  it('announces the expired strip politely, never assertively', () => {
    const { container } = renderComponent(
      <TargetPicker targets={TARGETS} expired onRetryTicket={vi.fn<() => void>()} />,
    )
    // `role="status"` supplies polite implicitly. The previous spelling was
    // `role="alert"` with an explicit `aria-live="polite"` fighting it —
    // which some assistive tech resolves the other way.
    const strip = screen.getByRole('status')
    expect(strip).not.toHaveAttribute('aria-live', 'assertive')
    expect(document.querySelectorAll('[role="alert"], [aria-live="assertive"]')).toHaveLength(0)
    expect(container.innerHTML).not.toContain('assertive')
  })

  it('has no axe violations', async () => {
    const { container } = renderComponent(
      <TargetPicker targets={TARGETS} canDecode={canDecode} onPlay={vi.fn<() => void>()} />,
    )
    await expectNoViolations(container)
  })
})

describe('TargetPicker — the ticket URL is a secret (§13)', () => {
  it.each([
    [
      'the chooser',
      <TargetPicker key="list" targets={TARGETS} canDecode={canDecode} onPlay={vi.fn<() => void>()} />,
    ],
    [
      'the collapsed button',
      <TargetPicker key="one" targets={[target()]} compact onPlay={vi.fn<() => void>()} />,
    ],
    [
      'the expired strip',
      <TargetPicker key="exp" targets={TARGETS} expired onRetryTicket={vi.fn<() => void>()} />,
    ],
  ])('never emits the ticket anywhere in %s', (_case, element) => {
    const { container } = renderComponent(element)
    for (const rendered of everythingRendered(container)) {
      expect(rendered).not.toContain(TICKET)
      expect(rendered).not.toContain('sig=9c1d7f')
    }
  })

  it('puts no url into href, title, aria-label or any data attribute', () => {
    const { container } = renderComponent(
      <TargetPicker targets={TARGETS} canDecode={canDecode} onPlay={vi.fn<() => void>()} />,
    )
    for (const element of Array.from(container.querySelectorAll('*'))) {
      expect(element.getAttribute('href')).toBeNull()
      expect(element.getAttribute('title')).toBeNull()
      expect(element.getAttribute('src')).toBeNull()
      expect(element.getAttribute('aria-label') ?? '').not.toContain('http')
    }
  })

  it('offers no copy and no share affordance', () => {
    renderComponent(<TargetPicker targets={TARGETS} canDecode={canDecode} onPlay={vi.fn<() => void>()} />)
    expect(screen.queryByRole('button', { name: /copy|share|link/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('keeps `url` off the type the render path sees', () => {
    // Structural, not incidental: the render functions take a `DisplayTarget`, so reaching for the
    // ticket is a compile error. If this line ever stops erroring, the guarantee has been lost.
    // @ts-expect-error — `url` is omitted from DisplayTarget on purpose.
    const reachable: DisplayTarget['url'] = undefined
    expect(reachable).toBeUndefined()
  })
})
