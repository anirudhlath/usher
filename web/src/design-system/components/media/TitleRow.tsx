import type { ReactNode } from 'react'
import clsx from 'clsx'
import { Artwork } from './Artwork'

/** Text-forward list row. This is the default for /browse, which returns items with **no artwork** —
 *  never fetch an image per row to fill this in. */
export interface TitleRowProps {
  title: {
    title_id?: string
    id?: string
    name: string
    year?: number | null
    kind?: string
    enrichment_state?: 'skeleton' | 'stub' | 'enriched' | 'failed'
    genres?: string[]
    artwork?: string | null
  }
  onOpen?: () => void
  /** Only when the payload actually carries artwork. /browse does not. */
  thumb?: boolean
  trailing?: ReactNode
  meta?: ReactNode
  /**
   * An SSE `title.updated` frame landed on this title within the last 1000 ms (patterns.md §7).
   * Colour only: the class adds a highlight fade and nothing that moves, resizes or reorders.
   * The component knows nothing about SSE — the surface owns the flag and its lifetime.
   */
  patched?: boolean
}

/**
 * Dense list row for browse list-density, search results and any text-forward list.
 * /browse items carry no artwork — `thumb` defaults to false and nothing here fetches one per row.
 *
 * **A sparse title is not a broken one.** A skeleton is the majority of a 1.27M-title catalog, so
 * the row degrades by printing less, never by printing damage: the year falls back to an em dash,
 * the tier is stated plainly in muted tone rather than flagged as an error, and the genres and
 * `meta` slots simply do not render. The name alone is a legitimate row.
 */
export function TitleRow({ title, onOpen, thumb = false, trailing, meta, patched = false }: TitleRowProps) {
  const { name, year, kind, enrichment_state, genres } = title

  return (
    <button type="button" className={clsx('u-row', patched && 'u-row--patched')} onClick={onOpen}>
      {thumb && (
        <span className="u-row__thumb">
          <Artwork id={title.artwork ?? null} kind="poster" width={154} name={name} alt="" />
        </span>
      )}
      <span className="u-row__body">
        <span className="u-row__title">{name}</span>
        <span className="u-row__sub">
          <span>{year || '—'}</span>
          {kind && <span>{kind}</span>}
          {enrichment_state === 'skeleton' && <span className="u-row__tier">skeleton</span>}
          {genres && genres.length > 0 && (
            <span className="u-row__genres">{genres.slice(0, 3).join(' · ')}</span>
          )}
          {meta}
        </span>
      </span>
      {trailing && <span className="u-row__trail">{trailing}</span>}
    </button>
  )
}
