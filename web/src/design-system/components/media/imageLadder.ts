/**
 * The image proxy's width ladder, and the only place an image-proxy URL is constructed.
 *
 * It lives beside `Artwork` rather than inside it because it is the piece with a failure mode
 * worth testing on its own: the query parameter is **`w`**, and FastAPI ignores a query parameter
 * it does not declare — so `?width=780` is not an error. It returns the 342 default, and the only
 * symptom is a blurry poster on a retina screen. One function, one test, no second spelling.
 */

/**
 * The four widths the proxy serves. `GET /images/{id}?w=` snaps anything else **up** to the next
 * rung server-side, so asking for 400 spends a 780. The ladder is closed: a rung that is not one of
 * these four is an invented number and the proxy will not honour it.
 */
export const IMAGE_LADDER = [154, 342, 780, 1280] as const

export type ImageWidth = (typeof IMAGE_LADDER)[number]

const LARGEST_RUNG: ImageWidth = 1280

/** What the proxy returns when `w` is absent or unparseable — and what `Artwork` asks for by default. */
export const DEFAULT_RUNG: ImageWidth = 342

/** Snaps a requested width up the ladder. Anything above the top rung gets the top rung. */
export function snapImageWidth(requested: number): ImageWidth {
  if (!Number.isFinite(requested)) return DEFAULT_RUNG
  return IMAGE_LADDER.find((rung) => rung >= requested) ?? LARGEST_RUNG
}

/** `/images/{id}?w={rung}`. The id is an image id, never a URL. */
export function imageProxyUrl(id: string, requested: number): string {
  return `/images/${encodeURIComponent(id)}?w=${snapImageWidth(requested)}`
}

/**
 * The candidate renditions for a requested width: every rung at or above the snapped one, never
 * below it. A smaller rung would render blurrier than the layout asked for, and the ladder has no
 * intermediate values to interpolate — so the browser picks between real renditions only.
 */
export function imageProxySrcSet(id: string, requested: number): string {
  const from = snapImageWidth(requested)
  return IMAGE_LADDER.filter((rung) => rung >= from)
    .map((rung) => `${imageProxyUrl(id, rung)} ${rung}w`)
    .join(', ')
}

/**
 * `sizes` is the snapped rung, not the raw request: the rung *is* the intended CSS width of the
 * rendition, so every number `Artwork` emits — `src`, `srcSet` and `sizes` — is a ladder value.
 */
export function imageProxySizes(requested: number): string {
  return `${snapImageWidth(requested)}px`
}
