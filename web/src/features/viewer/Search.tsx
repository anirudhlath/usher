/**
 * Search — one field, three lanes, two suggest tiers.
 *
 * Four things on this screen are correctness rules rather than presentation:
 *
 * · **The two suggest tiers get their own group headers** (patterns.md §12,
 *   ADR-0031). `prefix` is a btree probe with 1.9% measured typo recall;
 *   `fuzzy` is trigram + Levenshtein at p50 33.6 ms. They are two different
 *   queries against two different indexes, **not a fallback chain**, and
 *   merging them would tell the reader that a trigram hit is a worse prefix
 *   hit — which is not what happened.
 * · **`mode !== requested_mode` is the server saying the lane you asked for
 *   could not run**, and it is the only signal that it did. A surface that does
 *   not compare them shows semantic results that are not semantic.
 * · **`semantic_coverage` is printed against its real denominator** (§14). It
 *   reads ~1.0 while the vector lane can answer for roughly a tenth of the
 *   catalog, because its denominator is the *enriched* tier and not the
 *   catalog. Rendering it as a share of the library would be a fabrication, and
 *   it is only printed at all once a vector lane actually ran: on a `full_text`
 *   answer the field is `0.0` by construction, which is a fact about the lane
 *   rather than a measurement of coverage.
 * · **Skeleton-tier results are first-class.** A row with a name, a year and
 *   nothing else is the majority of a 1.27M-title catalog, so it degrades by
 *   printing less and never by printing damage.
 *
 * The default mode is `fused`, and `useSearch` carries the 1,300-query
 * measurement that decided it — the selector keeps all three lanes because the
 * one case that still favours `full_text` is real.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Badge,
  Button,
  Icon,
  Problem,
  SearchCombobox,
  Select,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  TitleRow,
  type ProblemDocument,
  type SuggestGroup,
} from '@/design-system'
import {
  UsherProblem,
  fieldErrors,
  useBootstrapStatus,
  useEventStream,
  useSearch,
  useSuggest,
  type SearchMode,
  type SearchResponse,
  type SuggestResponse,
} from '@/api'
import { ROUTES, titlePath } from '@/app/routes'
import { useViewport } from '@/app/useViewport'
import { useProblemTrace } from '@/features/shared/trace'

const FIELD_ID = 'search-q'
const PATCH_HIGHLIGHT_MS = 1_000
/** patterns.md §1: search results are a table shape, 6 rows. */
const SKELETON_ROWS = 6
/** The fuzzy tier is the debounced one — the client debounces, the server does not. */
const FUZZY_DEBOUNCE_MS = 200

const MODES: { value: SearchMode; label: string }[] = [
  { value: 'fused', label: 'Fused (default)' },
  { value: 'full_text', label: 'Lexical' },
  { value: 'semantic', label: 'Semantic' },
]

/** The two tiers, named as what they are rather than as first and second try. */
const TIER_LABEL = {
  prefix: 'Starts with · answers every keystroke at 4+ characters',
  fuzzy: 'Close matches · trigram + Levenshtein, debounced',
} as const

export default function Search() {
  const navigate = useNavigate()
  const { phone } = useViewport()
  const [params, setParams] = useSearchParams()
  const patched = usePatchedTitles()

  const q = params.get('q') ?? ''
  const mode = readMode(params.get('mode'))

  const [value, setValue] = useState(q)
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)

  /**
   * The URL is the source of truth, so Back and a pasted link both have to move
   * the field; local state exists only for the keystrokes between submissions.
   * Adjusted during render rather than from an effect — the field must never
   * paint one frame showing the previous query.
   */
  const [lastQ, setLastQ] = useState(q)
  if (lastQ !== q) {
    setLastQ(q)
    setValue(q)
  }

  const debounced = useDebounced(value, FUZZY_DEBOUNCE_MS)
  const typed = value.trim()
  const prefix = useSuggest(open ? typed : '', 'prefix')
  const fuzzy = useSuggest(open ? debounced.trim() : '', 'fuzzy')
  const answer = useSearch(q, mode)

  useSearchHotkey(() => setOpen(true))

  const submit = useCallback(
    (text: string) => {
      const next = new URLSearchParams(params)
      if (text.trim() === '') next.delete('q')
      else next.set('q', text)
      setParams(next)
    },
    [params, setParams],
  )

  const setMode = useCallback(
    (next: SearchMode) => {
      const updated = new URLSearchParams(params)
      updated.set('mode', next)
      setParams(updated)
    },
    [params, setParams],
  )

  const groups = suggestGroups(prefix.data, fuzzy.data)

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
      <h1 style={{ font: 'var(--text-title-lg)', color: 'var(--text-primary)' }}>Search</h1>

      <div
        style={{
          display: 'flex',
          gap: 'var(--space-3)',
          alignItems: 'flex-end',
          flexWrap: phone ? 'wrap' : 'nowrap',
        }}
      >
        <div style={{ flex: 1, minWidth: 0, maxWidth: 620 }}>
          <label className="u-visually-hidden" htmlFor={FIELD_ID}>
            Search the catalog
          </label>
          <SearchCombobox
            id={FIELD_ID}
            value={value}
            onChange={setValue}
            open={open && groups.length > 0}
            onOpenChange={setOpen}
            groups={groups}
            loading={prefix.isFetching || fuzzy.isFetching}
            activeIndex={activeIndex}
            onActiveIndexChange={setActiveIndex}
            placeholder="Search the catalog  ·  press / anywhere"
            {...emptyMessageFor(typed, prefix.data)}
            onSubmit={(item) => {
              setOpen(false)
              setActiveIndex(-1)
              if ('free' in item) submit(item.free)
              else navigate(titlePath(item.title_id))
            }}
          />
        </div>
        <div style={{ width: 180, flex: 'none' }}>
          <Select
            id="search-mode"
            label="Mode"
            value={mode}
            options={MODES}
            onChange={(event) => setMode(readMode(event.target.value))}
          />
        </div>
      </div>

      <Answer
        q={q}
        error={answer.error}
        data={answer.data}
        patched={patched}
        onOpen={(id) => navigate(titlePath(id))}
        onBrowse={() => navigate(ROUTES.browse)}
      />
    </div>
  )
}

/* ----------------------------------------------------------------- answer */

interface AnswerProps {
  q: string
  error: unknown
  data: SearchResponse | undefined
  patched: ReadonlySet<string>
  onOpen: (titleId: string) => void
  onBrowse: () => void
}

function Answer({ q, error, data, patched, onOpen, onBrowse }: AnswerProps) {
  const traceOf = useProblemTrace()

  if (q.trim() === '') {
    return (
      <StateBlock kind="never" title="Nothing searched yet" meta="q: null">
        Type a query and press Enter. Both suggest tiers answer as you type; the full lanes only run once you
        submit.
      </StateBlock>
    )
  }

  if (error !== null && error !== undefined) {
    return (
      <Problem
        scale="page"
        icon={<Icon name="search-x" size={24} />}
        problem={toProblemDocument(error, "We couldn't find that.")}
        {...traceOf(error)}
        actions={
          <Button variant="secondary" onClick={onBrowse}>
            Browse instead
          </Button>
        }
      />
    )
  }

  if (data === undefined) {
    return (
      <SkeletonRegion busy label="Searching …">
        <Skeleton shape="table" count={SKELETON_ROWS} />
      </SkeletonRegion>
    )
  }

  return (
    <>
      <ModeNotice answer={data} />
      <Provenance answer={data} />
      {data.results.length === 0 ? (
        <StateBlock kind="empty" title="Nothing matched" meta={`mode: "${data.mode}" · results: []`}>
          {data.mode === 'fused'
            ? `Both lanes answered and neither found a match for “${data.query}”.`
            : `The ${data.mode} lane answered and found no match for “${data.query}”.`}{' '}
          Spelling is the usual cause — the fuzzy tier tolerates one or two transposed characters, not five.
        </StateBlock>
      ) : (
        <>
          <div
            style={{
              border: '1px solid var(--border-default)',
              borderRadius: 'var(--radius-card)',
              background: 'var(--bg-surface)',
              overflow: 'hidden',
            }}
          >
            {data.results.map((result, index) => (
              <div
                key={result.title_id}
                style={{ borderTop: index > 0 ? '1px solid var(--border-subtle)' : 'none' }}
              >
                <TitleRow
                  title={{
                    title_id: result.title_id,
                    name: result.name,
                    year: result.year,
                    kind: result.kind,
                  }}
                  patched={patched.has(result.title_id)}
                  onOpen={() => onOpen(result.title_id)}
                  trailing={
                    <span style={{ display: 'flex', gap: 'var(--space-2x)', alignItems: 'center' }}>
                      {result.popularity === null ? (
                        <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                          — popularity never measured
                        </span>
                      ) : (
                        <Badge mono outline>
                          popularity {result.popularity.toFixed(2)}
                        </Badge>
                      )}
                      <Badge tone={result.owned ? 'good' : 'neutral'}>
                        {result.owned ? 'owned' : 'catalog only'}
                      </Badge>
                    </span>
                  }
                />
              </div>
            ))}
          </div>
          <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)', maxWidth: '70ch' }}>
            Skeleton results are real catalog rows with a name and a year and nothing else — they are the
            majority of the catalog, not failures. Opening one asks the server to enrich it.
          </span>
        </>
      )}
    </>
  )
}

/**
 * The mode-narrowing notice.
 *
 * `requested_mode` beside `mode` is the degradation made visible, and both are
 * printed in mono because they are wire values an operator will grep for.
 */
function ModeNotice({ answer }: { answer: SearchResponse }) {
  if (answer.requested_mode === answer.mode) return null

  return (
    <div
      role="status"
      style={{
        display: 'flex',
        gap: 'var(--space-3)',
        alignItems: 'flex-start',
        padding: 'var(--space-3)',
        border: '1px solid var(--info-border)',
        background: 'var(--info-quiet)',
        borderRadius: 'var(--radius-card)',
      }}
    >
      <span style={{ color: 'var(--info-text)', flex: 'none', marginTop: 1 }}>
        <Icon name="info" size={20} />
      </span>
      <span style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <span style={{ font: 'var(--text-label)', color: 'var(--text-primary)' }}>
          {answer.mode === 'full_text'
            ? 'We narrowed this to lexical search.'
            : 'We narrowed the lane for this search.'}
        </span>
        <span
          style={{
            font: 'var(--text-body-sm)',
            color: 'var(--text-secondary)',
            maxWidth: '68ch',
          }}
        >
          You asked for <span className="u-mono">{answer.requested_mode}</span> and got{' '}
          <span className="u-mono">{answer.mode}</span>. The semantic lane can only answer for titles that
          have been embedded.
        </span>
      </span>
    </div>
  )
}

/**
 * What actually ran, and what the numbers on it are denominated in.
 *
 * `semantic_coverage` is only printed when a vector lane ran. On a `full_text`
 * answer the field reads `0.0` **by construction** — the lexical lane consults
 * no vectors — so printing "0.0 of 130,647 enriched titles" would report a
 * measurement that was never taken. §2's not-applicable treatment is the honest
 * one: an em dash and one clause.
 */
function Provenance({ answer }: { answer: SearchResponse }) {
  const { data: bootstrap } = useBootstrapStatus()
  const enriched = bootstrap?.genome.enriched ?? null
  const catalog = bootstrap?.titles ?? null
  const vectorLaneRan = answer.mode === 'semantic' || answer.mode === 'fused'

  return (
    <div
      style={{
        display: 'flex',
        gap: 'var(--space-3)',
        alignItems: 'center',
        flexWrap: 'wrap',
      }}
    >
      <Badge mono outline>
        mode {answer.mode}
      </Badge>
      {answer.search_id !== null && (
        <Badge mono outline>
          search_id {answer.search_id}
        </Badge>
      )}

      {vectorLaneRan ? (
        enriched === null || catalog === null ? (
          <StateBlock kind="never" title="Coverage has no denominator here" meta="semantic_coverage">
            The enriched-title count comes from <span className="u-mono">GET /admin/bootstrap/status</span>,
            which this page has not read. A coverage figure without it would be a share of nothing.
          </StateBlock>
        ) : (
          <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)', maxWidth: '70ch' }}>
            <span className="u-mono">semantic_coverage</span>{' '}
            <span className="u-mono">{answer.semantic_coverage.toFixed(2)}</span> — of{' '}
            {enriched.toLocaleString('en-US')} enriched titles, not of the {catalog.toLocaleString('en-US')}
            -row catalog.
          </span>
        )
      ) : (
        <StateBlock kind="na">
          <span className="u-mono">semantic_coverage</span> does not apply: the lexical lane consults no
          vectors, so the field reads 0.0 by construction rather than as a measurement.
        </StateBlock>
      )}

      {answer.expanded_query === null ? (
        <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)', maxWidth: '70ch' }}>
          Query expansion is off — it measured worse (MRR 0.733 → 0.373), so{' '}
          <span className="u-mono">expanded_query</span> is null by design.
        </span>
      ) : (
        <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)', maxWidth: '70ch' }}>
          Expanded to <span className="u-mono">{answer.expanded_query}</span> before the lanes ran.
        </span>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- suggest */

/**
 * One group per tier, always labelled, never merged.
 *
 * A tier that returned nothing contributes no group rather than an empty one:
 * "the prefix index has no answer" and "the prefix index was not consulted" are
 * different facts, and an empty labelled box asserts neither.
 */
function suggestGroups(
  prefix: SuggestResponse | undefined,
  fuzzy: SuggestResponse | undefined,
): SuggestGroup[] {
  const groups: SuggestGroup[] = []
  if (prefix !== undefined && prefix.results.length > 0) {
    groups.push({
      tier: 'prefix',
      label: TIER_LABEL.prefix,
      // `SuggestResultResponse` carries no enrichment tier, so `item.tier` is
      // omitted rather than guessed — the combobox then prints no tier chip.
      items: prefix.results.map((item) => ({
        title_id: item.title_id,
        name: item.name,
        year: item.year,
      })),
    })
  }
  if (fuzzy !== undefined && fuzzy.results.length > 0) {
    groups.push({
      tier: 'fuzzy',
      label: TIER_LABEL.fuzzy,
      items: fuzzy.results.map((item) => ({
        title_id: item.title_id,
        name: item.name,
        year: item.year,
      })),
    })
  }
  return groups
}

/**
 * "We did not look" is a different sentence from "we looked and found none",
 * which is why `min_query_length` is on the wire at all.
 */
function emptyMessageFor(typed: string, prefix: SuggestResponse | undefined): { emptyMessage?: string } {
  if (prefix === undefined || typed.length >= prefix.min_query_length) return {}
  return {
    emptyMessage: `Nothing was looked up — suggest needs at least ${prefix.min_query_length} characters. Press Enter to search the full catalog.`,
  }
}

function useDebounced(value: string, ms: number): string {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return debounced
}

/**
 * patterns.md §9: `/` or `⌘K`/`Ctrl+K` focuses search and opens the combobox.
 * `/` is ignored while something is already being typed into, or the shortcut
 * would eat the character.
 */
function useSearchHotkey(onOpen: () => void): void {
  const openRef = useRef(onOpen)
  useEffect(() => {
    openRef.current = onOpen
  })

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target
      const typing =
        target instanceof HTMLElement &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
      const shortcut =
        (event.key === '/' && !typing) ||
        ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k')
      if (!shortcut) return
      event.preventDefault()
      document.getElementById(FIELD_ID)?.focus()
      openRef.current()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])
}

/* ------------------------------------------------------------------- live */

/**
 * patterns.md §7 — delight, never mechanism. A `title.updated` frame adds a
 * 1000 ms highlight class to the matching row and does nothing else: no
 * refetch, no reorder, no focus change. Every branch above is correct when this
 * returns an empty set forever.
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

/* ---------------------------------------------------------------- reading */

function readMode(value: string | null): SearchMode {
  const match = MODES.find((option) => option.value === value)
  return match?.value ?? 'fused'
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
