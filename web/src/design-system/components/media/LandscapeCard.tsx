import type { ReactNode } from 'react'
import clsx from 'clsx'
import { Artwork } from './Artwork'
import { ProgressBar } from './ProgressBar'
import type { RowCard } from './PosterCard'

/** 16:9 card for rows whose `display_hint` is landscape or wide, and for episode stills. */
export interface LandscapeCardProps {
  card: RowCard
  onOpen?: () => void
  /** Replaces the year line — e.g. "Aired 12 Mar 2019 · 48 min". */
  subtitle?: string
  aspect?: 'backdrop' | 'square'
  badge?: ReactNode
  /**
   * An SSE `title.updated` frame landed on this title within the last 1000 ms (patterns.md §7).
   * Colour only: the class adds a highlight fade and nothing that moves, resizes or reorders.
   * The component knows nothing about SSE — the surface owns the flag and its lifetime.
   */
  patched?: boolean
}

/**
 * Continue-watching always uses this card, so the composed accessible name carries the watch state
 * as well as the episode label: `aria-label` on a button replaces its subtree, which means the
 * progress bar's own `aria-valuetext` is not part of the name (patterns.md §9). One focusable
 * button, one name, no nested play button.
 */
export function LandscapeCard({
  card,
  onOpen,
  subtitle,
  aspect = 'backdrop',
  badge,
  patched = false,
}: LandscapeCardProps) {
  const { name, year, artwork, episode_label, position_seconds, runtime_seconds, played } = card
  const started = (position_seconds ?? 0) > 0 && !played
  const label = `${name}${episode_label ? `, ${episode_label}` : ''}${played ? ', watched' : started ? ', partly watched' : ''}`

  return (
    <button
      type="button"
      className={clsx('u-card', 'u-card--landscape', patched && 'u-card--patched')}
      onClick={onOpen}
      aria-label={label}
    >
      <span className="u-card__shot">
        <Artwork
          id={artwork ?? null}
          kind={aspect === 'square' ? 'logo' : 'backdrop'}
          width={780}
          name={name}
          alt=""
        />
        {badge && <span className="u-card__badge">{badge}</span>}
        <span className={clsx('u-card__overlay', (started || played) && 'u-card__overlay--always')}>
          {episode_label && <span className="u-card__ep-line">{episode_label}</span>}
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
        <span className="u-card__sub">{subtitle || year || '—'}</span>
      </span>
    </button>
  )
}
