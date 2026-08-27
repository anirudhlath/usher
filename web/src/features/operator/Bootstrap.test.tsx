import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'
import { ToastProvider } from '@/patterns'
import { ToastStack } from '@/features/shared/ToastStack'
import { renderApp, screen, waitFor, within } from '@/test/render'
import { installFakeEventSource } from '@/test/sse'
import { server } from '@/test/server'
import { expectNoViolations } from '@/test/axe'
import {
  bootstrapStatus,
  bootstrapStatusEmpty,
  importCompleted,
  importRunning,
  problemHandler,
  sourceUnavailable,
} from '@/test/fixtures'
import type { BootstrapStatusResponse } from '@/api'
import { TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES } from '@/app/routes'
import BootstrapScreen from './Bootstrap'

/**
 * The toast region, never the whole document. The screen renders a
 * `LiveIndicator`, whose §7 announcement is its own `role="status"` live
 * region, so a bare `findByRole('status')` is ambiguous — and that ambiguity is
 * the component working as specified rather than a collision to design around.
 * Same helper `Sources.test.tsx` already uses.
 */
function toasts(): HTMLElement {
  return screen.getByRole('region', { name: 'Notifications' })
}

function render() {
  return renderApp(
    <ToastProvider>
      <BootstrapScreen />
      <ToastStack />
    </ToastProvider>,
    { theme: 'light', density: 'compact', route: ROUTES.bootstrap },
  )
}

/**
 * The shipped fixture's `heartbeat_at` is a fixed timestamp and is therefore
 * always older than the 120 s threshold by the time a test runs. The stall
 * boundary is a fact about *age*, so it is dated against the same clock the
 * screen reads — no fake timers, and the assertion is exercisable at 119 and
 * 121 exactly as `CursorProgress` was designed to be.
 */
function statusWithHeartbeat(agoSeconds: number): BootstrapStatusResponse {
  return {
    ...bootstrapStatus,
    runs: [
      {
        ...importRunning,
        started_at: new Date(Date.now() - 3_600_000).toISOString(),
        heartbeat_at: new Date(Date.now() - agoSeconds * 1000).toISOString(),
      },
    ],
  }
}

function heartbeatHandler(agoSeconds: number) {
  return http.get('/admin/bootstrap/status', () => HttpResponse.json(statusWithHeartbeat(agoSeconds)))
}

describe('Bootstrap', () => {
  it('lists the six phases in mandatory execution order and says why the order is not stylistic', async () => {
    const { container } = render()

    expect(await screen.findByRole('heading', { level: 1, name: 'Bootstrap' })).toBeInTheDocument()
    await screen.findByText('IMDb basics')

    const wire = Array.from(container.querySelectorAll('.u-mono'))
      .map((node) => node.textContent)
      .filter((text): text is string =>
        ['imdb', 'credit-names', 'aliases', 'tmdb-ids', 'crosswalk', 'movielens'].includes(text ?? ''),
      )
    expect(wire).toEqual(['imdb', 'credit-names', 'aliases', 'tmdb-ids', 'crosswalk', 'movielens'])

    expect(screen.getByText(/joins to titles on imdb_id, and it writes only skeletons/)).toBeInTheDocument()
  })

  it('shows the loading state as a table-shaped skeleton in a busy region', () => {
    const { container } = render()

    expect(screen.getByText('Loading bootstrap status …')).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  })

  it('renders no percent character and no aria-valuenow in its cursor progress', async () => {
    server.use(heartbeatHandler(4))
    const { container } = render()

    await screen.findByText('No completion estimate — the server reports a cursor, not a percentage.')

    const bars = screen.getAllByRole('progressbar')
    expect(bars.length).toBeGreaterThan(0)
    for (const bar of bars) {
      expect(bar).not.toHaveAttribute('aria-valuenow')
      expect(bar).toHaveAttribute('aria-valuetext')
      const card = bar.closest('.u-cursor')
      expect(card).not.toBeNull()
      expect(card?.textContent ?? '').not.toContain('%')
    }

    // The resume point, verbatim and in mono.
    expect(within(container).getByText(String(importRunning.position))).toBeInTheDocument()
  })

  it('shows rows/sec as an em dash until a second poll has happened', async () => {
    server.use(heartbeatHandler(4))
    render()

    const label = await screen.findByText('rows / sec')
    const value = label.nextElementSibling
    expect(value?.textContent).toBe('—')
  })

  it('does not call a heartbeat 119 s old stalled', async () => {
    server.use(heartbeatHandler(119))
    render()

    await screen.findByText('No completion estimate — the server reports a cursor, not a percentage.')
    expect(screen.queryByText('Stalled?')).toBeNull()
    expect(screen.queryByText('stalled?')).toBeNull()
  })

  it('calls a heartbeat 121 s old "Stalled?" — with the question mark, because the inference is ours', async () => {
    server.use(heartbeatHandler(121))
    render()

    expect(await screen.findByText('Stalled?')).toBeInTheDocument()
    expect(screen.getByText(/No heartbeat for over 120 s\. The import may have died/)).toBeInTheDocument()
    expect(
      screen.queryByText('No completion estimate — the server reports a cursor, not a percentage.'),
    ).toBeNull()
  })

  it('polls only while something is running, and says so when nothing is', async () => {
    server.use(heartbeatHandler(4))
    const { unmount } = render()
    expect(await screen.findByText('polling every 10 s while something runs')).toBeInTheDocument()
    unmount()

    server.use(
      http.get('/admin/bootstrap/status', () =>
        HttpResponse.json({
          ...bootstrapStatus,
          runs: bootstrapStatus.runs.filter((run) => run.status !== 'running'),
        }),
      ),
    )
    render()
    expect(await screen.findByText('idle — not polling')).toBeInTheDocument()
  })

  it('treats a failed run as a normal state: bad tone, the error verbatim, the position kept, and "Resume"', async () => {
    const { container } = render()

    // Named twice on purpose: once as the live run, once as the phase it is.
    expect((await screen.findAllByText('crosswalk')).length).toBeGreaterThan(0)
    // The status word in the bad tone, carried by the class the CSS keys on.
    expect(container.querySelector('.u-cursor__status--failed')?.textContent).toBe('failed')
    expect(screen.getByText('wdqs: HTTP 429 after 3 retries (query timeout 60 s)')).toBeInTheDocument()
    expect(screen.getByText('88,140')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument()
  })

  it('confirms an import with four facts, is not red, and raises a Queued receipt carrying the job key', async () => {
    const { user } = render()

    await user.click(await screen.findByRole('button', { name: 'Resume' }))

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Run the Wikidata crosswalk phase?')).toBeInTheDocument()
    expect(within(dialog).getByText('downloads')).toBeInTheDocument()
    expect(within(dialog).getByText('measured')).toBeInTheDocument()
    expect(within(dialog).getByText('writes')).toBeInTheDocument()
    expect(within(dialog).getByText('resumable')).toBeInTheDocument()

    // An import is expensive-but-safe, so the confirm is `primary`, never red.
    const confirm = within(dialog).getByRole('button', { name: 'Resume import' })
    expect(confirm).toHaveClass('u-btn--primary')
    expect(confirm).not.toHaveClass('u-btn--danger-solid')

    await user.click(confirm)

    const toast = await within(toasts()).findByRole('status')
    expect(toast.textContent).toContain('Queued')
    expect(toast.textContent).toContain('Queued the crosswalk phase')
    expect(within(toast).getByText('key bootstrap:crosswalk')).toBeInTheDocument()
    expect(within(toast).getByRole('link', { name: /Watch it under Running now/ })).toBeInTheDocument()
  })

  it('states an unmeasured duration rather than inventing a range', async () => {
    const { user } = render()

    // `credit-names` has never run here, so nothing has measured it.
    await user.click((await screen.findAllByRole('button', { name: 'Run' }))[0] as HTMLElement)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Run the Credit names phase?')).toBeInTheDocument()
    expect(within(dialog).getByText('not measured on this deployment')).toBeInTheDocument()
  })

  it('prints a measured duration read off the run that actually finished here', async () => {
    render()

    // movielens: 2026-08-17T23:04:12Z → 23:19:38Z is 15 m 26 s.
    expect(await screen.findByText(/15 m 26 s on this deployment/)).toBeInTheDocument()
  })

  it('prints genome coverage as counts and as a ratio whose denominator is named', async () => {
    render()

    expect(await screen.findByText('128,400 / 130,647 — 98.3%')).toBeInTheDocument()
    expect(screen.getByText(/of enriched titles — the denominator is/)).toBeInTheDocument()
    expect(screen.getByText('128,400 / 1,272,869 — 10.1%')).toBeInTheDocument()
    expect(screen.getByText('enriched_with_vector')).toBeInTheDocument()
  })

  it('states the never-built database as a fact about the database, not as an error', async () => {
    server.use(http.get('/admin/bootstrap/status', () => HttpResponse.json(bootstrapStatusEmpty)))
    render()

    expect(await screen.findByText('The catalog has never been built')).toBeInTheDocument()
    expect(
      screen.getByText(
        'No import has ever run on this deployment, so there is no checkpoint to resume from.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('runs: [] · titles: 0')).toBeInTheDocument()
    // No fabricated ratio out of a zero denominator.
    expect(screen.getAllByText('0 / 0 — no denominator to divide by').length).toBeGreaterThan(0)
    expect(screen.getByText('idle — not polling')).toBeInTheDocument()
  })

  it('renders a failed status read as a panel-scale Problem with code, status and detail', async () => {
    server.use(problemHandler('get', '/admin/bootstrap/status', sourceUnavailable('/admin/bootstrap/status')))
    render()

    expect(await screen.findByText('code source_unavailable')).toBeInTheDocument()
    expect(screen.getByText('HTTP 503')).toBeInTheDocument()
    expect(screen.getByText('Living Room Emby did not answer within 5.0 s.')).toBeInTheDocument()
  })

  it('has no accessibility violations', async () => {
    server.use(heartbeatHandler(4))
    const { container } = render()

    await screen.findByText('No completion estimate — the server reports a cursor, not a percentage.')
    await expectNoViolations(container)
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderFailed(tempoUrl: string | null, traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/admin/bootstrap/status',
          sourceUnavailable('/admin/bootstrap/status'),
          traceId === undefined ? {} : { traceId },
        ),
      )
      return renderApp(
        withTempo(
          <ToastProvider>
            <BootstrapScreen />
            <ToastStack />
          </ToastProvider>,
          tempoUrl,
        ),
        { theme: 'light', density: 'compact', route: ROUTES.bootstrap },
      )
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      const { container } = renderFailed(TEMPO_URL, TRACE_ID)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      const { container } = renderFailed(null, TRACE_ID)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      const { container } = renderFailed(TEMPO_URL)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })
  })
})

/**
 * The screen is driven by `bootstrap.progress`, and these are the cases that
 * distinguish that from the poll it replaced. The fake `EventSource` is
 * installed before `render()` in every one of them, because `openEventStream`
 * constructs its socket in the mount effect and a fake installed afterwards is
 * a fake nothing ever uses.
 */
describe('Bootstrap — live rather than polled', () => {
  let sse: ReturnType<typeof installFakeEventSource>

  beforeEach(() => {
    sse = installFakeEventSource()
  })

  afterEach(() => {
    sse.restore()
  })

  /** A `runs` array in which nothing is running — the state a trigger starts from. */
  const settled: BootstrapStatusResponse = {
    ...bootstrapStatus,
    runs: [{ ...importCompleted }],
  }

  function frame(over: Partial<Record<string, unknown>> = {}) {
    return {
      dataset: 'imdb.title.basics',
      phase: 'imdb',
      requested_phase: 'all',
      status: 'running',
      revision: '2026-08-26',
      position: 418_002,
      rows_seen: 418_002,
      rows_written: 411_774,
      error: null,
      started_at: new Date(Date.now() - 60_000).toISOString(),
      heartbeat_at: new Date().toISOString(),
      finished_at: null,
      ...over,
    }
  }

  it('reaches the running state after a trigger with no reload, which is the bug this replaced', async () => {
    /**
     * The defect, as a case. `POST /admin/bootstrap/{phase}` answers 202 before
     * the worker has claimed the job, so the single refetch the mutation
     * invalidates still reports nothing running — and a `refetchInterval` gated
     * on "is something running" then evaluates `false` and never fires again.
     * Measured on a real deployment: one status read after the press and then
     * **91 seconds of silence** while all eight datasets imported and finished.
     *
     * Nothing here advances a timer, and that is the point: the screen must
     * arrive at the running state on the strength of a frame alone.
     */
    server.use(http.get('/admin/bootstrap/status', () => HttpResponse.json(settled)))
    const { user } = render()

    await screen.findByText('Nothing is running')
    await user.click(await screen.findByRole('button', { name: 'Run all phases' }))
    await user.click(screen.getByRole('button', { name: 'Start all phases' }))
    await within(toasts()).findByRole('status')

    sse.latest().open()
    sse.latest().emit('bootstrap.progress', frame())

    expect(await screen.findByText('IMDb basics')).toBeInTheDocument()
    expect(screen.queryByText('Nothing is running')).toBeNull()
    expect(await screen.findByText('418,002')).toBeInTheDocument()
  })

  it('patches the card from the frame rather than answering it with a request', async () => {
    /**
     * The whole reason the payload is the whole run. A frame that only said
     * *something moved* would buy a `GET /admin/bootstrap/status` per committed
     * batch — ~0.33 s, uncached, four scans of `titles`, 61 of them for
     * `--phase imdb` alone — which is strictly worse than the 10 s poll.
     *
     * Counting the requests is the assertion; asserting the number changed is
     * not, because a refetch would change it too.
     */
    let reads = 0
    server.use(
      http.get('/admin/bootstrap/status', () => {
        reads += 1
        return HttpResponse.json({ ...settled, runs: [{ ...importRunning, rows_seen: 1 }] })
      }),
    )
    render()

    await screen.findByText('1', { selector: '.u-cursor__v' })
    const afterFirstPaint = reads

    sse.latest().open()
    sse.latest().emit('bootstrap.progress', frame({ rows_seen: 999_111 }))

    expect(await screen.findByText('999,111')).toBeInTheDocument()
    expect(reads).toBe(afterFirstPaint)
  })

  it('refetches once on a terminal frame, because titles and the genome are not on it', async () => {
    /**
     * The one refetch that survives, and it is bounded to a transition rather
     * than to a batch. `bootstrap.progress` carries the run and nothing else —
     * `titles`, the genome counts and the vocabulary all move when a phase
     * finishes and none of them is on the wire.
     */
    let reads = 0
    server.use(
      http.get('/admin/bootstrap/status', () => {
        reads += 1
        return HttpResponse.json(settled)
      }),
    )
    render()

    await screen.findByText('Nothing is running')
    const afterFirstPaint = reads

    sse.latest().open()
    sse.latest().emit('bootstrap.progress', frame({ rows_seen: 12 }))
    expect(await screen.findByText('12')).toBeInTheDocument()
    expect(reads).toBe(afterFirstPaint)

    sse
      .latest()
      .emit('bootstrap.progress', frame({ status: 'completed', finished_at: new Date().toISOString() }))
    await waitFor(() => expect(reads).toBe(afterFirstPaint + 1))
  })

  it('actually stops polling while live, and actually resumes when the stream drops', async () => {
    /**
     * The case with teeth, and the one the copy above cannot be. A screen whose
     * badge reads "live — not polling" while a 10 s interval keeps firing
     * passes every assertion about the sentence — the claim and the behaviour
     * are separate facts and only this one is about the behaviour.
     *
     * Both directions in one case deliberately: "no requests happened" is also
     * what a broken query produces, so the second half is the control that
     * makes the first half mean something.
     */
    vi.useFakeTimers()
    try {
      let reads = 0
      server.use(
        http.get('/admin/bootstrap/status', () => {
          reads += 1
          return HttpResponse.json({ ...settled, runs: [{ ...importRunning }] })
        }),
      )
      render()

      await vi.waitFor(() => expect(reads).toBeGreaterThan(0))
      sse.latest().open()
      await vi.waitFor(() => expect(screen.queryByText('live — not polling')).not.toBeNull())

      const whileLive = reads
      await vi.advanceTimersByTimeAsync(60_000)
      expect(reads).toBe(whileLive)

      sse.latest().fail()
      await vi.waitFor(() => expect(screen.queryByText(/polling every 10 s/)).not.toBeNull())
      await vi.advanceTimersByTimeAsync(60_000)
      expect(reads).toBeGreaterThan(whileLive)
    } finally {
      vi.useRealTimers()
    }
  })

  it('says it is live and not polling, and says the opposite when the stream is down', async () => {
    /**
     * patterns.md §7 requires the UI to be fully correct if zero frames ever
     * arrive, and §8 requires the fallback to be *visible*: with `usher work` in
     * its own container every frame reaches a `NullEventPublisher` and no client
     * is ever told. A fallback nobody can see is indistinguishable from a screen
     * that has quietly stopped updating.
     */
    server.use(
      http.get('/admin/bootstrap/status', () =>
        HttpResponse.json({ ...settled, runs: [{ ...importRunning }] }),
      ),
    )
    render()

    sse.latest().open()
    expect(await screen.findByText('live — not polling')).toBeInTheDocument()
    expect(screen.queryByText(/polling every 10 s/)).toBeNull()

    // A dropped stream is the split deployment and a restarting backend alike,
    // and the fallback has to be nameable in both.
    sse.latest().fail()
    expect(await screen.findByText(/polling every 10 s/)).toBeInTheDocument()
    expect(screen.queryByText('live — not polling')).toBeNull()
  })
})
