/**
 * Browse — a filter and sort surface over a 1.27M-row catalog.
 *
 * **There is no total, no page number and no result count anywhere on this
 * screen** (patterns.md §4, §14). `GET /browse` answers `items` and an opaque
 * `next_cursor`, and that is deliberately all it answers: counting 1.27M rows
 * would cost more than the page it decorated. Progress therefore counts
 * *loaded* — "24 loaded so far" — and the end of the list is a **sentence**,
 * because a silent stop is indistinguishable from a bug.
 *
 * The other two rules this screen owns:
 *
 * · **Facets explain themselves when they are unavailable** (§2).
 *   `facets.computed: false` carries a `reason`, and the two members get two
 *   different sentences with two different fixes: `"unpredicated"` is fixed by
 *   setting a filter, `"not_requested"` by asking for them.
 * · **Changing a filter or a sort invalidates every outstanding cursor** (§4).
 *   The in-flight page is dropped, the accumulated items are discarded and the
 *   list restarts from the top — and `invalid_cursor` MUST NOT reach the UI,
 *   because a user who changed a filter did nothing wrong and has nothing to
 *   fix.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  Badge,
  Button,
  FilterChip,
  Icon,
  IconButton,
  LoadMore,
  PosterCard,
  Problem,
  Select,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  TitleRow,
  type ProblemDocument,
} from '@/design-system'
import {
  UsherProblem,
  fieldErrors,
  recoveryFor,
  useBootstrapStatus,
  useBrowse,
  useEventStream,
  type BrowseFacets,
  type BrowseFilters,
  type BrowseItem,
  type BrowseSort,
} from '@/api'
import { titlePath } from '@/app/routes'
import { useViewport } from '@/app/useViewport'
import { rememberScroll, useLayer, useRestoreScroll } from '@/patterns'
import { useProblemTrace } from '@/features/shared/trace'

const PAGE_SIZE = 24
const PATCH_HIGHLIGHT_MS = 1_000

const SORTS: { value: BrowseSort; label: string }[] = [
  { value: 'name', label: 'Name' },
  { value: 'year', label: 'Year' },
  { value: 'popularity', label: 'Popularity' },
  { value: 'vote_count', label: 'Vote count' },
]

/** patterns.md §1: the browse list skeleton is 8 table rows. */
const SKELETON_ROWS = 8
/** The grid skeleton is one screen's worth, reflowed to the grid's columns. */
const SKELETON_TILES = 12

type Density = 'list' | 'grid'

export default function Browse() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { phone, tablet } = useViewport()
  const [params, setParams] = useSearchParams()
  const [sheetOpen, setSheetOpen] = useState(false)
  const patched = usePatchedTitles()
  const traceOf = useProblemTrace()

  const sort = readSort(params.get('sort'))
  const genre = params.get('genre')
  const year = readYear(params.get('year'))
  const owned = readOwned(params.get('owned'))
  const density: Density = params.get('density') === 'grid' ? 'grid' : 'list'

  /**
   * `facets: true` is sent **always**, and that is the honest choice rather
   * than an expensive one. A client that declined to ask when nothing is
   * filtered would be answered `not_requested` — "we did not look" — when the
   * real reason counts are missing is that no predicate exists to count under.
   * Asking every time is what makes the server's own `unpredicated` reason, and
   * its own fix, reach the panel.
   */
  const filters: BrowseFilters = {
    sort,
    genre,
    year,
    // `undefined` is the chip's third state, "either"; on the wire it is the
    // absence of the parameter, which `useBrowse` spells `null`.
    owned: owned ?? null,
    facets: true,
    limit: PAGE_SIZE,
  }

  const { data, error, fetchNextPage, hasNextPage, isFetchingNextPage, refetch } = useBrowse(filters)

  const pages = data?.pages ?? []
  const items = pages.flatMap((page) => page.items)
  const facets = pages.at(0)?.facets
  const nextCursor = pages.at(-1)?.next_cursor ?? null

  useCursorRestart(error, pages.length, queryClient)
  useScrollMemory(params.toString(), pages.length, fetchNextPage, hasNextPage)

  /**
   * Every filter and sort change goes through here, because §4 makes all three
   * of its clauses one action: cancel the request already in flight, throw away
   * what has accumulated, and start again from the top. The first is this
   * `cancelQueries`; the other two are what a changed query key does by itself,
   * since the new key has no pages and `initialPageParam` is `null`.
   */
  const applyFilters = useCallback(
    (mutate: (next: URLSearchParams) => void) => {
      void queryClient.cancelQueries({ queryKey: ['browse'] })
      const next = new URLSearchParams(params)
      mutate(next)
      setParams(next)
    },
    [params, queryClient, setParams],
  )

  const setParam = useCallback(
    (key: string, value: string | null) => {
      applyFilters((next) => {
        if (value === null) next.delete(key)
        else next.set(key, value)
      })
    },
    [applyFilters],
  )

  /** Density is a rendering choice, not a query — it must not restart the list. */
  const setDensity = useCallback(
    (next: Density) => {
      const updated = new URLSearchParams(params)
      updated.set('density', next)
      setParams(updated, { replace: true })
    },
    [params, setParams],
  )

  const facetPanel = (
    <FacetPanel
      facets={facets}
      onPickGenre={(name) => setParam('genre', name)}
      onPickYear={(value) => setParam('year', value)}
    />
  )

  return (
    <div
      style={{
        paddingInline: phone ? 'var(--gutter-page-phone)' : 'var(--gutter-page)',
        paddingBlock: 'var(--space-6)',
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--space-5)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--space-3)', flexWrap: 'wrap' }}>
        <h1 style={{ font: 'var(--text-title-lg)', color: 'var(--text-primary)' }}>Browse</h1>
        <CatalogNote />
      </div>

      <Controls
        genre={genre}
        year={year}
        owned={owned}
        sort={sort}
        density={density}
        onGenre={(value) => setParam('genre', value)}
        onYear={(value) => setParam('year', value)}
        onOwned={(value) => setParam('owned', value === undefined ? null : String(value))}
        onSort={(value) => setParam('sort', value)}
        onDensity={setDensity}
      />

      <div style={{ display: 'flex', gap: 'var(--space-6)', alignItems: 'flex-start' }}>
        {!phone && (
          <aside
            aria-label="Facets"
            style={{
              width: tablet ? 200 : 236,
              flex: 'none',
              border: '1px solid var(--border-default)',
              borderRadius: 'var(--radius-card)',
              background: 'var(--bg-surface)',
              padding: 'var(--space-4)',
            }}
          >
            {facetPanel}
          </aside>
        )}

        <div
          style={{
            flex: 1,
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            gap: 'var(--space-4)',
          }}
        >
          <Results
            loaded={data !== undefined}
            error={error}
            items={items}
            density={density}
            patched={patched}
            catalogNote={<CatalogEmptyNote />}
            onOpen={(id) => navigate(titlePath(id))}
            onRetry={async () => {
              await refetch()
            }}
          />
          {/* A page that failed *after* the first one leaves the list standing
              and puts the failure beside it: the items already loaded are real,
              and throwing them away to show an error would lose them. */}
          {data !== undefined && error !== null && !isRestarting(error) && (
            <Problem
              scale="panel"
              icon={<Icon name="server-off" size={20} />}
              problem={toProblemDocument(error, "We couldn't load any more of this list.")}
              {...traceOf(error)}
              onRetry={async () => {
                await fetchNextPage()
              }}
            />
          )}
          {data !== undefined && !isRestarting(error) && (
            <LoadMore
              autoLoad
              nextCursor={nextCursor}
              loading={isFetchingNextPage}
              onLoad={() => {
                void fetchNextPage()
              }}
              loadedLabel={`${items.length} loaded so far · there may be more`}
            />
          )}
        </div>
      </div>

      {phone && (
        <>
          <Button
            variant="secondary"
            block
            iconLeft={<Icon name="sliders-horizontal" size={16} />}
            aria-expanded={sheetOpen}
            onClick={() => setSheetOpen(true)}
          >
            Filters and facets
          </Button>
          {sheetOpen && <FacetSheet onClose={() => setSheetOpen(false)}>{facetPanel}</FacetSheet>}
        </>
      )}
    </div>
  )
}

/* --------------------------------------------------------------- controls */

interface ControlsProps {
  genre: string | null
  year: number | null
  owned: boolean | undefined
  sort: BrowseSort
  density: Density
  onGenre: (value: string | null) => void
  onYear: (value: string | null) => void
  onOwned: (value: boolean | undefined) => void
  onSort: (value: BrowseSort) => void
  onDensity: (value: Density) => void
}

function Controls({
  genre,
  year,
  owned,
  sort,
  density,
  onGenre,
  onYear,
  onOwned,
  onSort,
  onDensity,
}: ControlsProps) {
  return (
    <div style={{ display: 'flex', gap: 'var(--space-2x)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
      {genre !== null && <FilterChip label={genre} active removable onToggle={() => onGenre(null)} />}
      {year !== null && <FilterChip label={String(year)} active removable onToggle={() => onYear(null)} />}
      {/* The one three-state filter in the product, so it prints its state as a
          word: three states cannot be read off a border, and a checkbox has
          only two. */}
      <FilterChip label="Owned" tri value={owned} onToggle={onOwned} />
      <div style={{ width: 170 }}>
        <Select
          id="browse-sort"
          label="Sort"
          value={sort}
          options={SORTS}
          onChange={(event) => onSort(readSort(event.target.value))}
        />
      </div>
      <span style={{ marginLeft: 'auto', display: 'flex', gap: 2 }}>
        <IconButton
          label="List density"
          icon={<Icon name="list" size={20} />}
          outlined={density === 'list'}
          aria-pressed={density === 'list'}
          onClick={() => onDensity('list')}
        />
        <IconButton
          label="Grid density"
          icon={<Icon name="layout-grid" size={20} />}
          outlined={density === 'grid'}
          aria-pressed={density === 'grid'}
          onClick={() => onDensity('grid')}
        />
      </span>
    </div>
  )
}

/* ---------------------------------------------------------------- results */

interface ResultsProps {
  loaded: boolean
  error: unknown
  items: BrowseItem[]
  density: Density
  patched: ReadonlySet<string>
  catalogNote: ReactNode
  onOpen: (titleId: string) => void
  onRetry: () => Promise<void>
}

function Results({ loaded, error, items, density, patched, catalogNote, onOpen, onRetry }: ResultsProps) {
  const traceOf = useProblemTrace()

  /**
   * `invalid_cursor` is caught by `useCursorRestart` and is **never rendered**
   * (§3). Falling through to the skeleton is what the user sees: the list is
   * being rebuilt from the top, which is exactly what is happening.
   */
  if (isRestarting(error)) return <BrowseSkeleton density={density} />

  if (error !== null && error !== undefined && !loaded) {
    return (
      <Problem
        // A 422 here names a *query* parameter, and this screen has no form
        // field to hang an inline hint on — the filters live in the URL. Panel
        // scale with `errors[]` listed is what names the field instead.
        scale={scaleOf(error)}
        icon={<Icon name="x-circle" size={20} />}
        problem={toProblemDocument(error, 'That filter combination is not valid.')}
        {...traceOf(error)}
        {...(recoveryFor(codeOf(error) ?? '')?.retryable === true ? { onRetry } : {})}
      />
    )
  }

  if (!loaded) return <BrowseSkeleton density={density} />

  if (items.length === 0) {
    return (
      <StateBlock kind="empty" title="No titles match this filter" meta="items: [] · next_cursor: null">
        {catalogNote} Clearing the year filter is the usual fix.
      </StateBlock>
    )
  }

  if (density === 'grid') {
    return (
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill,minmax(var(--grid-poster-min),1fr))',
          gap: 'var(--space-4)',
        }}
      >
        {items.map((item) => (
          <PosterCard
            key={item.title_id}
            // `/browse` carries **no artwork key** by contract, so the card
            // draws its "no artwork on record" state rather than this screen
            // firing one image request per row to fill it in.
            card={{ title_id: item.title_id, name: item.name, year: item.year, kind: item.kind }}
            patched={patched.has(item.title_id)}
            onOpen={() => onOpen(item.title_id)}
          />
        ))}
      </div>
    )
  }

  return (
    <div
      style={{
        border: '1px solid var(--border-default)',
        borderRadius: 'var(--radius-card)',
        background: 'var(--bg-surface)',
        overflow: 'hidden',
      }}
    >
      {items.map((item, index) => (
        <div key={item.title_id} style={{ borderTop: index > 0 ? '1px solid var(--border-subtle)' : 'none' }}>
          <TitleRow
            title={{ title_id: item.title_id, name: item.name, year: item.year, kind: item.kind }}
            patched={patched.has(item.title_id)}
            onOpen={() => onOpen(item.title_id)}
            trailing={<Measurements item={item} />}
          />
        </div>
      ))}
    </div>
  )
}

/**
 * `popularity` and `vote_count` are both nullable and both stay nullable.
 * `null` is "nobody has measured this" — 980,523 of the 1,272,367 rows this
 * route was measured against — and rendering it as `0` would make it
 * indistinguishable from "measured, and unpopular" (ADR-0014, patterns.md §2).
 */
function Measurements({ item }: { item: BrowseItem }) {
  return (
    <span style={{ display: 'flex', gap: 'var(--space-2x)', alignItems: 'center' }}>
      {item.vote_count === null ? (
        <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>— never rated</span>
      ) : (
        <span className="u-mono" style={{ color: 'var(--text-secondary)' }}>
          {item.vote_count.toLocaleString('en-US')} votes
        </span>
      )}
      {item.popularity === null ? (
        <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
          — popularity never measured
        </span>
      ) : (
        <Badge mono outline>
          popularity {item.popularity.toFixed(2)}
        </Badge>
      )}
    </span>
  )
}

/** patterns.md §1: shaped like the layout that is coming, never a spinner. */
function BrowseSkeleton({ density }: { density: Density }) {
  if (density === 'grid') {
    return (
      <SkeletonRegion busy label="Loading the catalog …">
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill,minmax(var(--grid-poster-min),1fr))',
            gap: 'var(--space-4)',
          }}
        >
          {Array.from({ length: SKELETON_TILES }, (_, index) => (
            <div key={`tile-${index}`} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <Skeleton shape="block" height="auto" style={{ aspectRatio: '2 / 3' }} />
              <Skeleton shape="text" lines={2} />
            </div>
          ))}
        </div>
      </SkeletonRegion>
    )
  }
  return (
    <SkeletonRegion busy label="Loading the catalog …">
      <Skeleton shape="table" count={SKELETON_ROWS} />
    </SkeletonRegion>
  )
}

/* ----------------------------------------------------------------- facets */

interface FacetPanelProps {
  facets: BrowseFacets | undefined
  onPickGenre: (name: string) => void
  onPickYear: (year: string) => void
}

/**
 * The facet panel, and the reason it is unavailable when it is.
 *
 * patterns.md §2 fixes both sentences and they are not interchangeable:
 * `"unpredicated"` means nobody counted because nothing was filtered — the fix
 * is a filter — and `"not_requested"` means nobody counted because nobody
 * asked — the fix is `facets=true`. One sentence for both would send half the
 * readers to the wrong fix.
 */
function FacetPanel({ facets, onPickGenre, onPickYear }: FacetPanelProps) {
  if (facets === undefined) {
    return <Skeleton shape="text" lines={4} />
  }

  if (!facets.computed) {
    return facets.reason === 'unpredicated' ? (
      <StateBlock
        kind="never"
        title="Facet counts unavailable"
        icon={<Icon name="circle-dashed" />}
        meta='facets: { computed: false, reason: "unpredicated" }'
      >
        Counts are only computed once a filter is set — over 1.27M rows an unfiltered count would cost more
        than the page it decorates. Pick a genre or a year and they appear.
      </StateBlock>
    ) : (
      <StateBlock
        kind="never"
        title="Facet counts unavailable"
        icon={<Icon name="circle-dashed" />}
        meta='facets: { computed: false, reason: "not_requested" }'
      >
        Facets were not requested. This page asked the server for items only, so nothing counted them.
      </StateBlock>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-5)' }}>
      <FacetGroup label="Genre" counts={facets.genres} onPick={onPickGenre} />
      <FacetGroup label="Year" counts={facets.years} onPick={onPickYear} />
      <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
        Counts are for the current filter set, not the whole catalog.
      </span>
    </div>
  )
}

const FACET_LIMIT = 8

function FacetGroup({
  label,
  counts,
  onPick,
}: {
  label: string
  /** The schema declares this required; the wire omits it unless `computed`. */
  counts: { [key: string]: number } | undefined
  onPick: (key: string) => void
}) {
  const entries = Object.entries(counts ?? {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, FACET_LIMIT)

  if (entries.length === 0) {
    return (
      <StateBlock kind="empty" title={label} meta={`${label.toLowerCase()}: {}`}>
        Counted, and nothing in this filter set falls under a {label.toLowerCase()}.
      </StateBlock>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2x)' }}>
      <span className="u-eyebrow">{label}</span>
      {entries.map(([name, count]) => (
        <button
          key={name}
          type="button"
          onClick={() => onPick(name)}
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            gap: 'var(--space-3)',
            appearance: 'none',
            background: 'none',
            border: 'none',
            padding: '2px 0',
            cursor: 'pointer',
            color: 'var(--text-secondary)',
            font: 'var(--text-body-sm)',
          }}
        >
          <span>{name}</span>
          <span
            style={{
              font: 'var(--text-mono-xs)',
              color: 'var(--text-muted)',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {count.toLocaleString('en-US')}
          </span>
        </button>
      ))}
    </div>
  )
}

/**
 * The phone treatment of the facet panel (patterns.md §11).
 *
 * Deliberately **not** `aria-modal`: a modal owes the reader a focus trap, and
 * §9 says `Tab` is never trapped except inside one. This is a bottom sheet that
 * `Esc` closes through the shared layer stack, so it closes exactly one layer
 * and the page behind it stays reachable.
 */
function FacetSheet({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const heading = useRef<HTMLHeadingElement>(null)
  useLayer('sheet', true, onClose)

  useEffect(() => {
    heading.current?.focus({ preventScroll: true })
  }, [])

  return (
    <div
      role="dialog"
      aria-labelledby="browse-facet-sheet-title"
      style={{
        position: 'fixed',
        insetInline: 0,
        bottom: 0,
        zIndex: 'var(--z-modal)',
        maxHeight: '70dvh',
        overflowY: 'auto',
        background: 'var(--bg-raised)',
        borderTop: '1px solid var(--border-default)',
        borderStartStartRadius: 'var(--radius-card)',
        borderStartEndRadius: 'var(--radius-card)',
        padding: 'var(--space-4)',
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--space-4)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
        <h2
          id="browse-facet-sheet-title"
          ref={heading}
          tabIndex={-1}
          style={{ font: 'var(--text-title-sm)', color: 'var(--text-primary)' }}
        >
          Filters and facets
        </h2>
        <span style={{ marginLeft: 'auto' }}>
          <IconButton
            label="Close filters and facets"
            icon={<Icon name="x" size={20} />}
            touch
            onClick={onClose}
          />
        </span>
      </div>
      {children}
    </div>
  )
}

/* ------------------------------------------------------------ catalog size */

/**
 * The catalog's size is a real denominator and the only number on this screen —
 * it comes from `GET /admin/bootstrap/status`, never from counting a page.
 * When it has not been read, the sentence drops the number rather than guessing
 * one (§14).
 */
function useCatalogSize(): number | null {
  const { data } = useBootstrapStatus()
  return data?.titles ?? null
}

function CatalogNote() {
  const titles = useCatalogSize()
  return (
    <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-muted)' }}>
      {titles === null
        ? 'Results are not counted.'
        : `${titles.toLocaleString('en-US')} titles in the catalog. Results are not counted.`}
    </span>
  )
}

function CatalogEmptyNote() {
  const titles = useCatalogSize()
  return (
    <>
      {titles === null
        ? 'Nothing in the catalog matches this filter.'
        : `The catalog holds ${titles.toLocaleString('en-US')} titles, and none of them match this filter.`}
    </>
  )
}

/* ------------------------------------------------------------------- live */

/**
 * patterns.md §7. A `title.updated` frame adds a highlight class for 1000 ms
 * and does nothing else — no refetch, no reorder, no focus change. Every branch
 * on this screen is correct when zero frames ever arrive.
 */
function usePatchedTitles(): ReadonlySet<string> {
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

/* --------------------------------------------------------------- cursors */

/** `invalid_cursor` and nothing else. Its scale is `none`: it is never drawn. */
function isRestarting(error: unknown): boolean {
  return codeOf(error) === 'invalid_cursor'
}

/**
 * §4's third clause, and the reason it is an effect rather than a render
 * branch: a cursor carries a hash of the query, so an `invalid_cursor` means
 * the filters moved under an in-flight page. The list discards what it has and
 * re-requests from the top **silently**.
 *
 * The `pages.length > 0` guard is what stops that from becoming a loop. A 400
 * on page one is not a stale cursor — there is no cursor — so restarting would
 * re-issue the identical request forever; after a restart there are no pages,
 * so the guard closes behind it.
 */
function useCursorRestart(
  error: unknown,
  pageCount: number,
  queryClient: ReturnType<typeof useQueryClient>,
): void {
  useEffect(() => {
    if (!isRestarting(error) || pageCount === 0) return
    void queryClient.resetQueries({ queryKey: ['browse'] })
  }, [error, pageCount, queryClient])
}

/**
 * patterns.md §4's scroll restoration. Cursors are not durable, so this
 * re-requests from the top and chases the remembered offset for at most three
 * pages before settling for the top — never `scrollIntoView`.
 */
function useScrollMemory(
  key: string,
  pageCount: number,
  fetchNextPage: () => void,
  hasNextPage: boolean,
): void {
  const memoryKey = `browse:${key}`
  const pages = useRef(pageCount)
  useEffect(() => {
    pages.current = pageCount
  })

  useRestoreScroll(memoryKey, pageCount, fetchNextPage, hasNextPage)

  useEffect(
    () => () => rememberScroll(memoryKey, { offset: window.scrollY, pages: pages.current }),
    [memoryKey],
  )
}

/* ---------------------------------------------------------------- reading */

function readSort(value: string | null): BrowseSort {
  const match = SORTS.find((option) => option.value === value)
  return match?.value ?? 'name'
}

function readYear(value: string | null): number | null {
  if (value === null || !/^\d{4}$/.test(value)) return null
  return Number(value)
}

/** Three states: `true`, `false`, and absent — which means "either". */
function readOwned(value: string | null): boolean | undefined {
  if (value === 'true') return true
  if (value === 'false') return false
  return undefined
}

/* --------------------------------------------------------------- problems */

function codeOf(error: unknown): string | null {
  return error instanceof UsherProblem ? error.knownCode : null
}

/**
 * The seven-code table decides the scale, with one translation: `field` scale
 * assumes a field to attach to, and browse's inputs are URL parameters. The
 * panel lists `errors[].loc`/`.msg`, which names the field the other way round.
 */
function scaleOf(error: unknown): 'page' | 'panel' | 'inline' {
  const entry = recoveryFor(codeOf(error) ?? '')
  if (entry === null) return 'panel'
  if (entry.scale === 'page') return 'page'
  if (entry.scale === 'inline') return 'inline'
  return 'panel'
}

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
