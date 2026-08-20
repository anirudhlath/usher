import {
  Badge,
  Button,
  ChartPanel,
  DataTable,
  Icon,
  Problem,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  type Column,
  type ProblemDocument,
} from '@/design-system'
import { BackendWork, OpsHeader, OpsSection, Tri } from '@/app/shells/OperatorShell'
import { useViewport } from '@/app/useViewport'
import { UsherProblem, fieldErrors, readinessFromError, useReadiness } from '@/api'
import { useProblemTrace } from '@/features/shared/trace'

/**
 * Pipeline — **the whole screen is REQUIRES BACKEND WORK** (patterns.md §15,
 * items 3 and 4), and it is the highest-value design in the console.
 *
 * Every mutating admin action answers `202 {kind, key}` and *there is no route
 * that reads a key, lists the queue, or releases a parked job* — those actions
 * exist only in the CLI. So every receipt in this console points here, this
 * screen is where the evidence would appear, and until the four routes exist it
 * has to say so out loud rather than draw a queue out of nothing.
 *
 * What it does show is real: the nine job kinds and the four priorities are
 * documented vocabulary, and `GET /health/ready` genuinely reports whether the
 * worker lane that drains the queue is running. Showing the vocabulary is
 * honest; showing invented counts is not, so every place a number would go
 * carries `StateBlock kind="never"` or a never-fired panel instead.
 */
export default function Pipeline() {
  const { phone } = useViewport()
  const traceOf = useProblemTrace()
  const readiness = useReadiness()
  // A 503 from readiness is a *state*, not a failure: the body is still a
  // readiness document and names the lane that is down.
  const degraded = readinessFromError(readiness.error)
  const lanes = readiness.data ?? degraded

  return (
    <>
      <OpsHeader
        title="Pipeline"
        subtitle="Queue depth by kind, what is in flight, and parked jobs with the error that stopped them. None of it is reachable from this API yet."
      />
      <div className="u-ops__body">
        <BackendWork routes={MISSING_ROUTES}>
          Nothing on this screen can be built today. A 202 hands back{' '}
          <span className="u-mono">{'{kind, key}'}</span> and there is no route that reads a key, lists the
          queue, or releases a parked job — those actions exist only in the CLI. The four routes above are the
          whole ask, and they are worth more than any other addition to the admin surface.
        </BackendWork>

        {/* The panels keep their metric names, because that is the one thing an
            operator can act on while the number does not exist: the series is
            findable in Grafana even though this console cannot read it. */}
        <OpsSection
          title="The three numbers"
          note="Every panel prints its metric name, so the same series is findable in Grafana even though nothing here can read it."
        >
          <div style={{ display: 'flex', gap: 'var(--space-3)', flexWrap: 'wrap' }}>
            <ChartPanel title="Queued" metric="usher.jobs.queued" state="never" />
            <ChartPanel title="Parked" metric="usher.jobs.parked" state="never" />
            <ChartPanel title="Throughput" metric="usher.jobs.duration" state="never" />
          </div>
        </OpsSection>

        <OpsSection
          title="Queue by kind"
          note="Priorities are DEMAND 100 · VISIBLE 80 · NEW 50 · BACKFILL 20, with exponential backoff and jitter on retry."
        >
          <StateBlock
            kind="never"
            title="No depth has ever been read"
            meta="GET /admin/jobs/stats · GET /admin/jobs?kind=&state= — neither route exists"
          >
            <span className="u-mono">queued</span>, <span className="u-mono">running</span>,{' '}
            <span className="u-mono">parked</span> and <span className="u-mono">p95</span> would be four more
            columns on this table, one set per kind. Nothing answers them, so the counts are absent rather
            than zero — a zero here would claim an empty queue, which is a measurement nobody has taken.
          </StateBlock>
          <DataTable
            caption="The nine job kinds"
            keyField="kind"
            rows={JOB_KINDS}
            asCards={phone}
            columns={KIND_COLUMNS}
          />
          <div style={{ display: 'flex', gap: 'var(--space-2x)', flexWrap: 'wrap' }}>
            {PRIORITIES.map((priority) => (
              <Badge key={priority.name} tone="neutral" mono>
                {priority.name} {priority.value}
              </Badge>
            ))}
          </div>
        </OpsSection>

        <OpsSection
          title="Parked jobs"
          note="Five failed attempts and it stops trying. Releasing puts the job back at its original priority and resets the count."
          action={
            <Button size="sm" variant="secondary" disabled>
              Release all parked jobs
            </Button>
          }
        >
          <StateBlock
            kind="never"
            title="No parked job has ever been listed"
            meta="GET /admin/jobs?state=parked · POST /admin/jobs/{id}/release — neither route exists"
          >
            The designed table is <span className="u-mono">kind</span>,{' '}
            <span className="u-mono">subject</span>, <span className="u-mono">attempts</span>,{' '}
            <span className="u-mono">error</span> and <span className="u-mono">last_tried</span>, with the
            last error verbatim and a per-row release. The release control above is disabled because there is
            no route behind it; until there is, <span className="u-mono">usher sync-status</span> in the CLI
            is the only way to see a parked job, and releasing one is a CLI action too.
          </StateBlock>
        </OpsSection>

        <OpsSection
          title="Worker lane"
          note="Not the queue. GET /health/ready is the only route that says anything about job processing today, and it says one thing: whether the lane that drains the queue is running."
        >
          {readiness.isPending ? (
            <SkeletonRegion busy label="Loading the worker lane …">
              <Skeleton shape="block" height={28} width="40%" />
            </SkeletonRegion>
          ) : lanes === null ? (
            <Problem
              problem={asProblem(readiness.error)}
              {...traceOf(readiness.error)}
              icon={<Icon name="x-circle" size={20} />}
              onRetry={() => {
                void readiness.refetch()
              }}
            />
          ) : (
            <div
              style={{
                display: 'flex',
                gap: 'var(--space-4)',
                alignItems: 'center',
                flexWrap: 'wrap',
              }}
            >
              <Tri
                value={lanes.lanes.worker}
                labels={['worker running', 'worker not running', 'not reported']}
              />
              <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)', maxWidth: '76ch' }}>
                Reported, not gated: a deployment with no worker still answers reads. A lane that is not
                running means nothing drains the queue — and this screen cannot tell you how much has piled up
                behind it.
              </span>
            </div>
          )}
        </OpsSection>

        <OpsSection
          title="Where every receipt points"
          note="A 202 receipt in this console links here. The pointer is honest; the surface is not ready."
        >
          <StateBlock kind="never" title="A key cannot be looked up" meta="GET /admin/jobs/{key} — no route">
            Every mutating admin action answers <span className="u-mono">202 {'{kind, key}'}</span> and hands
            you a key that nothing can query. That is why a receipt in this console persists until it is
            dismissed and prints its key in mono: the toast is the only copy, and an operator pastes it into a
            log search. When <span className="u-mono">GET /admin/jobs/&#123;key&#125;</span> exists, this is
            the surface that resolves it.
          </StateBlock>
        </OpsSection>
      </div>
    </>
  )
}

/** §15 items 3 and 4, printed in mono where anybody reading the screen can see them. */
const MISSING_ROUTES =
  'GET /admin/jobs?kind=&state= · GET /admin/jobs/{key} · GET /admin/jobs/stats · POST /admin/jobs/{id}/release'

interface JobKindRow extends Record<string, unknown> {
  readonly kind: string
  readonly does: string
}

/**
 * `JobKind`'s nine members, in the enum's own order. Real vocabulary — this is
 * what the queue deduplicates on together with a key — and deliberately without
 * a priority column: a priority belongs to an *enqueued job*, not to a kind, so
 * a table pairing them would state a fact the system does not hold.
 */
const JOB_KINDS: JobKindRow[] = [
  { kind: 'match', does: 'Attach a file a walk reported to a catalog title.' },
  { kind: 'enrich', does: 'Fill a title from the metadata provider.' },
  { kind: 'watch_history', does: 'Read a source’s watch state into the catalog.' },
  { kind: 'index', does: 'Rebuild a title’s search document and its embedding.' },
  { kind: 'derive', does: 'Re-derive people, credits and collections.' },
  { kind: 'curate', does: 'Generate this household’s LLM-curated shelves.' },
  { kind: 'watch_writeback', does: 'Push watch state back out to the source.' },
  { kind: 'sync', does: 'Walk a source’s library, full or delta.' },
  { kind: 'bootstrap', does: 'Run one bulk-import phase.' },
]

const KIND_COLUMNS: Column<JobKindRow>[] = [
  { key: 'kind', header: 'Kind', mono: true },
  { key: 'does', header: 'What it does' },
]

/** The four priority tiers, with their real numeric values. */
const PRIORITIES = [
  { name: 'DEMAND', value: 100 },
  { name: 'VISIBLE', value: 80 },
  { name: 'NEW', value: 50 },
  { name: 'BACKFILL', value: 20 },
] as const

/** See `Review.tsx` — `detail` verbatim, `code` and `status` always rendered. */
function asProblem(error: unknown): ProblemDocument {
  if (!(error instanceof UsherProblem)) {
    return {
      status: 0,
      title: 'The request never reached the server.',
      detail: String(error),
    }
  }
  const errors = fieldErrors(error.errors).map((one) => ({ loc: [one.field], msg: one.message }))
  return {
    status: error.status,
    detail: error.detail,
    ...(error.knownCode === null ? {} : { code: error.knownCode }),
    ...(error.instance === undefined ? {} : { instance: error.instance }),
    ...(error.retryAfter === null ? {} : { retry_after: error.retryAfter }),
    ...(errors.length === 0 ? {} : { errors }),
  }
}
