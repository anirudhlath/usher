import type { ReactNode } from 'react'
import clsx from 'clsx'
import { Artwork } from './Artwork'
import { ProgressBar } from './ProgressBar'

/**
 * Portrait card for home rails and browse grids. Takes a RowCard straight off /home:
 * { title_id, kind, name, year, enrichment_state, owned, position_seconds, runtime_seconds,
 *   played, episode_id, episode_label, artwork }.
 * `display_hint` is a hint, not a layout — this component is the portrait choice, LandscapeCard the other.
 *
 * @startingPoint section="Components" subtitle="Poster, landscape and list-row cards with progress and tier" viewport="700x300"
 */
export interface RowCard {
  title_id: string
  kind?: 'movie' | 'series' | 'episode'
  name: string
  year?: number | null
  enrichment_state?: 'skeleton' | 'stub' | 'enriched' | 'failed'
  owned?: boolean
  position_seconds?: number
  runtime_seconds?: number | null
  played?: boolean
  episode_id?: string | null
  /** Already formatted "S02E05" — print it, never recompose it. */
  episode_label?: string | null
  /** Image id, never a URL. */
  artwork?: string | null
}

export interface PosterCardProps {
  card: RowCard
  onOpen?: () => void
  /** Prints "· skeleton" in the meta line. On by default: skeletons are the majority. */
  showTier?: boolean
  /** Dims to --unowned-opacity for un-owned collection members. */
  unowned?: boolean
  badge?: ReactNode
  /**
   * An SSE `title.updated` frame landed on this title within the last 1000 ms (patterns.md §7).
   * Colour only: the class adds a highlight fade and nothing that moves, resizes or reorders.
   * The component knows nothing about SSE — the surface owns the flag and its lifetime.
   */
  patched?: boolean
}

/**
 * The whole card is **one** focusable button carrying a composed accessible name — "Stalker, 1979,
 * partly watched" (patterns.md §9). A nested play button would put two stops in the rail's tab
 * order for one thing, and `aria-label` on the button replaces its subtree for name computation, so
 * the watch state has to be composed into the name rather than left to the progress bar to carry.
 */
export function PosterCard({
  card,
  onOpen,
  showTier = true,
  unowned = false,
  badge,
  patched = false,
}: PosterCardProps) {
  const {
    name,
    year,
    artwork,
    kind,
    enrichment_state,
    episode_label,
    position_seconds,
    runtime_seconds,
    played,
  } = card
  const started = (position_seconds ?? 0) > 0 && !played
  const label = `${name}${year ? `, ${year}` : ''}${played ? ', watched' : started ? ', partly watched' : ''}`

  return (
    <button
      type="button"
      className={clsx('u-card', 'u-card--poster', unowned && 'u-card--unowned', patched && 'u-card--patched')}
      onClick={onOpen}
      aria-label={label}
    >
      <span className="u-card__shot">
        <Artwork id={artwork ?? null} kind="poster" width={342} name={name} alt="" />
        {badge && <span className="u-card__badge">{badge}</span>}
        {episode_label && <span className="u-card__ep">{episode_label}</span>}
        <span className={clsx('u-card__overlay', (started || played) && 'u-card__overlay--always')}>
          {(started || played) && (
            <ProgressBar
              positionSeconds={position_seconds ?? 0}
              runtimeSeconds={runtime_seconds ?? null}
              played={played ?? false}
            />
          )}
        </span>
      </span>
      <span className="u-card__meta">
        <span className="u-card__title">{name}</span>
        <span className="u-card__sub">
          <span>{year || '—'}</span>
          {kind === 'series' && <span>· series</span>}
          {showTier && enrichment_state === 'skeleton' && <span className="u-card__tier">· skeleton</span>}
        </span>
      </span>
    </button>
  )
}
