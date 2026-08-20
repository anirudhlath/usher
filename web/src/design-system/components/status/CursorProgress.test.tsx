import { describe, expect, it } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { CursorProgress } from './index'

/** The specimen's live run. */
const RUNNING = {
  dataset: 'imdb',
  phase: 'title.basics',
  status: 'running',
  rowsSeen: 4_120_338,
  rowsWritten: 3_988_104,
  rowsPerSecond: 1240,
  position: 'tt0104988',
  elapsed: '1 h 12 m',
  heartbeatAgoSeconds: 4,
  revision: '2026-08-19',
} as const

const NO_ESTIMATE = 'No completion estimate — the server reports a cursor, not a percentage.'

/** The value beside a key in the six-number grid. */
function valueFor(container: HTMLElement, key: string): string {
  const cell = [...container.querySelectorAll('.u-cursor__k')].find((element) => element.textContent === key)
  return cell?.nextElementSibling?.textContent ?? ''
}

describe('CursorProgress — contract', () => {
  it('names the dataset, the phase and the revision', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} />)
    expect(container.querySelector('.u-cursor__ds')).toHaveTextContent('imdb')
    expect(container.querySelector('.u-cursor__phase')).toHaveTextContent('title.basics · rev 2026-08-19')
  })

  it('renders the six real numbers', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} />)
    expect(valueFor(container, 'rows seen')).toBe('4,120,338')
    expect(valueFor(container, 'rows written')).toBe('3,988,104')
    expect(valueFor(container, 'rows / sec')).toBe('1,240')
    expect(valueFor(container, 'elapsed')).toBe('1 h 12 m')
    expect(valueFor(container, 'heartbeat')).toBe('4s ago')
    expect(valueFor(container, 'position')).toBe('tt0104988')
  })

  it('prints position verbatim in mono — it is the resume point', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} />)
    const cell = [...container.querySelectorAll('.u-cursor__k')].find(
      (element) => element.textContent === 'position',
    )
    expect(cell?.nextElementSibling).toHaveClass('u-cursor__v')
    expect(cell?.nextElementSibling).toHaveTextContent('tt0104988')
  })

  it('shows rows/sec as an em dash until two polls have happened', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} rowsPerSecond={null} />)
    expect(valueFor(container, 'rows / sec')).toBe('—')
  })

  it('shows an unknown heartbeat as an em dash rather than as zero', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={null} />)
    expect(valueFor(container, 'heartbeat')).toBe('—')
  })

  it('states outright that there is no completion estimate', () => {
    renderComponent(<CursorProgress {...RUNNING} />)
    expect(screen.getByText(NO_ESTIMATE)).toBeInTheDocument()
  })

  it('renders the indeterminate sweep only while running', () => {
    const { container, rerender } = renderComponent(<CursorProgress {...RUNNING} />)
    expect(container.querySelector('.u-cursor__sweep')).not.toBeNull()
    rerender(<CursorProgress {...RUNNING} status="completed" />)
    expect(container.querySelector('.u-cursor__sweep')).toBeNull()
  })

  it.each([
    ['running', 'u-cursor__status--running'],
    ['completed', 'u-cursor__status--completed'],
    ['failed', 'u-cursor__status--failed'],
  ] as const)('renders the %s status word in its own tone', (status, className) => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} status={status} />)
    const word = container.querySelector('.u-cursor__status')
    expect(word).toHaveTextContent(status)
    expect(word).toHaveClass(className)
  })
})

describe('CursorProgress — the 120 s stall threshold (§8)', () => {
  it('is not stalled at 119 s', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={119} />)
    expect(screen.queryByText('Stalled?')).toBeNull()
    expect(container.querySelector('.u-cursor__track')).not.toHaveClass('u-cursor__stalled')
    expect(screen.getByText(NO_ESTIMATE)).toBeInTheDocument()
    expect(container.querySelector('.u-cursor__status')).toHaveTextContent('running')
  })

  it('is stalled at 121 s', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={121} />)
    expect(screen.getByText('Stalled?')).toHaveClass('u-cursor__status--stalled')
    expect(container.querySelector('.u-cursor__track')).toHaveClass('u-cursor__stalled')
  })

  it('keeps the question mark, because the inference is the design’s and not the API’s', () => {
    renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={121} />)
    expect(screen.getByText('Stalled?')).toBeInTheDocument()
    expect(screen.queryByText('Stalled')).toBeNull()
  })

  it('says the run may have died and is resumable from this position', () => {
    renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={121} />)
    expect(
      screen.getByText(
        'No heartbeat for over 120 s. The import may have died; it is resumable from this position.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText(NO_ESTIMATE)).toBeNull()
  })

  it('honours a caller-supplied threshold on both sides of the boundary', () => {
    const { container, rerender } = renderComponent(
      <CursorProgress {...RUNNING} heartbeatAgoSeconds={29} stalledThresholdSeconds={30} />,
    )
    expect(screen.queryByText('Stalled?')).toBeNull()
    rerender(<CursorProgress {...RUNNING} heartbeatAgoSeconds={31} stalledThresholdSeconds={30} />)
    expect(screen.getByText('Stalled?')).toBeInTheDocument()
    expect(container.querySelector('.u-cursor__note--warn')).toHaveTextContent('No heartbeat for over 30 s.')
  })

  it('is never stalled when the heartbeat age is unknown', () => {
    renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={null} />)
    expect(screen.queryByText('Stalled?')).toBeNull()
  })

  it('marks the heartbeat value itself, so the stall is not encoded in the sweep alone', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={121} />)
    const cell = [...container.querySelectorAll('.u-cursor__k')].find(
      (element) => element.textContent === 'heartbeat',
    )
    expect(cell?.nextElementSibling).toHaveClass('u-cursor__v--warn')
  })
})

describe('CursorProgress — a failed run is a designed state (§8)', () => {
  const FAILED = {
    dataset: 'crosswalk',
    phase: 'wikidata',
    status: 'failed',
    rowsSeen: 220_144,
    rowsWritten: 219_008,
    position: 'Q19241',
    elapsed: '18 m',
    heartbeatAgoSeconds: null,
    error: 'HTTPStatusError: 429 Too Many Requests from query.wikidata.org',
  } as const

  it('shows the status word in the bad tone', () => {
    const { container } = renderComponent(<CursorProgress {...FAILED} />)
    const word = container.querySelector('.u-cursor__status')
    expect(word).toHaveTextContent('failed')
    expect(word).toHaveClass('u-cursor__status--failed')
  })

  it('shows the server’s error verbatim and does not parse it', () => {
    renderComponent(<CursorProgress {...FAILED} />)
    expect(screen.getByText('HTTPStatusError: 429 Too Many Requests from query.wikidata.org')).toHaveClass(
      'u-cursor__note--bad',
    )
  })

  it('retains the position, because that is where a resume starts', () => {
    const { container } = renderComponent(<CursorProgress {...FAILED} />)
    expect(valueFor(container, 'position')).toBe('Q19241')
  })

  it('drops the sweep and the no-estimate sentence, which belong to a live run', () => {
    const { container } = renderComponent(<CursorProgress {...FAILED} />)
    expect(container.querySelector('.u-cursor__sweep')).toBeNull()
    expect(screen.queryByText(NO_ESTIMATE)).toBeNull()
  })
})

describe('CursorProgress — accessibility (§12: progress with no denominator)', () => {
  it('is a progressbar with no aria-valuenow', () => {
    renderComponent(<CursorProgress {...RUNNING} />)
    const bar = screen.getByRole('progressbar', { name: 'Importing imdb' })
    expect(bar).not.toHaveAttribute('aria-valuenow')
    expect(bar).not.toHaveAttribute('aria-valuemin')
    expect(bar).not.toHaveAttribute('aria-valuemax')
  })

  it('puts the state in words in aria-valuetext', () => {
    renderComponent(<CursorProgress {...RUNNING} />)
    expect(screen.getByRole('progressbar')).toHaveAttribute(
      'aria-valuetext',
      '4,120,338 rows seen, 3,988,104 written. No completion estimate is available.',
    )
  })

  it('says the stall in words too', () => {
    renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={121} />)
    expect(screen.getByRole('progressbar')).toHaveAttribute(
      'aria-valuetext',
      '4,120,338 rows seen, 3,988,104 written. No heartbeat for over 120 s. Stalled?',
    )
  })

  it('has no axe violations', async () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} />)
    await expectNoViolations(container)
  })

  it('has no axe violations in the operator default (light, compact)', async () => {
    const { container } = renderComponent(
      <>
        <CursorProgress {...RUNNING} />
        <CursorProgress {...RUNNING} heartbeatAgoSeconds={412} rowsPerSecond={0} />
        <CursorProgress dataset="crosswalk" phase="wikidata" status="failed" error="boom" position="Q1" />
      </>,
      { theme: 'light', density: 'compact' },
    )
    await expectNoViolations(container)
  })
})

describe('CursorProgress — anti-patterns (no fabricated denominator)', () => {
  it.each([
    ['running', { ...RUNNING }],
    ['stalled', { ...RUNNING, heartbeatAgoSeconds: 412 }],
    ['completed', { ...RUNNING, status: 'completed' as const }],
    ['failed', { ...RUNNING, status: 'failed' as const, error: 'boom' }],
  ])('renders no percent character in the %s state', (_name, props) => {
    const { container } = renderComponent(<CursorProgress {...props} />)
    expect(container.textContent).not.toContain('%')
    const bar = container.querySelector('[role="progressbar"]')
    expect(bar?.getAttribute('aria-valuetext') ?? '').not.toContain('%')
  })

  it('never sets aria-valuenow, whatever the counts are', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} heartbeatAgoSeconds={412} />)
    expect(container.querySelector('[aria-valuenow]')).toBeNull()
  })

  it('offers no completion estimate or ETA anywhere in the copy', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} />)
    expect(container.textContent).not.toMatch(/ETA|remaining|complete in|\bof\s[\d,]+\b/i)
  })

  it('keeps the counts out of the sweep: the sweep means alive, not n% done', () => {
    const { container } = renderComponent(<CursorProgress {...RUNNING} />)
    const track = container.querySelector('.u-cursor__track')
    expect(track).not.toBeNull()
    expect(track?.textContent).toBe('')
    // No inline width, no transform: nothing here is a fraction of anything.
    expect(track?.getAttribute('style')).toBeNull()
    expect(track?.querySelector('.u-cursor__sweep')?.getAttribute('style')).toBeNull()
  })
})
