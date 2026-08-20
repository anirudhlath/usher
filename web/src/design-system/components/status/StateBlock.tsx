import type { ReactNode } from 'react'
import clsx from 'clsx'
import { Icon, STATE_ICON } from '../icon'

/**
 * "Absent" is four different facts in this API and they get four treatments:
 *
 * · never — dashed hairline, italic sentence. The value has never been computed (`computed_at: null`,
 *           `facets.computed: false`, a metric with no samples, `expanded_query: null`).
 * · empty — solid hairline on a sunken fill. Computed, genuinely nothing there (`neighbors: []`).
 * · stale — amber hairline. Computed, but the inputs changed since (`stale: true`).
 * · na    — an em dash and a short clause. Not applicable to this kind of title (collections are films only).
 */
export interface StateBlockProps {
  kind?: 'never' | 'empty' | 'stale' | 'na'
  /** Overrides the default heading. */
  title?: string
  children?: ReactNode
  /** Mono line for the field that proves it: "computed_at: null". */
  meta?: string
  icon?: ReactNode
  action?: ReactNode
}

export type StateBlockKind = NonNullable<StateBlockProps['kind']>

/** The three block kinds. `na` has no heading — it is an em dash and one clause, inline. */
const HEADING = {
  never: 'Never computed',
  empty: 'Computed, and empty',
  stale: 'Stale',
} as const satisfies Record<Exclude<StateBlockKind, 'na'>, string>

/**
 * §12: hue is never the only carrier. `never` and `stale` are two of the six fixed
 * state glyphs, so they are supplied rather than left to the call site; `empty` has
 * no glyph in that vocabulary and is carried by its heading and its solid hairline.
 */
function defaultIcon(kind: Exclude<StateBlockKind, 'na'>): ReactNode {
  if (kind === 'never') return <Icon name={STATE_ICON.never} />
  if (kind === 'stale') return <Icon name={STATE_ICON.stale} />
  return undefined
}

/**
 * The absent-state block. Four kinds, four treatments, because the API distinguishes them
 * and choosing the wrong one is a correctness bug (patterns.md §2).
 *
 * `meta` names the field that proves the claim — it is the product's honesty made visible
 * and is rendered whenever it is given. It is not droppable to reduce clutter.
 */
export function StateBlock({ kind = 'empty', title, children, meta, icon, action }: StateBlockProps) {
  if (kind === 'na') {
    return (
      <span className="u-state u-state--na">
        <span className="u-state__body">— {children}</span>
      </span>
    )
  }
  return (
    <div className={clsx('u-state', `u-state--${kind}`)} role="status">
      <span className="u-state__head">
        {icon ?? defaultIcon(kind)}
        {title ?? HEADING[kind]}
      </span>
      <span className="u-state__body">{children}</span>
      {meta ? <span className="u-state__meta">{meta}</span> : null}
      {action}
    </div>
  )
}
