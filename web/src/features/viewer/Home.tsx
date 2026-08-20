/**
 * Home — the server-composed screen.
 *
 * `GET /home` returns rows and nothing else: a slug, a title, a **`reason`
 * sentence** and a `display_hint`. The reason is the product rather than
 * decoration — "Because you watched Stalker." is the whole difference between a
 * recommendation and a shelf — so every row prints its own, and a row whose
 * `reason` is `null` prints none rather than an invented one.
 *
 * Three behaviours here come straight out of patterns.md and are correctness
 * rules rather than polish:
 *
 * · **§1 — a cached-and-revalidating surface shows the cached content, never a
 *   skeleton.** `/home` is cached 30 s with an ETag, so the skeleton is a
 *   first-paint artefact. The branch below is on `data === undefined`, not on
 *   `isFetching`: stale-then-fresh, not blank-then-fresh.
 * · **§9 — `←`/`→` move focus between cards and the rail scrolls to keep the
 *   focused card visible.** The scroll is `track.scrollTo`, never
 *   `scrollIntoView`, which scrolls every ancestor too and lands somewhere
 *   nobody chose (§4).
 * · **§7 — the screen is fully correct if zero SSE frames ever arrive.** A
 *   `title.updated` frame adds a 1000 ms highlight class and nothing else: no
 *   movement, no resize, no reorder, no focus change.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  Badge,
  Button,
  Icon,
  LandscapeCard,
  PosterCard,
  Problem,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  type ProblemDocument,
} from '@/design-system'
import {
  UsherProblem,
  fieldErrors,
  useEventStream,
  useHome,
  type ProblemCode,
  type RowCard,
  type RowResponse,
} from '@/api'
import { ROUTES, episodePath, titlePath } from '@/app/routes'
import { useViewport } from '@/app/useViewport'
import { usePrefersReducedMotion } from '@/patterns'
import { useProblemTrace } from '@/features/shared/trace'

/** patterns.md §7: one second, opacity and colour only. */
const PATCH_HIGHLIGHT_MS = 1_000

/** patterns.md §1's shape for this surface: 3 rows, 6 cards, 2 on a phone. */
const SKELETON_ROWS = ['first', 'second', 'third'] as const

export default function Home() {
  const navigate = useNavigate()
  const { phone } = useViewport()
  const gutter = phone ? 'var(--gutter-page-phone)' : 'var(--gutter-page)'
  const reducedMotion = usePrefersReducedMotion()
  const patched = useLiveHome()
  const traceOf = useProblemTrace()

  const { data, error, refetch } = useHome()

  if (data === undefined && error !== null) {
    return (
      <div className="u-railset" style={{ paddingInline: gutter }}>
        <Problem
          scale="page"
          icon={<Icon name={iconFor(error)} size={24} />}
          problem={toProblemDocument(error, "We couldn't build your home screen.")}
          {...traceOf(error)}
          onRetry={async () => {
            await refetch()
          }}
        />
      </div>
    )
  }

  if (data === undefined) {
    return (
      <SkeletonRegion busy label="Loading your home screen …" className="u-railset">
        <h1 className="u-visually-hidden">Home</h1>
        {SKELETON_ROWS.map((row) => (
          <div key={row} className="flex flex-col gap-3" style={{ paddingInline: gutter }}>
            {/* The row title, at its real height. A skeleton that grows when
                content lands moves the page under the reader (§1). */}
            <Skeleton shape="block" height={20} width={190} />
            <Skeleton shape="rail" count={phone ? 2 : 6} />
          </div>
        ))}
      </SkeletonRegion>
    )
  }

  const rows = data.rows

  if (rows.length === 0) {
    return (
      <div
        style={{
          paddingInline: gutter,
          paddingBlock: 'var(--space-16)',
          maxWidth: 'var(--width-prose)',
        }}
      >
        <h1
          style={{
            font: 'var(--text-display-sm)',
            color: 'var(--text-primary)',
            letterSpacing: 'var(--track-display)',
          }}
        >
          Nothing to show you yet
        </h1>
        <p
          style={{
            font: 'var(--text-body-lg)',
            color: 'var(--text-secondary)',
            marginTop: 'var(--space-4)',
            textWrap: 'pretty',
          }}
        >
          Every row provider returned empty. That is expected before a source has finished its first sync: the
          catalog is browsable, but nothing is owned, watched or recently added yet.
        </p>
        <div style={{ display: 'flex', gap: 'var(--space-2x)', marginTop: 'var(--space-6)' }}>
          <Button variant="primary" onClick={() => navigate(ROUTES.browse)}>
            Browse the catalog
          </Button>
          <Button variant="secondary" onClick={() => navigate(ROUTES.search)}>
            Search for a title
          </Button>
        </div>
        <div style={{ marginTop: 'var(--space-8)' }}>
          <StateBlock kind="never" icon={<Icon name="circle-dashed" />} meta="rows: []">
            Recently added is the only row that fires for a household that has watched nothing, and it needs a
            title added in the last 30 days.
          </StateBlock>
        </div>
      </div>
    )
  }

  /**
   * **Degraded is a failed revalidation over content we still hold**, which is
   * the only partial state `/home` can actually report: the response carries
   * `rows` and no per-row error channel, so how many rows were dropped is the
   * server's sentence to write and ours to print verbatim (§3 — `detail` is
   * shown and never parsed). Inferring "three rows are missing" from a count of
   * enabled providers would be a fabrication: a provider that proposes nothing
   * produces no row, and an absent shelf is not a broken one.
   */
  const degraded = error !== null

  return (
    <div className="u-railset">
      <h1 className="u-visually-hidden">Home</h1>
      {degraded && (
        <div style={{ paddingInline: gutter }}>
          <Problem
            scale="panel"
            tone="warn"
            icon={<Icon name="alert-triangle" size={20} />}
            problem={toProblemDocument(error, 'Showing a partial home screen.')}
            {...traceOf(error)}
            retryLabel="Rebuild rows"
            onRetry={async () => {
              await refetch()
            }}
          />
        </div>
      )}
      {rows.map((row) => (
        <Rail
          key={row.slug}
          title={row.title}
          reason={row.reason}
          gutter={gutter}
          reducedMotion={reducedMotion}
        >
          {row.cards.map((card) => (
            <RowCardTile
              key={cardKey(row, card)}
              card={card}
              landscape={isLandscape(row.display_hint)}
              patched={patched.has(card.title_id)}
              onOpen={() =>
                navigate(card.episode_id === null ? titlePath(card.title_id) : episodePath(card.episode_id))
              }
            />
          ))}
        </Rail>
      ))}
      <p
        style={{
          paddingInline: gutter,
          font: 'var(--text-body-xs)',
          color: 'var(--text-muted)',
          maxWidth: '62ch',
        }}
      >
        Rows are composed by the server from ten providers and re-ranked per request. A provider that proposes
        nothing produces no row — an absent shelf is not a broken one.
      </p>
    </div>
  )
}

/* ------------------------------------------------------------------- rail */

interface RailProps {
  title: string
  /** `null` is a real state: a row assembled by a `SELECT` has no reason to give. */
  reason: string | null
  gutter: string
  reducedMotion: boolean
  children: ReactNode
}

/**
 * One shelf. `←`/`→` move focus between the cards and the **track** scrolls to
 * follow — patterns.md §9 and §4.
 *
 * The listener is attached imperatively rather than as an `onKeyDown` prop
 * because the track is a plain scroll container: the focusable things are the
 * cards inside it, and a keyboard handler on a `<div>` with no role is exactly
 * what `jsx-a11y/no-static-element-interactions` exists to catch. Delegating
 * from the container is the same behaviour without the false claim that the
 * container is interactive.
 */
function Rail({ title, reason, gutter, reducedMotion, children }: RailProps) {
  const track = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const element = track.current
    if (element === null) return

    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return
      if (element === null) return
      const cards = Array.from(element.querySelectorAll<HTMLElement>('.u-card'))
      const index = cards.findIndex((card) => card === document.activeElement)
      if (index === -1) return
      const next = cards[index + (event.key === 'ArrowRight' ? 1 : -1)]
      if (next === undefined) return
      event.preventDefault()
      // `preventScroll` because the rail owns the scrolling: the browser's own
      // "bring it into view" scrolls every ancestor, which on a page of rails
      // moves the whole screen sideways under the reader.
      next.focus({ preventScroll: true })
      keepVisible(element, next, reducedMotion)
    }

    element.addEventListener('keydown', onKeyDown)
    return () => element.removeEventListener('keydown', onKeyDown)
  }, [reducedMotion])

  return (
    <section className="u-rail" aria-label={title}>
      <div className="u-rail__head" style={{ paddingInline: gutter, flexWrap: 'wrap', rowGap: 2 }}>
        <h2 className="u-rail__title">{title}</h2>
        {reason !== null && <p className="u-rail__reason">{reason}</p>}
      </div>
      <div className="u-rail__track" ref={track} style={{ paddingInline: gutter }}>
        {children}
      </div>
    </section>
  )
}

/**
 * Scrolls the track just far enough that the focused card is whole.
 *
 * **Never `scrollIntoView`** (patterns.md §4). Reduced motion drops the
 * animation and keeps the destination — §12 collapses durations, it does not
 * remove the behaviour.
 */
function keepVisible(track: HTMLElement, card: HTMLElement, reducedMotion: boolean): void {
  const behavior: ScrollBehavior = reducedMotion ? 'auto' : 'smooth'
  const left = card.offsetLeft - track.offsetLeft
  const right = left + card.offsetWidth
  if (left < track.scrollLeft) {
    track.scrollTo({ left, behavior })
  } else if (right > track.scrollLeft + track.clientWidth) {
    track.scrollTo({ left: right - track.clientWidth, behavior })
  }
}

/* ------------------------------------------------------------------ cards */

interface RowCardTileProps {
  card: RowCard
  landscape: boolean
  patched: boolean
  onOpen: () => void
}

/**
 * `display_hint` picks the card component and nothing else. It says what shape
 * a card *is*; it never says where to put it (ADR-0006), which is why the
 * layout above is the same for every row.
 *
 * Continue-watching progress is the 3 px bar `PosterCard` and `LandscapeCard`
 * already draw on the artwork's bottom edge from `position_seconds` /
 * `runtime_seconds`; a card with no runtime gets a bar that says "Progress
 * unknown" rather than a fabricated percentage.
 */
function RowCardTile({ card, landscape, patched, onOpen }: RowCardTileProps) {
  if (landscape) {
    // `exactOptionalPropertyTypes`: `subtitle={undefined}` is not the same as
    // omitting it, so the prop is spread in or not at all.
    const subtitle = subtitleOf(card)
    return (
      <LandscapeCard
        card={card}
        onOpen={onOpen}
        patched={patched}
        {...(subtitle === undefined ? {} : { subtitle })}
      />
    )
  }
  return (
    <PosterCard
      card={card}
      onOpen={onOpen}
      patched={patched}
      badge={card.owned ? null : <Badge tone="neutral">not owned</Badge>}
    />
  )
}

/**
 * "S1E1 · Pilot · 47 min" — `episode_label` is already formatted by the server,
 * so it is printed and never recomposed. A card with no runtime contributes no
 * minutes clause rather than a zero.
 */
function subtitleOf(card: RowCard): string | undefined {
  if (card.episode_label === null) return undefined
  const minutes = card.runtime_seconds === null ? null : Math.round(card.runtime_seconds / 60)
  return minutes === null ? card.episode_label : `${card.episode_label} · ${minutes} min`
}

function isLandscape(hint: RowResponse['display_hint']): boolean {
  return hint === 'landscape' || hint === 'wide'
}

/**
 * A title can legitimately appear in two rows, and `title_id` alone would then
 * collide. The row's slug is what makes the key unique.
 */
function cardKey(row: RowResponse, card: RowCard): string {
  return `${row.slug}:${card.episode_id ?? card.title_id}`
}

/* ------------------------------------------------------------------- live */

/**
 * The SSE surface for this screen, and it is **delight, never mechanism**
 * (patterns.md §7): every branch above is correct when this returns an empty
 * set forever, which is what a lossy in-process bus with nobody publishing
 * looks like.
 *
 * A `title.updated` frame adds one class for 1000 ms. It does not refetch, does
 * not reorder the rail, does not resize a card and does not touch focus —
 * moving a card under a pointer that is about to click it is hostile.
 *
 * `row.invalidated` is the one frame that does reach the network, and it
 * invalidates `/home` rather than a row: there is no per-row route to refetch,
 * so "refetch that row only" is not expressible against this API. Recomposing
 * the screen is the honest approximation and costs one cached request.
 */
function useLiveHome(): ReadonlySet<string> {
  const queryClient = useQueryClient()
  const [patched, setPatched] = useState<ReadonlySet<string>>(() => new Set<string>())
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  useEffect(() => {
    const running = timers.current
    return () => {
      for (const timer of running.values()) clearTimeout(timer)
      running.clear()
    }
  }, [])

  useEventStream({
    onEvent: (event) => {
      if (event.name === 'row.invalidated') {
        void queryClient.invalidateQueries({ queryKey: ['home'] })
        return
      }
      if (event.name !== 'title.updated') return
      const id = event.payload.title_id
      setPatched((current) => new Set(current).add(id))
      const running = timers.current.get(id)
      if (running !== undefined) clearTimeout(running)
      timers.current.set(
        id,
        setTimeout(() => {
          timers.current.delete(id)
          setPatched((current) => {
            const next = new Set(current)
            next.delete(id)
            return next
          })
        }, PATCH_HIGHLIGHT_MS),
      )
    },
  })

  return patched
}

/* ---------------------------------------------------------------- problems */

/**
 * An `UsherProblem` as the design system's `Problem` wants it.
 *
 * `detail` is copied across untouched and never parsed (§3); `code` and
 * `status` ride along because an operator pastes them into a log query. A
 * transport failure has neither, and saying so plainly beats printing
 * `HTTP 0`.
 */
function toProblemDocument(error: unknown, fallbackTitle: string): ProblemDocument {
  if (!(error instanceof UsherProblem)) {
    return {
      title: fallbackTitle,
      detail: error instanceof Error ? error.message : String(error),
    }
  }

  // The wire's `title` is the HTTP status phrase ("Service Unavailable"), and
  // `status` already carries that. The screen's own framing is the useful
  // heading; `detail` — the server's prose — is what §3 requires verbatim, and
  // it is copied across untouched below.
  const document: ProblemDocument = { status: error.status, detail: error.detail }
  document.title = fallbackTitle
  if (error.knownCode !== null) document.code = error.knownCode
  if (error.instance !== undefined) document.instance = error.instance
  if (error.retryAfter !== null) document.retry_after = error.retryAfter

  const fields = fieldErrors(error.errors)
  if (fields.length > 0) {
    document.errors = fields.map((field) => ({
      loc: field.field === '' ? [] : field.field.split('.'),
      msg: field.message,
    }))
  }
  return document
}

/** §12: hue is never the only carrier, so the failure gets its own glyph. */
function iconFor(error: unknown): 'server-off' | 'search-x' | 'x-circle' {
  const code: ProblemCode | null = error instanceof UsherProblem ? error.knownCode : null
  if (code === 'source_unavailable') return 'server-off'
  if (code === 'not_found') return 'search-x'
  return 'x-circle'
}
