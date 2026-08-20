import clsx from 'clsx'

/**
 * Progress for work the server refuses to put a denominator on: bootstrap runs and syncs.
 * `GET /admin/bootstrap/status` returns rows_seen / rows_written / position and no total, so this
 * shows throughput and position and says outright that there is no completion estimate.
 *
 * `heartbeat_at` is the only way to tell a live import from a dead process: older than 120 s reads
 * "Stalled?" — with the question mark, because that inference is the design's, not the API's.
 */
export interface CursorProgressProps {
  dataset: string
  phase: string
  status?: 'running' | 'completed' | 'failed'
  rowsSeen?: number
  rowsWritten?: number
  /** Derived client-side from two polls. Null until there are two. */
  rowsPerSecond?: number | null
  /** The durable checkpoint, printed verbatim in mono. */
  position?: string
  elapsed?: string
  heartbeatAgoSeconds?: number | null
  revision?: string
  /** `error` from the run record, shown verbatim. A failed run is a normal state. */
  error?: string
  stalledThresholdSeconds?: number
}

export type CursorStatus = NonNullable<CursorProgressProps['status']>

/** `—` is the only honest rendering of a number nobody has yet: there is no zero to fall back on. */
function n(value: number | null | undefined): string {
  return value == null ? '—' : value.toLocaleString('en-US')
}

/**
 * Bootstrap/sync progress without a denominator (patterns.md §8). The API returns counts and
 * refuses percentages, so this reports throughput and position instead of faking completion.
 *
 * Time is injected rather than read from a clock: `heartbeatAgoSeconds` is the age the caller's
 * poll already computed, which is what makes the 120 s threshold exercisable at 119 and 121
 * without faking timers.
 *
 * ARIA (§12): `role="progressbar"` with **no `aria-valuenow`** — there is no value to state —
 * and `aria-valuetext` in words, which says outright that no estimate exists.
 */
export function CursorProgress({
  dataset,
  phase,
  status = 'running',
  rowsSeen = 0,
  rowsWritten = 0,
  rowsPerSecond,
  position,
  elapsed,
  heartbeatAgoSeconds,
  revision,
  error,
  stalledThresholdSeconds = 120,
}: CursorProgressProps) {
  const stalled =
    status === 'running' && heartbeatAgoSeconds != null && heartbeatAgoSeconds > stalledThresholdSeconds

  const counts = `${n(rowsSeen)} rows seen, ${n(rowsWritten)} written`
  const valueText = stalled
    ? `${counts}. No heartbeat for over ${stalledThresholdSeconds} s. Stalled?`
    : `${counts}. No completion estimate is available.`

  return (
    <div className="u-cursor">
      <div className="u-cursor__head">
        <span className="u-cursor__ds">{dataset}</span>
        <span className="u-cursor__phase">
          {phase}
          {revision ? ` · rev ${revision}` : ''}
        </span>
        {stalled ? (
          <span className="u-cursor__status u-cursor__status--stalled">Stalled?</span>
        ) : (
          <span className={clsx('u-cursor__status', `u-cursor__status--${status}`)}>{status}</span>
        )}
      </div>

      {status === 'running' && (
        <div
          className={clsx('u-cursor__track', stalled && 'u-cursor__stalled')}
          role="progressbar"
          aria-label={`Importing ${dataset}`}
          aria-valuetext={valueText}
        >
          <span className="u-cursor__sweep" />
        </div>
      )}

      <div className="u-cursor__grid">
        <span>
          <span className="u-cursor__k">rows seen</span>
          <span className="u-cursor__v">{n(rowsSeen)}</span>
        </span>
        <span>
          <span className="u-cursor__k">rows written</span>
          <span className="u-cursor__v">{n(rowsWritten)}</span>
        </span>
        <span>
          <span className="u-cursor__k">rows / sec</span>
          <span className="u-cursor__v">{n(rowsPerSecond)}</span>
        </span>
        <span>
          <span className="u-cursor__k">elapsed</span>
          <span className="u-cursor__v">{elapsed ?? '—'}</span>
        </span>
        <span>
          <span className="u-cursor__k">heartbeat</span>
          <span className={clsx('u-cursor__v', stalled && 'u-cursor__v--warn')}>
            {heartbeatAgoSeconds == null ? '—' : `${heartbeatAgoSeconds}s ago`}
          </span>
        </span>
        {position ? (
          <span>
            <span className="u-cursor__k">position</span>
            <span className="u-cursor__v">{position}</span>
          </span>
        ) : null}
      </div>

      {stalled ? (
        <span className="u-cursor__note u-cursor__note--warn">
          No heartbeat for over {stalledThresholdSeconds} s. The import may have died; it is resumable from
          this position.
        </span>
      ) : null}
      {error ? <span className="u-cursor__note u-cursor__note--bad">{error}</span> : null}
      {!stalled && !error && status === 'running' ? (
        <span className="u-cursor__note">
          No completion estimate — the server reports a cursor, not a percentage.
        </span>
      ) : null}
    </div>
  )
}
