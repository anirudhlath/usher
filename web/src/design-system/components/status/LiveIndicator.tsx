import clsx from 'clsx'

/**
 * SSE stream state for /events. The bus is in-process and lossy by design: events are dropped when
 * nobody is listening, on buffer overflow, and on restart. The UI must be fully correct if zero
 * events ever arrive — live updates are delight, never the mechanism.
 *
 * A heartbeat comment arrives every 20 s, so an idle stream is HEALTHY. `idle` says so in words
 * rather than looking broken.
 */
export interface LiveIndicatorProps {
  state?: 'connected' | 'idle' | 'reconnecting' | 'off'
  /** e.g. "14:22" — only shown in idle state. */
  lastEventAt?: string
  /** Overrides the trailing clause, e.g. "resync_required — refetching". */
  detail?: string
}

export type LiveState = NonNullable<LiveIndicatorProps['state']>

const TEXT = {
  connected: 'Live',
  idle: 'Live · quiet',
  reconnecting: 'Reconnecting…',
  off: 'Not connected',
} as const satisfies Record<LiveState, string>

/**
 * SSE connection state. Quiet is healthy — a stream with nothing on it is not broken,
 * so `idle` is drawn in the same neutral tone as `connected` and never as a warning.
 *
 * patterns.md §7 announces **only** `reconnecting` and `resync_required`, never individual
 * frames — so the live region is a separate visually-hidden node that is empty in every
 * other state. A resync is surfaced by the consumer as `state="reconnecting"` with
 * `detail="resync_required — refetching"`, which is then the sentence that is read; a
 * frame count riding on `connected` or `idle` is drawn and never announced.
 */
export function LiveIndicator({ state = 'idle', lastEventAt, detail }: LiveIndicatorProps) {
  const announcement = state === 'reconnecting' ? [TEXT.reconnecting, detail].filter(Boolean).join(' ') : ''

  return (
    <span className={clsx('u-live', `u-live--${state}`)}>
      <span className="u-live__dot" aria-hidden="true" />
      <span className="u-live__label">{TEXT[state]}</span>
      {detail ? (
        <span className="u-live__detail">{detail}</span>
      ) : state === 'idle' ? (
        <span className="u-live__note">nothing has changed{lastEventAt ? ` since ${lastEventAt}` : ''}</span>
      ) : null}
      <span className="u-visually-hidden" role="status" aria-live="polite">
        {announcement}
      </span>
    </span>
  )
}
