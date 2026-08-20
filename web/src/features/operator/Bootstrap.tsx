/**
 * Bootstrap — the dataset import pipeline. This screen is patterns.md §8 in full.
 *
 * · **The phases run in a mandatory order and the order is measured.**
 *   `imdb`, `credit-names`, `aliases`, `tmdb-ids`, `crosswalk`, `movielens`.
 *   Three of them join to `titles` on `imdb_id`, so none of the three can
 *   precede `imdb`; `credit-names` comes before anything that *enriches* a
 *   title, because the fill writes only skeletons and an already-enriched title
 *   is deferred to TMDb for good.
 * · **A progress bar with an invented denominator is forbidden.** The route
 *   returns `rows_seen`, `rows_written` and `position` and deliberately no
 *   total. `CursorProgress` is the idiom: six real numbers, `rows/sec` derived
 *   here from two polls and `—` until there are two, and `position` verbatim in
 *   mono because it is the resume point.
 * · **Stall detection is the design's job.** `heartbeat_at` older than 120 s
 *   renders "Stalled?" — with the question mark, because the API states a
 *   timestamp and the inference is ours. The age is computed here; the
 *   threshold lives in the component.
 * · **Polling is conditional.** Status costs ~0.33 s and is uncached, so it is
 *   polled every 10 s and only while at least one run is `running`. When
 *   nothing runs the screen says "idle — not polling" rather than polling
 *   invisibly forever.
 * · **A `failed` run is a normal, designed state**: bad-tone status word,
 *   `error` verbatim, position retained, trigger relabelled "Resume".
 * · **Genome coverage is counts.** The route returns six of them and declines
 *   the division. Every ratio printed here is shown as numerator / denominator
 *   *and* as a percent whose denominator is named on screen, because picking
 *   one silently is how "~7%" came to mean four different things.
 */

import { useEffect, useRef, useState } from 'react'
import {
  Badge,
  Button,
  ConfirmDialog,
  CursorProgress,
  Icon,
  NOT_MEASURED,
  Problem,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  type ConfirmFact,
  type ProblemDocument,
} from '@/design-system'
import { OpsHeader, OpsSection } from '@/app/shells/OperatorShell'
import { ROUTES } from '@/app/routes'
import { useToasts } from '@/patterns'
import { useProblemTrace } from '@/features/shared/trace'
import {
  UsherProblem,
  useBootstrapStatus,
  useStartBootstrap,
  type BootstrapPhase,
  type ImportRun,
} from '@/api'

/* ------------------------------------------------------------------ shared */

/** An `UsherProblem` as the design system's document. Spread, never an explicit `undefined`. */
function problemOf(error: unknown): ProblemDocument {
  if (error instanceof UsherProblem) {
    return {
      status: error.status,
      detail: error.detail,
      ...(error.knownCode ? { code: error.knownCode } : {}),
      ...(error.instance ? { instance: error.instance } : {}),
      ...(error.retryAfter === null ? {} : { retry_after: error.retryAfter }),
    }
  }
  return { status: 0, detail: String(error) }
}

function secondsSince(iso: string): number | null {
  const at = Date.parse(iso)
  return Number.isNaN(at) ? null : Math.max(0, Math.round((Date.now() - at) / 1000))
}

function formatDuration(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h} h ${String(m).padStart(2, '0')} m`
  if (m > 0) return `${m} m ${String(s).padStart(2, '0')} s`
  return `${s} s`
}

function elapsedOf(run: ImportRun): string | undefined {
  const started = Date.parse(run.started_at)
  if (Number.isNaN(started)) return undefined
  const end = run.finished_at === null ? Date.now() : Date.parse(run.finished_at)
  return Number.isNaN(end) ? undefined : formatDuration(end - started)
}

/**
 * `rows/sec` from two polls, `null` until there are two (§8). The server
 * reports a cursor and no rate; a number before the second poll would be
 * invented rather than derived.
 */
function useThroughput(runs: readonly ImportRun[] | undefined): Map<string, number | null> {
  const previous = useRef<{ at: number; seen: Map<string, number> } | null>(null)
  const [rates, setRates] = useState<Map<string, number | null>>(new Map())

  useEffect(() => {
    if (!runs) return
    const now = Date.now()
    const seen = new Map(runs.map((run) => [run.dataset, run.rows_seen]))
    const last = previous.current
    previous.current = { at: now, seen }
    if (!last) return
    const seconds = (now - last.at) / 1000
    if (seconds <= 0) return
    const next = new Map<string, number | null>()
    for (const [dataset, rows] of seen) {
      const before = last.seen.get(dataset)
      next.set(dataset, before === undefined ? null : Math.max(0, Math.round((rows - before) / seconds)))
    }
    setRates(next)
  }, [runs])

  return rates
}

/* ------------------------------------------------------------------ phases */

interface PhaseSpec {
  phase: Exclude<BootstrapPhase, 'all'>
  label: string
  /** What it downloads. A dataset fact, not a measurement of this deployment. */
  size: string
  writes: string
  /** Why this one cannot move up the list. */
  because: string
}

/**
 * `BootstrapPhase`'s members in execution order — one vocabulary, shared by
 * `POST /admin/bootstrap/{phase}` and the CLI's `--phase`, so a phase cannot
 * exist on one boundary and not the other.
 */
const PHASES: readonly PhaseSpec[] = [
  {
    phase: 'imdb',
    label: 'IMDb basics',
    size: '~224 MB from IMDb (regenerated daily)',
    writes: 'title skeletons — names, years, runtimes',
    because: 'first: everything below joins to the titles this writes',
  },
  {
    phase: 'credit-names',
    label: 'Credit names',
    size: '~730 MB from IMDb',
    writes: 'person records for every credited name',
    because:
      'joins to titles on imdb_id, and it writes only skeletons — a title already enriched is deferred to TMDb for good, so it runs before anything that enriches',
  },
  {
    phase: 'aliases',
    label: 'Aliases',
    size: '~380 MB from IMDb',
    writes: 'alternate titles used by search',
    because: 'joins to titles on imdb_id',
  },
  {
    phase: 'tmdb-ids',
    label: 'TMDb id export',
    size: '~18 MB from TMDb',
    writes: 'the TMDb id crosswalk',
    because: 'needs the titles the IMDb phase wrote to attach ids to',
  },
  {
    phase: 'crosswalk',
    label: 'Wikidata crosswalk',
    size: 'SPARQL against query.wikidata.org, no dump',
    writes: 'IMDb ↔ TMDb ↔ Wikidata links',
    because: 'links the two id spaces the phases above populate',
  },
  {
    phase: 'movielens',
    label: 'MovieLens genome',
    size: '~265 MB from GroupLens',
    writes: 'tag genome vectors used by similarity',
    because: 'joins to titles on imdb_id',
  },
]

/** A dataset's human label, or the wire value when the API names one we do not. */
function labelFor(dataset: string): string {
  return PHASES.find((spec) => spec.phase === dataset)?.label ?? dataset
}

const ALL_FACTS: ConfirmFact[] = [
  { label: 'downloads', value: '~1.6 GB across six datasets' },
  { label: 'measured', value: NOT_MEASURED },
  { label: 'writes', value: 'title skeletons, people, aliases, the crosswalk and the genome' },
  { label: 'resumable', value: 'yes — each phase keeps its own cursor' },
]

/** What a run's status word is called on screen, and in which tone. */
function statusWord(run: ImportRun | undefined, stalled: boolean): string {
  if (!run) return 'never run'
  if (stalled) return 'stalled?'
  return run.status
}

function statusTone(
  run: ImportRun | undefined,
  stalled: boolean,
): 'good' | 'bad' | 'warn' | 'info' | 'neutral' {
  if (!run) return 'neutral'
  if (stalled) return 'warn'
  if (run.status === 'completed') return 'good'
  if (run.status === 'failed') return 'bad'
  return 'info'
}

/** "Resume" for a failed run: the position is retained and the import restarts from it. */
function triggerLabel(run: ImportRun | undefined): string {
  if (!run) return 'Run'
  if (run.status === 'failed') return 'Resume'
  if (run.status === 'running') return 'Running'
  return 'Run again'
}

interface PhaseRowProps {
  spec: PhaseSpec
  index: number
  run: ImportRun | undefined
  measured: string
  onRun: (spec: PhaseSpec) => void
}

function PhaseRow({ spec, index, run, measured, onRun }: PhaseRowProps) {
  const ago = run ? secondsSince(run.heartbeat_at) : null
  const stalled = run?.status === 'running' && ago !== null && ago > 120
  const word = statusWord(run, stalled)

  return (
    <div
      className="flex items-start gap-3"
      style={{
        padding: 'var(--space-3)',
        borderTop: index > 0 ? '1px solid var(--border-subtle)' : 'none',
      }}
    >
      <span className="u-mono flex-none" style={{ color: 'var(--text-muted)', width: 18, paddingTop: 3 }}>
        {index + 1}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="flex flex-wrap items-center gap-2">
          <span style={{ font: 'var(--text-heading-sm)', color: 'var(--text-primary)' }}>{spec.label}</span>
          <span className="u-mono" style={{ color: 'var(--text-muted)' }}>
            {spec.phase}
          </span>
          <Badge tone={statusTone(run, stalled)}>{word}</Badge>
        </span>
        <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
          {spec.size} · measured {measured} · writes {spec.writes} · resumable from the stored cursor
        </span>
        <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
          Order: {spec.because}.
        </span>
      </span>
      <span className="flex-none">
        <Button
          size="sm"
          variant={run?.status === 'running' ? 'ghost' : 'secondary'}
          disabled={run?.status === 'running'}
          onClick={() => onRun(spec)}
        >
          {triggerLabel(run)}
        </Button>
      </span>
    </div>
  )
}

/* ------------------------------------------------------------------ ratios */

function count(value: number): string {
  return value.toLocaleString('en-US')
}

/**
 * A ratio, printed both ways. The percent never travels without the two counts
 * it came from, and the caller names the denominator in words beside it.
 */
function ratio(numerator: number, denominator: number): string {
  if (denominator === 0) return `${count(numerator)} / 0 — no denominator to divide by`
  return `${count(numerator)} / ${count(denominator)} — ${((100 * numerator) / denominator).toFixed(1)}%`
}

/* ------------------------------------------------------------------ screen */

export default function Bootstrap() {
  const toasts = useToasts()
  const traceOf = useProblemTrace()
  const status = useBootstrapStatus({
    // §8: uncached and ~0.33 s a call, so poll only while something is running.
    refetchInterval: (query) =>
      query.state.data?.runs.some((run) => run.status === 'running') ? 10_000 : false,
  })
  const start = useStartBootstrap()
  const [pending, setPending] = useState<PhaseSpec | 'all' | null>(null)

  const data = status.data
  const runs = data?.runs
  const throughput = useThroughput(runs)

  const anyRunning = (runs ?? []).some((run) => run.status === 'running')
  const live = (runs ?? []).filter((run) => run.status === 'running' || run.status === 'failed')
  const neverBuilt = data !== undefined && data.runs.length === 0

  const runFor = (phase: string): ImportRun | undefined => (runs ?? []).find((run) => run.dataset === phase)

  /**
   * The only honest duration available: what this deployment actually took,
   * read off `started_at` and `finished_at` of a run that finished. Anything
   * else is `NOT_MEASURED` — patterns.md §5 forbids an invented range.
   */
  const measuredFor = (phase: string): string => {
    const run = runFor(phase)
    if (!run || run.finished_at === null) return NOT_MEASURED
    const started = Date.parse(run.started_at)
    const finished = Date.parse(run.finished_at)
    if (Number.isNaN(started) || Number.isNaN(finished)) return NOT_MEASURED
    return `${formatDuration(finished - started)} on this deployment`
  }

  const trigger = (target: PhaseSpec | 'all'): void => {
    const phase: BootstrapPhase = target === 'all' ? 'all' : target.phase
    // `(kind, key)` is unique in the queue, so pressing a phase while its own
    // run is live coalesces into the run in flight. That is observable here;
    // for `all` it is not, and an unknown is left unstated (§6).
    const coalesces = target === 'all' ? undefined : runFor(target.phase)?.status === 'running'
    setPending(null)
    start.mutate(phase, {
      onSuccess: (queued) => {
        toasts.receipt({
          title: phase === 'all' ? 'Queued every bootstrap phase' : `Queued the ${phase} phase`,
          detail: 'Accepted with a 202. Progress appears under Running now within a few seconds.',
          // `kind:key` is what the queue deduplicates on, and what an operator
          // pastes into a log search. Nothing can query it.
          jobKey: `${queued.kind}:${queued.key}`,
          ...(coalesces === undefined ? {} : { coalesced: coalesces }),
          destination: { label: 'Watch it under Running now', to: ROUTES.bootstrap },
        })
      },
    })
  }

  const phaseFacts = (spec: PhaseSpec): ConfirmFact[] => [
    { label: 'downloads', value: spec.size },
    { label: 'measured', value: measuredFor(spec.phase) },
    { label: 'writes', value: spec.writes },
    { label: 'resumable', value: 'yes — from the stored cursor' },
  ]

  return (
    <>
      <OpsHeader
        title="Bootstrap"
        subtitle="Six public datasets, in a mandatory order, each with its own durable cursor. Progress is a position, never a percentage."
      />

      <div className="u-ops__body">
        <div className="flex flex-wrap items-center gap-3">
          <Badge tone="neutral" mono>
            {count(data?.titles ?? 0)} titles
          </Badge>
          <Badge
            tone={anyRunning ? 'info' : 'neutral'}
            icon={<Icon name={anyRunning ? 'radio' : 'circle-dashed'} />}
          >
            {anyRunning ? 'polling every 10 s while something runs' : 'idle — not polling'}
          </Badge>
          <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
            Status costs about 0.33 s and is uncached, so it is only polled while a run is live.
          </span>
        </div>

        {status.isPending && (
          <SkeletonRegion busy label="Loading bootstrap status …">
            <Skeleton shape="table" count={6} />
          </SkeletonRegion>
        )}

        {status.isError && (
          <Problem
            scale="panel"
            problem={problemOf(status.error)}
            {...traceOf(status.error)}
            onRetry={() => void status.refetch()}
            icon={<Icon name="x-circle" size={20} />}
          />
        )}

        {start.error !== null && (
          <Problem
            scale="panel"
            problem={problemOf(start.error)}
            {...traceOf(start.error)}
            icon={<Icon name="x-circle" size={20} />}
          />
        )}

        {neverBuilt && (
          <div style={{ maxWidth: 'var(--width-prose)' }}>
            <h2 style={{ font: 'var(--text-title)', color: 'var(--text-primary)' }}>
              The catalog has never been built
            </h2>
            <p
              style={{
                font: 'var(--text-body)',
                color: 'var(--text-secondary)',
                marginTop: 'var(--space-3)',
              }}
            >
              Six phases, in the order below. Every one is resumable and the catalog is browsable throughout —
              you do not have to wait for it to finish before connecting a source. How long it takes on this
              hardware is not known until it has run here once.
            </p>
            <div style={{ marginTop: 'var(--space-5)' }}>
              <Button variant="primary" onClick={() => setPending('all')}>
                Run all phases
              </Button>
            </div>
            <div style={{ marginTop: 'var(--space-5)' }}>
              <StateBlock kind="never" meta={`runs: [] · titles: ${count(data?.titles ?? 0)}`}>
                No import has ever run on this deployment, so there is no checkpoint to resume from.
              </StateBlock>
            </div>
          </div>
        )}

        {data && !neverBuilt && (
          <OpsSection
            title="Running now"
            note="Six real numbers and a position. There is no total on the wire, so there is no bar to fill."
          >
            {live.length > 0 ? (
              <div className="flex flex-col gap-2">
                {live.map((run) => {
                  const elapsed = elapsedOf(run)
                  return (
                    <CursorProgress
                      key={run.dataset}
                      dataset={run.dataset}
                      phase={labelFor(run.dataset)}
                      status={run.status === 'failed' ? 'failed' : 'running'}
                      rowsSeen={run.rows_seen}
                      rowsWritten={run.rows_written}
                      rowsPerSecond={throughput.get(run.dataset) ?? null}
                      // Verbatim, in mono: this is the resume point.
                      position={String(run.position)}
                      revision={run.revision}
                      heartbeatAgoSeconds={secondsSince(run.heartbeat_at)}
                      {...(elapsed === undefined ? {} : { elapsed })}
                      {...(run.error === null ? {} : { error: run.error })}
                    />
                  )
                })}
              </div>
            ) : (
              <StateBlock kind="empty" title="Nothing is running" meta="no run reports status: running">
                Every recorded run has finished. Nothing is being polled — status is uncached and costs about
                0.33 s, so it is only asked for while a run is live.
              </StateBlock>
            )}
          </OpsSection>
        )}

        {data && (
          <OpsSection
            title="Phases"
            note="Listed in mandatory execution order, and the order is measured rather than stylistic. A later phase refuses until the ones above it have completed."
            action={
              <Button size="sm" variant="secondary" onClick={() => setPending('all')}>
                Run all phases
              </Button>
            }
          >
            <div
              style={{
                border: '1px solid var(--border-default)',
                borderRadius: 'var(--radius-card)',
                background: 'var(--bg-surface)',
              }}
            >
              {PHASES.map((spec, index) => (
                <PhaseRow
                  key={spec.phase}
                  spec={spec}
                  index={index}
                  run={runFor(spec.phase)}
                  measured={measuredFor(spec.phase)}
                  onRun={setPending}
                />
              ))}
            </div>
            <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
              <span className="u-mono">all</span> is its own key rather than a shortcut for the six: every
              phase is resumable and idempotent, so running it after a completed phase re-reads that
              phase&rsquo;s checkpoint and costs a re-parse instead of a wrong answer.
            </span>
          </OpsSection>
        )}

        {data && (
          <div className="flex flex-wrap items-start gap-6">
            <div className="min-w-0 flex-1 basis-[380px]">
              <OpsSection
                title="Genome coverage"
                note="The route returns counts and declines the division. Every ratio here is printed as numerator / denominator as well as a percent, and the denominator is named beside it."
              >
                <dl
                  className="grid gap-x-4 gap-y-2"
                  style={{
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-card)',
                    background: 'var(--bg-surface)',
                    padding: 'var(--space-3)',
                    gridTemplateColumns: '1fr auto',
                  }}
                >
                  {(
                    [
                      ['titles', data.genome.titles],
                      ['movies', data.genome.movies],
                      ['enriched', data.genome.enriched],
                      ['with_vector', data.genome.with_vector],
                      ['enriched_with_vector', data.genome.enriched_with_vector],
                    ] as const
                  ).map(([key, value]) => (
                    <div key={key} className="contents">
                      <dt className="u-mono" style={{ color: 'var(--text-secondary)' }}>
                        {key}
                      </dt>
                      <dd className="u-mono" style={{ color: 'var(--text-primary)', textAlign: 'right' }}>
                        {count(value)}
                      </dd>
                    </div>
                  ))}
                </dl>

                <dl className="flex flex-col gap-2">
                  <div>
                    <dt style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
                      Enriched titles carrying a genome vector
                    </dt>
                    <dd className="u-mono" style={{ color: 'var(--text-primary)' }}>
                      {ratio(data.genome.enriched_with_vector, data.genome.enriched)}
                    </dd>
                    <dd style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                      of enriched titles — the denominator is <span className="u-mono">genome.enriched</span>,
                      not the catalog.
                    </dd>
                  </div>
                  <div>
                    <dt style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
                      Every title carrying a genome vector
                    </dt>
                    <dd className="u-mono" style={{ color: 'var(--text-primary)' }}>
                      {ratio(data.genome.with_vector, data.genome.titles)}
                    </dd>
                    <dd style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                      of every title in the catalog — the denominator is{' '}
                      <span className="u-mono">genome.titles</span>.
                    </dd>
                  </div>
                  <div>
                    <dt style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
                      Enriched share of the catalog
                    </dt>
                    <dd className="u-mono" style={{ color: 'var(--text-primary)' }}>
                      {ratio(data.genome.enriched, data.genome.titles)}
                    </dd>
                    <dd style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                      of every title in the catalog — the denominator is{' '}
                      <span className="u-mono">genome.titles</span>.
                    </dd>
                  </div>
                </dl>

                {data.genome.revisions.length > 0 ? (
                  <div className="flex flex-col gap-1">
                    <span className="u-eyebrow">Vectors by revision</span>
                    {data.genome.revisions.map((revision) => (
                      <span key={revision.revision} className="u-mono">
                        {revision.revision} · {count(revision.vectors)} vectors
                      </span>
                    ))}
                    {data.genome.revisions.length > 1 && (
                      <span style={{ font: 'var(--text-body-xs)', color: 'var(--warn-text)' }}>
                        More than one revision is present, so there is no single revision to ask for.
                      </span>
                    )}
                  </div>
                ) : (
                  <StateBlock kind="never" meta="genome.revisions: []">
                    No genome revision has ever been loaded, so no vector can be attributed to one.
                  </StateBlock>
                )}
              </OpsSection>
            </div>

            <div className="min-w-0 flex-1 basis-[320px]">
              <OpsSection title="Vocabulary">
                <div
                  className="flex flex-col gap-2"
                  style={{
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-card)',
                    background: 'var(--bg-surface)',
                    padding: 'var(--space-4)',
                  }}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={data.vocabulary.state === 'named' ? 'good' : 'warn'}>
                      {data.vocabulary.state}
                    </Badge>
                    <span className="u-mono" style={{ color: 'var(--text-primary)' }}>
                      {data.vocabulary.tags === null ? 'tags: null' : `${count(data.vocabulary.tags)} tags`}
                    </span>
                  </div>
                  {data.vocabulary.detail !== null && (
                    <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
                      {data.vocabulary.detail}
                    </span>
                  )}
                  <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                    The five states are <span className="u-mono">no_vectors</span>,{' '}
                    <span className="u-mono">mixed_releases</span>, <span className="u-mono">not_loaded</span>
                    , <span className="u-mono">mismatched</span> and <span className="u-mono">named</span> —
                    each means something different and none of them means &ldquo;empty&rdquo;.
                  </span>
                </div>
              </OpsSection>
            </div>
          </div>
        )}
      </div>

      {pending !== null && pending !== 'all' && (
        <ConfirmDialog
          open
          title={`Run the ${pending.label} phase?`}
          facts={phaseFacts(pending)}
          confirmLabel={runFor(pending.phase)?.status === 'failed' ? 'Resume import' : 'Start import'}
          loading={start.isPending}
          onCancel={() => setPending(null)}
          onConfirm={() => trigger(pending)}
        >
          These are real public datasets, re-downloaded on each cold run. IMDb regenerates its dumps daily, so
          a rerun picks up a new revision rather than the same bytes.
        </ConfirmDialog>
      )}

      {pending === 'all' && (
        <ConfirmDialog
          open
          title="Run every bootstrap phase?"
          facts={ALL_FACTS}
          confirmLabel="Start all phases"
          loading={start.isPending}
          onCancel={() => setPending(null)}
          onConfirm={() => trigger('all')}
        >
          Phases run in the listed order and later ones refuse until earlier ones complete.
        </ConfirmDialog>
      )}
    </>
  )
}
