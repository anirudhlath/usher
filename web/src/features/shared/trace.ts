import { UsherProblem } from '@/api'
import { useTraceUrl } from '@/app/runtime-config-context'

/**
 * The two props `Problem` needs to render "Open trace", derived from a caught
 * error.
 *
 * patterns.md §3 makes the link a MUST — *"When the response carried a trace
 * id, `Problem` MUST render 'Open trace' into Tempo. This single link is what
 * separates a console from a settings page."* — and there are twenty-odd
 * places in this app that turn a caught error into a `<Problem>`. Deriving the
 * pair once, here, is what keeps that MUST from being twenty-odd chances to
 * forget it.
 */
export interface ProblemTrace {
  /**
   * The correlated trace id, or `null` when the error was not a `UsherProblem`
   * (the request never reached Usher) or the response carried no usable
   * `traceresponse` header.
   */
  readonly traceId: string | null
  /**
   * Where to open it, or `null` when this deployment has no `tempoUrl`.
   *
   * **`null` renders as no anchor at all, never as an empty `href`.** An
   * `<a href="">` navigates to the current page, so a link that cannot go
   * anywhere is worse than an absent one: it costs a click to discover. This
   * is the same absent-not-dead rule the dev drawer's own trace affordance
   * follows, and `Problem` enforces it — it needs *both* an id and a
   * destination before it renders the control.
   */
  readonly traceHref: string | null
}

/**
 * A frozen shared value rather than a fresh object: the no-trace case is the
 * common one, and `Problem` reads both members as booleans.
 */
const NO_TRACE: ProblemTrace = { traceId: null, traceHref: null }

/**
 * A hook that returns a **mapper**, not the pair itself.
 *
 * This shape is deliberate and is copied from `useTraceUrl()` above it. Almost
 * every `<Problem>` in this codebase is rendered inside a conditional — an
 * `isError` branch, a ternary, a `.map()`, a nested panel — and a screen with
 * four failure surfaces has four different errors to map. A `useProblemTrace(error)`
 * taking the error directly would either have to be called in those
 * conditionals (a rules-of-hooks violation) or be hoisted four times to the top
 * of a component that does not have all four errors in scope there. One hook
 * call per component, then a plain call per call site, has neither problem:
 *
 * ```tsx
 * const traceOf = useProblemTrace()
 * …
 * {query.isError && <Problem problem={problemOf(query.error)} {...traceOf(query.error)} />}
 * ```
 *
 * The spread is safe under `exactOptionalPropertyTypes` because both of
 * `Problem`'s trace props accept `null` — that widening exists precisely so no
 * call site needs a `...(id ? { traceId: id } : {})` dance.
 */
export function useProblemTrace(): (error: unknown) => ProblemTrace {
  const traceUrl = useTraceUrl()
  return (error: unknown): ProblemTrace => {
    // Not a `UsherProblem` means `fetch` itself rejected: there is no response,
    // so there is no header and no span to name. Inventing an id here would be
    // the one thing worse than no link.
    if (!(error instanceof UsherProblem)) return NO_TRACE
    const { traceId } = error
    if (traceId === null) return NO_TRACE
    return { traceId, traceHref: traceUrl(traceId) }
  }
}
