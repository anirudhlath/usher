import type { CSSProperties, ReactElement, ReactNode } from 'react'

/**
 * Loading placeholders shaped like the final layout. Budget guidance from measured server timings:
 * browse runs 140–320 ms p95 (skeleton, no spinner), home composes fast and is cached 30 s with an
 * ETag (skeleton rarely visible), a source probe or a /play call takes 1–5 s (button-level pending
 * state, not a page skeleton).
 *
 * A skeleton is aria-hidden; the surrounding region owns `aria-busy` — see `SkeletonRegion`.
 *
 * The sweep is one shared 1400 ms `--dur-shimmer` on `.u-skel`, so every surface loads at the same
 * tempo, and `prefers-reduced-motion` drops the sweep in CSS while keeping the shape.
 *
 * Sizes are minimums, never maximums: a skeleton that shrinks when real content lands moves the
 * page under the reader, and that is worse than a skeleton that was slightly too short.
 */
export interface SkeletonProps {
  /** text = stacked lines · rail = poster row · table = operator rows · hero = title detail · block = a raw box */
  shape?: 'text' | 'rail' | 'table' | 'hero' | 'block'
  width?: number | string
  height?: number | string
  /** text shape: how many lines. Last line is 62% wide. */
  lines?: number
  /** rail / table shape: how many items. */
  count?: number
  style?: CSSProperties
}

function range(length: number): number[] {
  return Array.from({ length }, (_, index) => index)
}

export function Skeleton({
  shape = 'text',
  width,
  height,
  lines = 3,
  count = 6,
  style,
}: SkeletonProps): ReactElement {
  if (shape === 'text') {
    return (
      <span className="u-skel-text" aria-hidden="true" style={{ width: width ?? '100%', ...style }}>
        {range(lines).map((index) => (
          <span
            key={index}
            className="u-skel u-skel--text"
            style={{ width: index === lines - 1 ? '62%' : '100%' }}
          />
        ))}
      </span>
    )
  }

  if (shape === 'rail') {
    return (
      <div className="u-skel-rail" aria-hidden="true" style={style}>
        {range(count).map((index) => (
          <div key={index}>
            <span className="u-skel u-skel--poster" />
            <span className="u-skel u-skel--text" style={{ width: '78%' }} />
            <span className="u-skel u-skel--text" style={{ width: '40%' }} />
          </div>
        ))}
      </div>
    )
  }

  if (shape === 'table') {
    return (
      <div className="u-skel-table" aria-hidden="true" style={style}>
        {range(count).map((index) => (
          <div className="u-skel-table__row" key={index}>
            <span className="u-skel u-skel--text" style={{ width: 96 }} />
            <span className="u-skel u-skel--text" style={{ width: 72 }} />
            <span className="u-skel u-skel--text" style={{ width: 140 }} />
            <span className="u-skel u-skel--text" style={{ width: 56, marginLeft: 'auto' }} />
          </div>
        ))}
      </div>
    )
  }

  if (shape === 'hero') {
    return (
      <div className="u-skel-hero" aria-hidden="true" style={style}>
        <span className="u-skel u-skel--poster u-skel-hero__poster" />
        <span className="u-skel-hero__body">
          <span className="u-skel" style={{ height: 34, width: '46%' }} />
          <span className="u-skel u-skel--text" style={{ width: '28%' }} />
          <span className="u-skel u-skel--text" style={{ width: '92%' }} />
          <span className="u-skel u-skel--text" style={{ width: '84%' }} />
        </span>
      </div>
    )
  }

  return (
    <span
      className="u-skel"
      style={{ width: width ?? '100%', height: height ?? 16, ...style }}
      aria-hidden="true"
    />
  )
}

export interface SkeletonRegionProps {
  /** True while the real content is pending. */
  busy: boolean
  /** The visually-hidden sentence a screen reader gets instead of the shapes. */
  label?: string
  children?: ReactNode
  className?: string
}

/**
 * The other half of the loading contract: the skeleton is `aria-hidden`, so the region that owns it
 * has to carry `aria-busy="true"` and a visually-hidden "Loading …" label. Without this a screen
 * reader is handed an empty box and no reason for it.
 */
export function SkeletonRegion({
  busy,
  label = 'Loading …',
  children,
  className,
}: SkeletonRegionProps): ReactElement {
  return (
    <div className={className} aria-busy={busy ? true : undefined}>
      {busy && <span className="u-visually-hidden">{label}</span>}
      {children}
    </div>
  )
}
