/**
 * Overview — the screen an operator opens first.
 *
 * Three rules carry this surface, and each of them is a correctness rule rather
 * than a layout preference:
 *
 * · **Readiness is shown with a cause.** `/health/ready` answers
 *   `{status, checks, lanes}`, and the word `degraded` is the *symptom*. The
 *   headline names the check that failed — "Migrations are behind" — and the
 *   API's own status word is kept in the mono meta line beside the HTTP code,
 *   so nothing is hidden and nothing is restated as a diagnosis.
 * · **A 503 is information, not an error.** `/health/ready` is one of two
 *   routes exempt from the RFC 9457 envelope: the 503 carries the *same*
 *   readiness document as the 200 and names the failing check. `useReadiness`
 *   already sets `retry: false`; `readinessFromError` pulls the document back
 *   out of the rejection, and only a 503 that is *not* a readiness document
 *   reaches `Problem`.
 * · **Lanes are reported, not gated.** `lanes.push` and `lanes.worker` are
 *   deliberately outside the readiness computation — a process is not taken out
 *   of a load balancer because Emby is unreachable — so this screen states them
 *   and says outright that they are not gated on.
 *
 * Nothing here fires a source probe. `GET /admin/sources/{id}/status` builds an
 * adapter and calls `verify()` against a real media server at 1–5 s a probe, so
 * it is an explicit action on the Sources screen and never a page load.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Badge,
  Button,
  ChartPanel,
  CursorProgress,
  DataTable,
  Icon,
  Problem,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  type Column,
  type IconName,
  type ProblemDocument,
} from '@/design-system'
import { BackendWork, OpsHeader, OpsSection, Tri } from '@/app/shells/OperatorShell'
import { ROUTES } from '@/app/routes'
import { useViewport } from '@/app/useViewport'
import {
  UsherProblem,
  readinessFromError,
  useBootstrapStatus,
  useReadiness,
  useSources,
  useUnmatched,
  type ImportRun,
  type ReadinessResponse,
  type SourceResponse,
} from '@/api'
import { useProblemTrace } from '@/features/shared/trace'

/* ------------------------------------------------------------------ shared */

/**
 * An `UsherProblem` as the design system's document. Built by spread rather
 * than by assigning `undefined`, because `exactOptionalPropertyTypes` makes an
 * explicit `undefined` a different thing from an absent key.
 */
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
  // A transport failure has no status and no body: `fetch` rejected and the
  // request never left. Recording it as 0 keeps "the request never left" and
  // "the server said nothing" from looking identical.
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
 * `rows/sec` derived client-side from two polls, and `null` until there are two
 * (patterns.md §8). The server reports a cursor and no rate, so a number here
 * before the second poll would be invented.
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

/* --------------------------------------------------------------- readiness */

/** The failing check, named. `degraded` is the symptom; this is the cause. */
function readinessHeadline(doc: ReadinessResponse): string {
  if (!doc.checks.database) return 'The database is unreachable'
  if (!doc.checks.migrations) return 'Migrations are behind'
  return 'Ready'
}

/**
 * One sentence per failing check.
 *
 * The handoff's copy said "two revisions behind"; the API sends a boolean, so
 * the count is dropped rather than invented — a fabricated number is the exact
 * failure this product is organised against.
 */
function readinessCause(doc: ReadinessResponse): ReactNode {
  if (!doc.checks.database) {
    return 'Usher cannot reach its database. Nothing reads and nothing writes until it can.'
  }
  if (!doc.checks.migrations) {
    return (
      <>
        Migrations are behind the running code. Reads are fine; anything that writes may fail. Run{' '}
        <span className="u-mono">alembic upgrade head</span> and restart.
      </>
    )
  }
  return null
}

function ReadinessCard({ doc, httpStatus }: { doc: ReadinessResponse; httpStatus: number }) {
  const degraded = !doc.checks.database || !doc.checks.migrations
  const cause = readinessCause(doc)

  return (
    <div
      className="flex flex-1 basis-[340px] flex-col gap-3"
      style={{
        border: `1px solid ${degraded ? 'var(--warn-border)' : 'var(--border-default)'}`,
        background: degraded ? 'var(--warn-quiet)' : 'var(--bg-surface)',
        borderRadius: 'var(--radius-card)',
        padding: 'var(--space-4)',
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Icon name={degraded ? 'alert-triangle' : 'heart-pulse'} size={20} />
        <span style={{ font: 'var(--text-heading-sm)', color: 'var(--text-primary)' }}>
          {readinessHeadline(doc)}
        </span>
        <span className="ml-auto" style={{ font: 'var(--text-mono-xs)', color: 'var(--text-muted)' }}>
          status {doc.status} · HTTP {httpStatus} · /health/ready
        </span>
      </div>

      {cause && <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>{cause}</span>}

      <dl
        className="grid items-center gap-x-4 gap-y-2"
        style={{ gridTemplateColumns: 'auto 1fr', justifyItems: 'start' }}
      >
        <dt className="u-mono" style={{ color: 'var(--text-muted)' }}>
          checks.database
        </dt>
        <dd>
          <Badge tone={doc.checks.database ? 'good' : 'bad'}>
            {doc.checks.database ? 'ok' : 'unreachable'}
          </Badge>
        </dd>
        <dt className="u-mono" style={{ color: 'var(--text-muted)' }}>
          checks.migrations
        </dt>
        <dd>
          <Badge tone={doc.checks.migrations ? 'good' : 'bad'}>
            {doc.checks.migrations ? 'ok' : 'behind'}
          </Badge>
        </dd>
        {/* Reported, never gated on — so these two sit below a hairline of
            their own and carry no good/bad hue that would read as a gate. */}
        <dt className="u-mono" style={{ color: 'var(--text-muted)' }}>
          lanes.worker
        </dt>
        <dd>
          <Badge tone="neutral" icon={<Icon name={doc.lanes.worker ? 'radio' : 'circle-dashed'} />}>
            {doc.lanes.worker ? 'running' : 'not running'}
          </Badge>
        </dd>
        <dt className="u-mono" style={{ color: 'var(--text-muted)' }}>
          lanes.push
        </dt>
        <dd>
          <Badge tone="neutral" icon={<Icon name={doc.lanes.push.length > 0 ? 'radio' : 'circle-dashed'} />}>
            {doc.lanes.push.length > 0 ? doc.lanes.push.join(', ') : 'no push lane is running'}
          </Badge>
        </dd>
      </dl>

      <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
        Lanes are reported, never gated on: the server stays up whether or not they are running.
      </span>
    </div>
  )
}

/* --------------------------------------------------------- needs a person */

interface Attention {
  id: string
  icon: IconName
  tone: 'warn' | 'bad'
  text: string
  meta: string
  to: string
}

/* ------------------------------------------------------------------ screen */

export default function Overview() {
  const navigate = useNavigate()
  const { phone } = useViewport()
  const traceOf = useProblemTrace()

  const readiness = useReadiness()
  const bootstrap = useBootstrapStatus({
    // patterns.md §8: status costs ~0.33 s and is uncached, so it is polled
    // only while something is running. Overview obeys the same rule Bootstrap
    // does — a background screen polling forever is the worse offender.
    refetchInterval: (query) =>
      query.state.data?.runs.some((run) => run.status === 'running') ? 10_000 : false,
  })
  const sourceList = useSources()
  const unmatched = useUnmatched()

  const runs = bootstrap.data?.runs
  const throughput = useThroughput(runs)

  // A 503 carries the same readiness document as a 200. Only a body that is not
  // a readiness document is a genuine failure.
  const fromError = readiness.error ? readinessFromError(readiness.error) : null
  const document = readiness.data ?? fromError
  const readinessStatus = readiness.data ? 200 : 503

  const running = (runs ?? []).filter((run) => run.status === 'running')
  const failed = (runs ?? []).filter((run) => run.status === 'failed')
  const unmatchedLoaded = (unmatched.data?.pages ?? []).reduce((total, page) => total + page.items.length, 0)

  const attention: Attention[] = []
  if (unmatchedLoaded > 0) {
    attention.push({
      id: 'unmatched',
      icon: 'scan-search',
      tone: 'warn',
      text: 'Files could not be matched',
      // Keyset: loaded, never a total. There is no denominator to quote.
      meta: `${unmatchedLoaded.toLocaleString('en-US')} loaded so far`,
      to: ROUTES.review,
    })
  }
  for (const run of failed) {
    attention.push({
      id: `import-${run.dataset}`,
      icon: 'database',
      tone: 'bad',
      text: `The ${run.dataset} import failed`,
      meta: run.error ?? 'no error was recorded',
      to: ROUTES.bootstrap,
    })
  }

  const sourceColumns: Column<SourceResponse>[] = [
    { key: 'name', header: 'Name' },
    { key: 'base_url', header: 'Base URL', mono: true },
    {
      key: 'supports_push',
      header: 'Push',
      render: (row) => <Tri value={row.supports_push} labels={['available', 'unavailable', 'unknown']} />,
    },
    {
      key: 'created_at',
      header: 'Added',
      mono: true,
      render: (row) => row.created_at.slice(0, 10),
    },
  ]

  return (
    <>
      <OpsHeader
        title="Overview"
        subtitle="Readiness with its cause, what is running right now, and what is waiting on a person."
      />
      <div className="u-ops__body">
        <OpsSection
          title="Readiness"
          note="Reported by /health/ready every 15 s. A 503 from this route is a state, not a failed request: it carries the same document and names the check that failed."
        >
          {readiness.isPending && (
            <SkeletonRegion
              busy
              label="Loading readiness …"
              className="flex flex-1 basis-[340px] flex-col gap-3"
            >
              {/* The shape of the card that lands, never a spinner: a skeleton
                  reads as "arriving" and a spinner reads as "restarting". */}
              <Skeleton shape="text" lines={3} />
            </SkeletonRegion>
          )}

          {!readiness.isPending && document === null && (
            <Problem
              scale="panel"
              problem={problemOf(readiness.error)}
              {...traceOf(readiness.error)}
              onRetry={() => void readiness.refetch()}
              icon={<Icon name="x-circle" size={20} />}
            />
          )}

          <div className="flex flex-wrap items-stretch gap-3">
            {document && <ReadinessCard doc={document} httpStatus={readinessStatus} />}
            <ChartPanel
              title="Catalog"
              metric="usher.catalog.titles"
              loading={bootstrap.isPending}
              value={(bootstrap.data?.titles ?? 0).toLocaleString('en-US')}
              sub={
                bootstrap.data
                  ? `titles · ${bootstrap.data.genome.enriched.toLocaleString('en-US')} enriched · counts, not shares`
                  : 'titles · counts, not shares'
              }
            />
            <ChartPanel
              title="Titles with a genome vector"
              metric="usher.genome.vectors"
              loading={bootstrap.isPending}
              state={bootstrap.data?.genome.with_vector === 0 ? 'zero' : 'ok'}
              value={(bootstrap.data?.genome.with_vector ?? 0).toLocaleString('en-US')}
              sub={
                bootstrap.data
                  ? `of ${bootstrap.data.genome.titles.toLocaleString('en-US')} titles in the catalog — the denominator is named because the route declines to divide`
                  : 'the route returns counts and declines to divide'
              }
            />
          </div>
        </OpsSection>

        <OpsSection
          title="Running now"
          note="Bootstrap and sync report a cursor, not a percentage. A heartbeat older than 120 s is the only signal that a run has died."
        >
          {bootstrap.isError && (
            <Problem
              scale="panel"
              problem={problemOf(bootstrap.error)}
              {...traceOf(bootstrap.error)}
              onRetry={() => void bootstrap.refetch()}
              icon={<Icon name="x-circle" size={20} />}
            />
          )}
          {bootstrap.data && running.length > 0 && (
            <div className="flex flex-col gap-2">
              {running.map((run) => {
                const elapsed = elapsedOf(run)
                return (
                  <CursorProgress
                    key={run.dataset}
                    dataset={run.dataset}
                    phase="bootstrap"
                    status="running"
                    rowsSeen={run.rows_seen}
                    rowsWritten={run.rows_written}
                    rowsPerSecond={throughput.get(run.dataset) ?? null}
                    position={String(run.position)}
                    revision={run.revision}
                    heartbeatAgoSeconds={secondsSince(run.heartbeat_at)}
                    {...(elapsed === undefined ? {} : { elapsed })}
                  />
                )
              })}
            </div>
          )}
          {bootstrap.data && running.length === 0 && runs?.length === 0 && (
            <StateBlock kind="never" meta="runs: []">
              No import has ever run on this deployment, so there is no checkpoint to resume from.
            </StateBlock>
          )}
          {bootstrap.data && running.length === 0 && (runs?.length ?? 0) > 0 && (
            <StateBlock kind="empty" title="Nothing is running" meta="no run reports status: running">
              Every recorded run has finished or failed. Nothing is being polled — status costs about 0.33 s
              and is uncached, so it is only asked for while a run is live.
            </StateBlock>
          )}
        </OpsSection>

        <div className="flex flex-wrap items-start gap-6">
          <div className="min-w-0 flex-1 basis-[420px]">
            <OpsSection
              title="Sources"
              note="A probe is a real round trip to the media server and costs 1–5 s, so nothing on this screen fires one. Probe from Sources."
              action={
                <Button size="sm" variant="secondary" onClick={() => navigate(ROUTES.sources)}>
                  Open sources
                </Button>
              }
            >
              {sourceList.isError ? (
                <Problem
                  scale="panel"
                  problem={problemOf(sourceList.error)}
                  {...traceOf(sourceList.error)}
                  onRetry={() => void sourceList.refetch()}
                  icon={<Icon name="server-off" size={20} />}
                />
              ) : (
                <DataTable
                  caption="Configured sources"
                  keyField="id"
                  rows={sourceList.data ?? []}
                  columns={sourceColumns}
                  asCards={phone}
                  emptyMessage="No media server is connected. The catalog is still browsable; a source is what makes a title playable."
                  onRowClick={() => navigate(ROUTES.sources)}
                />
              )}
            </OpsSection>
          </div>

          <div className="min-w-0 flex-1 basis-[340px]">
            <OpsSection
              title="Needs a person"
              note="Everything here is waiting on a decision, not on a machine."
            >
              {unmatched.isError && (
                <Problem
                  scale="panel"
                  problem={problemOf(unmatched.error)}
                  {...traceOf(unmatched.error)}
                  onRetry={() => void unmatched.refetch()}
                  icon={<Icon name="x-circle" size={20} />}
                />
              )}
              {attention.length > 0 ? (
                <div className="flex flex-col gap-2">
                  {attention.map((item) => (
                    <button
                      key={item.id}
                      type="button"
                      className="flex items-center gap-2 text-left"
                      onClick={() => navigate(item.to)}
                      style={{
                        appearance: 'none',
                        padding: 'var(--space-2x) var(--space-3)',
                        border: '1px solid var(--border-default)',
                        borderRadius: 'var(--radius-card)',
                        background: 'var(--bg-surface)',
                        font: 'var(--text-body-sm)',
                        color: 'var(--text-primary)',
                      }}
                    >
                      <span className="flex" style={{ color: `var(--${item.tone}-text)` }}>
                        <Icon name={item.icon} size={16} />
                      </span>
                      <span className="flex min-w-0 flex-col">
                        <span>{item.text}</span>
                        <span className="u-mono" style={{ color: 'var(--text-muted)' }}>
                          {item.meta}
                        </span>
                      </span>
                      <span className="ml-auto flex" style={{ color: 'var(--text-muted)' }}>
                        <Icon name="chevron-right" size={16} />
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                !unmatched.isError &&
                !bootstrap.isError && (
                  <StateBlock
                    kind="empty"
                    title="Nothing is waiting on a person"
                    meta="unmatched: 0 loaded · runs: none failed"
                  >
                    The review queue is empty and no import has failed. This list is built from those two
                    facts and from nothing else.
                  </StateBlock>
                )
              )}
            </OpsSection>
          </div>
        </div>

        <OpsSection
          title="Recent activity"
          note="Sync runs are recorded per attempt with cursors and errors — in the database and the CLI only."
        >
          <BackendWork routes="GET /admin/sources/{id}/runs">
            A timeline of the last syncs, imports and resolutions belongs here and there is no HTTP route that
            can read it. Everything needed already exists in <span className="u-mono">sync_runs</span> and{' '}
            <span className="u-mono">import_runs</span>; it has never been exposed.
          </BackendWork>
        </OpsSection>
      </div>
    </>
  )
}
