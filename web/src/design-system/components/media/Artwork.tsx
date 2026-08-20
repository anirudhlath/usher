import { useState } from 'react'
import clsx from 'clsx'
import { Icon } from '../icon'
import { DEFAULT_RUNG, imageProxySizes, imageProxySrcSet, imageProxyUrl } from './imageLadder'

/**
 * Artwork from the image proxy. Never takes a URL — `id` is an image id and the component builds
 * `/images/{id}?w=`. Width snaps up the exact ladder: 154, 342, 780, 1280. Immutable, cached a year.
 *
 * Three distinct absent/failure states, drawn differently:
 * · no id at all      → "No artwork on record" + first initial (the API omits images when empty)
 * · 404               → "Artwork unavailable" (this proxy declines that artwork)
 * · 503 + Retry-After → "Artwork source is down. Retrying in 5 s." in warn tone
 */
export interface ArtworkProps {
  /** Image id from artwork / images[].id. Null or undefined is a legitimate state. */
  id?: string | null
  /** Drives aspect ratio — the API carries no width/height. */
  kind?: 'poster' | 'backdrop' | 'logo' | 'still' | 'profile'
  /** Requested width; snaps up to 154 | 342 | 780 | 1280. */
  width?: number
  /** Empty string when the adjacent title text already names the thing. */
  alt?: string
  /** Falls back to this name's initial when there is no artwork. */
  name?: string
  /** Force a failure treatment: 'retry' for 503 + Retry-After, 'declined' for 404. */
  status?: 'retry' | 'declined' | null
  /** Fixtures and UI kits only — bypasses the proxy with a literal src. Never use in product code. */
  srcOverride?: string
}

type LoadState = 'loading' | 'ready' | 'error'

/**
 * Aspect comes from `kind` because the API carries no dimensions: poster and profile are 2:3,
 * backdrop and still are 16:9, a logo gets the square box. `media.css` owns the three ratios.
 */
function shapeOf(kind: NonNullable<ArtworkProps['kind']>): 'poster' | 'backdrop' | 'square' {
  if (kind === 'backdrop' || kind === 'still') return 'backdrop'
  if (kind === 'logo') return 'square'
  return 'poster'
}

export function Artwork({
  id,
  kind = 'poster',
  width = DEFAULT_RUNG,
  alt = '',
  name,
  status,
  srcOverride,
}: ArtworkProps) {
  /**
   * A declared failure means we do not ask the proxy for bytes at all: a 404 stays a 404 however
   * many times it is requested, and the fallback is opaque over the image anyway. Fixtures win
   * outright — `srcOverride` exists so the gallery never touches the proxy.
   */
  const proxied =
    !srcOverride && id && !status
      ? { src: imageProxyUrl(id, width), srcSet: imageProxySrcSet(id, width), sizes: imageProxySizes(width) }
      : null
  const img = srcOverride ? { src: srcOverride } : proxied

  const [state, setState] = useState<LoadState>('loading')
  /**
   * `title.updated` can hand a skeleton title its first artwork while the card is on screen, so the
   * id changes under a mounted component. Resetting the load state during render (React's own
   * derive-state-from-props idiom) is what keeps the shimmer honest across that swap.
   */
  const [loadedSrc, setLoadedSrc] = useState<string | null>(img?.src ?? null)
  if ((img?.src ?? null) !== loadedSrc) {
    setLoadedSrc(img?.src ?? null)
    setState('loading')
  }

  const failure = status ?? (state === 'error' ? 'declined' : null)
  const showFallback = !srcOverride && (!id || failure !== null)

  return (
    <div
      className={clsx(
        'u-art',
        `u-art--${shapeOf(kind)}`,
        img !== null && state === 'loading' && 'u-art--loading',
      )}
    >
      {img !== null && (
        <img
          {...img}
          alt={alt}
          loading="lazy"
          decoding="async"
          onLoad={() => setState('ready')}
          onError={() => setState('error')}
          style={{
            opacity: state === 'ready' ? 1 : 0,
            transition: 'opacity var(--dur-base) var(--ease-out)',
          }}
        />
      )}
      {showFallback && (
        <div className={clsx('u-art__fallback', failure === 'retry' && 'u-art__fallback--retry')}>
          {!id ? (
            <>
              <span className="u-art__initial">{(name || '?').slice(0, 1)}</span>
              <span>No artwork on record</span>
            </>
          ) : failure === 'retry' ? (
            <>
              {/* Warn tone is the only hue-carried state here, so patterns.md §12's hue + icon + word applies. */}
              <Icon name="alert-triangle" />
              <span>Artwork source is down. Retrying in 5 s.</span>
            </>
          ) : (
            <>
              <span className="u-art__initial">{(name || '?').slice(0, 1)}</span>
              <span>Artwork unavailable</span>
            </>
          )}
        </div>
      )}
    </div>
  )
}
