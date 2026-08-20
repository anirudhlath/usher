import type { ReactElement } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Button, Icon, Problem, type ProblemDocument } from '@/design-system'
import { UsherProblem, isProblemCode } from '@/api'
import { ROUTES } from '@/app/routes'
import { SkipLink } from '@/patterns'
import { useProblemTrace } from './trace'

/**
 * The `*` route, and the mapping every screen uses to turn a thrown client
 * error into the one error component.
 *
 * patterns.md §3 fixes the treatment for `not_found`: **page scale, back plus
 * search, and no retry.** A retry button here would be a control whose only
 * possible outcome is the same 404, which is why `Problem`'s own table refuses
 * to render one for this code even when a handler is passed.
 *
 * The app chrome is left intact — no scrim, no overlay, no focus trap — and
 * `Problem` moves focus to its own `<h1>` because the route changed under the
 * user and nothing else announces it.
 */
export function NotFound(): ReactElement {
  const { pathname } = useLocation()

  return (
    <div className="u-shell">
      {/* This route renders outside both shells, so it brings its own skip
          link and its own `#main`: the shells' copies are not above it. */}
      <SkipLink />
      <main id="main" className="mx-auto flex w-full max-w-content flex-col gap-6 px-4 py-10 tablet:px-6">
        {/* No `traceId` and no `traceHref`, deliberately. This 404 is
            **synthetic**: the router matched nothing, so no request was made,
            no response came back and no span exists. There is no id to pass and
            nothing in Tempo to open — unlike `ScreenProblem` below, which
            renders a failure a server actually answered. */}
        <Problem
          scale="page"
          icon={<Icon name="search-x" size={24} />}
          problem={{
            code: 'not_found',
            status: 404,
            detail:
              'No screen in this console is routed at that address. The link may be from an older build, or the address may have a typo in it.',
            instance: pathname,
          }}
          actions={<BackAndSearch />}
        />
      </main>
    </div>
  )
}

/**
 * `not_found`'s recovery, as patterns.md §3 words it: back **and** search.
 *
 * Two controls rather than one, because they answer different questions — "I
 * followed a bad link" is undone by going back, and "the thing I wanted moved"
 * is answered by looking for it.
 */
function BackAndSearch(): ReactElement {
  const navigate = useNavigate()
  return (
    <>
      <Button
        size="sm"
        variant="secondary"
        iconLeft={<Icon name="arrow-left" size={16} />}
        onClick={() => navigate(-1)}
      >
        Go back
      </Button>
      <Button
        size="sm"
        variant="ghost"
        iconLeft={<Icon name="search" size={16} />}
        onClick={() => navigate(ROUTES.search)}
      >
        Search the catalog
      </Button>
    </>
  )
}

/**
 * A failed query, rendered at the scale its `code` is designed at.
 *
 * The scale is **not** passed: `Problem` looks it up from the closed
 * seven-member vocabulary, so a screen cannot accidentally demote a `not_found`
 * to a panel or promote a `source_unavailable` to a whole page. `onRetry` is
 * likewise safe to pass unconditionally — the same table suppresses the control
 * for every code where repeating the request cannot help.
 *
 * The trace link (patterns.md §3) is wired here rather than at each of the five
 * screens behind this wrapper, for the same reason the scale is: it is a rule
 * about *what a rendered problem must show*, not a decision any one screen gets
 * to make. `useProblemTrace` yields both props at once and both are `null` when
 * there is nothing to open, which renders as **no anchor**.
 */
export interface ScreenProblemProps {
  error: unknown
  /** The route that failed, for the case where the server did not name it. */
  instance?: string
  onRetry?: () => void | Promise<void>
}

export function ScreenProblem({ error, instance, onRetry }: ScreenProblemProps): ReactElement {
  const traceOf = useProblemTrace()
  const problem = problemDocumentOf(error, instance)
  const page = problem.code === 'not_found'
  return (
    <Problem
      problem={problem}
      icon={<Icon name={page ? 'search-x' : 'x-circle'} size={page ? 24 : 20} />}
      {...traceOf(error)}
      {...(onRetry ? { onRetry } : {})}
      {...(page ? { actions: <BackAndSearch /> } : {})}
    />
  )
}

/**
 * `UsherProblem` → the design system's problem shape.
 *
 * Three rules survive the crossing and each is a correctness rule rather than a
 * formatting one (patterns.md §3):
 *
 * · **`detail` is passed through untouched and is never parsed.** It is prose
 *   the server may reword at any release and is frequently the only thing that
 *   says what happened.
 * · **`title` is deliberately dropped.** Usher sends the bare HTTP reason
 *   phrase ("Not Found"), and `Problem`'s own table carries the designed
 *   sentence for each of the seven. Passing the wire's value would replace
 *   "We couldn't find that." with "Not Found" for no gain.
 * · **A code outside the seven is not coerced into one.** It is left off, and
 *   `Problem` falls back to showing `status` and `detail`, which is all a
 *   surface can honestly claim about a vocabulary this build predates.
 */
function problemDocumentOf(error: unknown, instance?: string): ProblemDocument {
  if (!(error instanceof UsherProblem)) {
    // No status, no body, no code: `fetch` rejected and the request never
    // reached Usher. "The request never left" and "the server said nothing"
    // look identical from a spinner, so this says which one it was.
    return {
      status: 0,
      title: "We couldn't reach the server.",
      detail:
        'The request did not complete, so there is no status and no message from Usher. The server may be down, or the connection may have dropped.',
      ...(instance === undefined ? {} : { instance }),
    }
  }

  const route = error.instance ?? instance
  return {
    status: error.status,
    detail: error.detail,
    ...(isProblemCode(error.code) ? { code: error.code } : {}),
    ...(route === undefined ? {} : { instance: route }),
    ...(error.retryAfter === null ? {} : { retry_after: error.retryAfter }),
  }
}
