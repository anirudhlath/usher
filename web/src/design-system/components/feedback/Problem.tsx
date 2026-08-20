import { useCallback, useEffect, useRef, useState, type ReactElement, type ReactNode } from 'react'
import clsx from 'clsx'
import { Button } from '../actions'

/**
 * The one error component, at four scales: `inline` (a field hint), `panel` (a failed section),
 * `page` (a failed route), `toast` (a background failure).
 *
 * The API's error set is closed — seven codes — and each has exactly one correct recovery:
 * · not_found (404)          → back / search. No retry.
 * · validation_failed (422)  → field errors from errors[].loc/.msg.
 * · method_not_allowed (405) → generic. Developer error.
 * · invalid_cursor (400)     → NEVER shown. The list silently restarts from the top.
 * · source_unavailable (503) → retryable, honour Retry-After.
 * · not_playable (409)       → no retry button. There is no playable file.
 * · ticket_invalid (404)     → one tap re-requests and plays.
 *
 * `detail` is prose that may be reworded at any release: never parse it, always show it — it is
 * often the only thing that tells an operator what happened.
 */

/**
 * The vocabulary is declared here rather than imported from `src/api/` on purpose: the
 * design-system boundary forbids that import, and a component that knew the transport would not be
 * reusable. `src/api/problem.ts` declares the same seven members; they are checked against each
 * other by the feature layer that maps one onto the other.
 */
export type ProblemCode =
  | 'not_found'
  | 'validation_failed'
  | 'method_not_allowed'
  | 'invalid_cursor'
  | 'source_unavailable'
  | 'not_playable'
  | 'ticket_invalid'

export type ProblemScale = 'inline' | 'panel' | 'page' | 'toast'

export interface ProblemDocument {
  type?: string
  title?: string
  status?: number
  code?: ProblemCode
  detail?: string
  instance?: string
  errors?: { loc?: (string | number)[]; msg: string }[]
  retry_after?: number
}

export interface ProblemProps {
  problem: ProblemDocument
  scale?: ProblemScale
  tone?: 'bad' | 'warn'
  /**
   * Widened from the handoff's `() => void` so the component can disable the control *while the
   * retry is in flight* (patterns.md §3). A `() => void` is still assignable; a handler that
   * returns a promise gets the pending state for free.
   */
  onRetry?: () => void | Promise<void>
  retryLabel?: string
  actions?: ReactNode
  /**
   * Correlated trace id from the response. The link into Tempo is what makes this a real console.
   *
   * `null` is accepted alongside `undefined` because that is what the transport actually produces:
   * `UsherProblem.traceId` is `string | null` (an absent or malformed `traceresponse` header), and
   * under `exactOptionalPropertyTypes` a caller passing `null` to a `string | undefined` prop does
   * not compile. Widening here is what keeps every call site from writing
   * `...(id ? { traceId: id } : {})`; `Boolean(null)` is `false`, so the behaviour is unchanged.
   */
  traceId?: string | null
  /**
   * Where the trace lives. The design system cannot know this deployment's Tempo base URL, so the
   * URL arrives as a prop; `onOpenTrace` is the callback form for a client-side router or an
   * analytics hook. patterns.md §3 requires one of the two whenever `traceId` is present.
   *
   * **`null` is the configured-nothing case and it renders no anchor at all.** `useTraceUrl()`
   * returns `null` when this deployment has no `tempoUrl`, and a link to `""` would navigate to
   * the current page — a control that costs a click to discover does nothing. Absent, never dead:
   * the same rule the dev drawer's own trace affordance follows.
   */
  traceHref?: string | null
  onOpenTrace?: () => void
  icon?: ReactNode
}

interface ProblemTreatment {
  /** The scale this code is designed at. Used when the caller does not pass one. */
  readonly scale: ProblemScale
  /** `false` suppresses the retry control even when `onRetry` is supplied. */
  readonly retry: boolean
  /** Rendered at all. `invalid_cursor` is caught in the query layer and never reaches a screen. */
  readonly rendered: boolean
  /** Fallback title when the server sent none. */
  readonly message: string
  /** Default label for the retry control. */
  readonly retryLabel: string
}

/**
 * The closed vocabulary as an exhaustive lookup. `Record<ProblemCode, …>` is the point: adding a
 * code to the union without deciding its treatment is a compile error, so recovery stays a lookup
 * and never becomes a judgement at the call site.
 */
const TREATMENT = {
  not_found: {
    scale: 'page',
    retry: false,
    rendered: true,
    message: "We couldn't find that.",
    retryLabel: 'Try again',
  },
  validation_failed: {
    scale: 'inline',
    retry: false,
    rendered: true,
    message: 'Some fields need another look.',
    retryLabel: 'Try again',
  },
  method_not_allowed: {
    scale: 'panel',
    retry: false,
    rendered: true,
    message: 'That request is not allowed here.',
    retryLabel: 'Try again',
  },
  invalid_cursor: {
    scale: 'panel',
    retry: false,
    rendered: false,
    message: 'This list restarted.',
    retryLabel: 'Try again',
  },
  source_unavailable: {
    scale: 'panel',
    retry: true,
    rendered: true,
    message: "Couldn't reach your media server.",
    retryLabel: 'Try again',
  },
  not_playable: {
    scale: 'panel',
    retry: false,
    rendered: true,
    message: "There's no playable file for this.",
    retryLabel: 'Try again',
  },
  ticket_invalid: {
    scale: 'inline',
    retry: true,
    rendered: true,
    message: 'That link expired.',
    retryLabel: 'Play again',
  },
} as const satisfies Record<ProblemCode, ProblemTreatment>

function formatLoc(loc: (string | number)[] | undefined): string {
  return Array.isArray(loc) ? loc.join('.') : ''
}

export function Problem({
  problem,
  scale,
  tone = 'bad',
  onRetry,
  retryLabel,
  actions,
  traceId,
  traceHref,
  onOpenTrace,
  icon,
}: ProblemProps): ReactElement | null {
  const treatment = problem.code ? TREATMENT[problem.code] : undefined
  const resolvedScale: ProblemScale = scale ?? treatment?.scale ?? 'panel'

  const headingRef = useRef<HTMLHeadingElement | null>(null)
  const mounted = useRef(true)
  const [retrying, setRetrying] = useState(false)
  const [cooldown, setCooldown] = useState(problem.retry_after ?? 0)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // Retry-After arrives with the response; a new one restarts the window. Adjusted during render
  // rather than from an effect, so the button is never briefly enabled on the frame the new
  // response lands.
  const [lastRetryAfter, setLastRetryAfter] = useState(problem.retry_after)
  if (lastRetryAfter !== problem.retry_after) {
    setLastRetryAfter(problem.retry_after)
    setCooldown(problem.retry_after ?? 0)
  }

  useEffect(() => {
    if (cooldown <= 0) return undefined
    const id = setTimeout(() => setCooldown(cooldown - 1), 1000)
    return () => clearTimeout(id)
  }, [cooldown])

  /**
   * A page-scale error moves focus to its own heading: the route changed under the user and the
   * announcement has to follow. The app chrome around it is left alone — this component renders no
   * scrim and no overlay, so nobody is trapped.
   */
  useEffect(() => {
    if (resolvedScale === 'page') headingRef.current?.focus()
  }, [resolvedScale])

  const handleRetry = useCallback(() => {
    if (!onRetry || retrying) return
    setRetrying(true)
    void Promise.resolve(onRetry()).then(
      () => {
        if (mounted.current) setRetrying(false)
      },
      () => {
        if (mounted.current) setRetrying(false)
      },
    )
  }, [onRetry, retrying])

  // `invalid_cursor` is never rendered. The list restarts from the top and says nothing.
  if (treatment && !treatment.rendered) return null

  const title = problem.title ?? treatment?.message ?? 'Something went wrong.'
  const className = clsx(
    'u-problem',
    `u-problem--${resolvedScale}`,
    resolvedScale === 'panel' && tone === 'warn' && 'u-problem--panel-warn',
  )

  const showRetry = Boolean(onRetry) && treatment?.retry !== false
  /**
   * **Both halves of the condition matter, and the second is the absent-link
   * rule.** A trace id with nowhere to open it — `USHER_TEMPO_URL` unset, so
   * `useTraceUrl()` answered `null` — renders *nothing*, not an `<a href="">`,
   * which navigates to the current page. Absent and dead are different facts
   * and only the first is one this product may state.
   */
  const showTrace = Boolean(traceId) && (Boolean(traceHref) || Boolean(onOpenTrace))
  const errors = problem.errors ?? []

  const traceBody = (
    <>
      Open trace <span className="u-problem__trace">{traceId?.slice(0, 8)}</span>
    </>
  )

  /**
   * Hoisted above the `inline` early return on purpose. It used to be declared
   * below it, so the two codes whose treatment *is* `inline` — `validation_failed`
   * and `ticket_invalid` — rendered no trace link at all, silently dropping
   * patterns.md §3's MUST for exactly the failures an operator is most likely to
   * be chasing. A 422 naming a field and a 404 on an expired ticket both want
   * the link as much as a 503 does.
   */
  const traceLink = showTrace ? (
    traceHref ? (
      <Button
        as="a"
        variant="ghost"
        size="sm"
        href={traceHref}
        target="_blank"
        rel="noreferrer noopener"
        onClick={onOpenTrace}
      >
        {traceBody}
      </Button>
    ) : (
      <Button type="button" variant="ghost" size="sm" onClick={onOpenTrace}>
        {traceBody}
      </Button>
    )
  ) : null

  if (resolvedScale === 'inline') {
    return (
      <span className={className} role="status">
        {icon}
        <span>{problem.detail ?? title}</span>
        {onRetry && treatment?.retry === true && (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={handleRetry}
            disabled={retrying || cooldown > 0}
          >
            {retryLabel ?? treatment.retryLabel}
          </Button>
        )}
        {traceLink}
      </span>
    )
  }

  /**
   * patterns.md §12: nothing in this product is assertive. `role="alert"` carries an implicit
   * `aria-live="assertive"`, and an explicit `polite` on top of it is a contradiction rather than
   * an override — assistive tech is free to resolve it either way. `role="status"` is implicitly
   * polite, so the politeness is stated by the role and needs no attribute to repeat it.
   *
   * Page scale is not a live region at all. §3 requires a page-scale error to move focus to its
   * heading, and the screen reader reads a focused heading as a consequence of the focus move; a
   * live region wrapped around the same words would announce them a second time.
   */
  const liveRole = resolvedScale === 'page' ? undefined : 'status'

  return (
    <div className={className} role={liveRole}>
      {icon && <span className="u-problem__icon">{icon}</span>}
      <span className="u-problem__body">
        {resolvedScale === 'page' ? (
          <h1 className="u-problem__title" tabIndex={-1} ref={headingRef}>
            {title}
          </h1>
        ) : (
          <span className="u-problem__title">{title}</span>
        )}
        {/* `detail` is shown verbatim and never parsed. */}
        {problem.detail && <span className="u-problem__detail">{problem.detail}</span>}
        {errors.length > 0 && (
          <ul className="u-problem__errors">
            {errors.map((error, index) => (
              <li key={`${formatLoc(error.loc)}-${index}`}>
                <span className="u-problem__loc">{formatLoc(error.loc)}</span> — {error.msg}
              </li>
            ))}
          </ul>
        )}
        <span className="u-problem__meta">
          {problem.code && <span>code {problem.code}</span>}
          {problem.status != null && <span>HTTP {problem.status}</span>}
          {problem.instance && <span>{problem.instance}</span>}
          {problem.retry_after != null && <span>retry after {problem.retry_after}s</span>}
        </span>
        {(showRetry || actions || showTrace) && (
          <span className="u-problem__actions">
            {/* `loading` is the in-flight state, `disabled` the Retry-After window: both refuse
                the click, and `Button` folds them into one `disabled` element, but only the
                first one draws a spinner and sets `aria-busy`. */}
            {showRetry && (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={handleRetry}
                loading={retrying}
                disabled={cooldown > 0}
              >
                {cooldown > 0
                  ? `${retryLabel ?? treatment?.retryLabel ?? 'Try again'} in ${cooldown} s`
                  : (retryLabel ?? treatment?.retryLabel ?? 'Try again')}
              </Button>
            )}
            {actions}
            {traceLink}
          </span>
        )}
      </span>
    </div>
  )
}
