import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Badge,
  Button,
  Icon,
  LoadMore,
  Problem,
  SearchCombobox,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  TitleRow,
  type ProblemDocument,
  type SuggestGroup,
  type SuggestItem,
} from '@/design-system'
import { BackendWork, OpsHeader, OpsSection } from '@/app/shells/OperatorShell'
import { useViewport } from '@/app/useViewport'
import {
  UsherProblem,
  fieldErrors,
  useResolveUnmatched,
  useSuggest,
  useUnmatched,
  type SuggestResponse,
} from '@/api'
import { useToasts } from '@/patterns'
import { useProblemTrace } from '@/features/shared/trace'

/**
 * The review queue — the two-panel matcher.
 *
 * **This is the weakest surface in the API and the biggest design opportunity.**
 * `GET /admin/unmatched` hands back an `external_id`, a `source_id`, two
 * timestamps and an availability flag, and *no candidates at all* — while the
 * matcher that gave up on the file already computed confidence scores it does
 * not return. So the screen is organised around what it can honestly do: name
 * the handle for what it is, then let an operator search the catalog for the
 * title by hand, keyboard-first.
 *
 * The register's item 5 (patterns.md §15) is printed on the left panel with the
 * five missing fields in mono. Nothing here fabricates one of them.
 */
export default function Review() {
  const { phone } = useViewport()
  const traceOf = useProblemTrace()
  const queue = useUnmatched(undefined, QUEUE_PAGE_SIZE)
  const { mutate: resolveItem, isPending: resolving, error: resolveError } = useResolveUnmatched()
  const { notice } = useToasts()

  const queueRef = useRef<HTMLDivElement>(null)
  const [index, setIndex] = useState(0)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const [candidate, setCandidate] = useState<SuggestItem | null>(null)

  const items = useMemo(() => queue.data?.pages.flatMap((page) => page.items) ?? [], [queue.data])
  // A page shrinks under the cursor whenever a resolve removes its own row, so
  // the cursor is clamped **during render** rather than corrected afterwards by
  // an effect: a state write in an effect renders the out-of-range frame first.
  const cursor = items.length === 0 ? 0 : Math.min(index, items.length - 1)
  const item = items[cursor]

  /**
   * The two tiers are two different queries against two different indexes
   * (ADR-0031), never a fallback chain — which is why they are fetched
   * separately and rendered under separate group headers.
   */
  const prefix = useSuggest(query, 'prefix')
  const fuzzy = useSuggest(query, 'fuzzy')

  const groups = useMemo<SuggestGroup[]>(() => {
    const built: SuggestGroup[] = []
    const fromPrefix = toGroup('prefix', 'Starts with — the as-you-type index', prefix.data)
    const fromFuzzy = toGroup('fuzzy', 'Close to it — trigram and edit distance', fuzzy.data)
    if (fromPrefix) built.push(fromPrefix)
    if (fromFuzzy) built.push(fromFuzzy)
    return built
  }, [prefix.data, fuzzy.data])

  const focusQueueItem = useCallback((at: number) => {
    queueRef.current?.querySelector<HTMLElement>(`[data-queue-index="${at}"]`)?.focus()
  }, [])

  /** `j` / `k` / `s` all land here. Roving focus follows the cursor. */
  const move = useCallback(
    (delta: number) => {
      if (items.length === 0) return
      const next = Math.min(items.length - 1, Math.max(0, cursor + delta))
      setIndex(next)
      focusQueueItem(next)
    },
    [cursor, focusQueueItem, items.length],
  )

  const resolveSelected = useCallback(() => {
    if (!item || !candidate || resolving) return
    const external = item.external_id
    const name = candidate.name
    resolveItem(
      { id: item.id, body: { title_id: candidate.title_id } },
      {
        onSuccess: () => {
          // **Not a receipt.** This is the one admin write that answers 200 with
          // the row it wrote rather than 202 with a key, so there is no key to
          // print and printing one would be an invention.
          notice({
            tone: 'good',
            title: `Resolved ${external} → ${name}`,
            detail:
              'The copy is attached to the catalog title. This route answers 200 with the row it wrote, not a 202, so there is no job key to keep.',
          })
          setCandidate(null)
          setQuery('')
          setOpen(false)
          setActive(-1)
        },
      },
    )
  }, [candidate, item, notice, resolveItem, resolving])

  /**
   * patterns.md §9's triage keys. Two guards make them safe to bind globally:
   * they never fire while a text field has focus (the candidate search is a text
   * field, and `j` is a letter), and `Enter` defers to whatever button or link
   * actually has focus unless that focus is inside the queue itself.
   */
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return
      if (isTextEntry(document.activeElement)) return
      if (event.key === 'j') {
        event.preventDefault()
        move(1)
      } else if (event.key === 'k') {
        event.preventDefault()
        move(-1)
      } else if (event.key === 's') {
        event.preventDefault()
        move(1)
      } else if (event.key === 'Enter') {
        const focused = document.activeElement
        const inQueue = focused !== null && queueRef.current?.contains(focused) === true
        if (!inQueue && isActivatable(focused)) return
        event.preventDefault()
        resolveSelected()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [move, resolveSelected])

  const lastPage = queue.data?.pages[queue.data.pages.length - 1]

  return (
    <>
      <OpsHeader
        title="Review queue"
        subtitle="Files a walk reported that no match resolved. They are never dropped — they land here."
      />
      <div className="u-ops__body">
        {queue.isError ? (
          <Problem
            problem={asProblem(queue.error)}
            {...traceOf(queue.error)}
            icon={<Icon name="x-circle" size={20} />}
            onRetry={() => {
              void queue.refetch()
            }}
          />
        ) : queue.isPending ? (
          <SkeletonRegion busy label="Loading the review queue …">
            <Skeleton shape="table" count={8} />
          </SkeletonRegion>
        ) : items.length === 0 ? (
          <OpsSection title="Nothing is waiting for review">
            <p
              style={{
                font: 'var(--text-body)',
                color: 'var(--text-secondary)',
                maxWidth: 'var(--width-prose)',
              }}
            >
              Every file both sources reported matched a catalog title. Matching runs locally against the
              bootstrapped skeleton first and only falls back to the provider search as a last resort, so this
              queue stays short when the catalog is well built.
            </p>
            <StateBlock kind="empty" meta="items: [] · next_cursor: null">
              Files that cannot be matched are never dropped — they land here. An empty queue means the
              pipeline agreed with itself, not that nothing was scanned.
            </StateBlock>
          </OpsSection>
        ) : (
          <>
            <div
              style={{
                display: 'flex',
                gap: 'var(--space-3)',
                alignItems: 'center',
                flexWrap: 'wrap',
              }}
            >
              <span className="u-mono" style={{ color: 'var(--text-primary)' }}>
                {cursor + 1} / {items.length} loaded so far
              </span>
              <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                keyset-paged, up to {QUEUE_PAGE_SIZE} at a time — there is no total
              </span>
              <span
                style={{
                  marginLeft: 'auto',
                  display: 'flex',
                  gap: 'var(--space-2x)',
                  alignItems: 'center',
                  font: 'var(--text-body-xs)',
                  color: 'var(--text-muted)',
                }}
              >
                <Key>j</Key>
                <Key>k</Key> move · <Key>Enter</Key> resolve · <Key>s</Key> skip
              </span>
            </div>

            {/* 1440: three panels side by side. 834: queue + item, candidates
                below. 390: one column, stacked in triage order (§11). */}
            <div className="grid gap-4 md:grid-cols-[220px_minmax(0,1fr)] xl:grid-cols-[220px_minmax(0,1fr)_minmax(0,1.2fr)]">
              <Panel label="The queue">
                <div ref={queueRef}>
                  <ul aria-label="Unmatched files" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
                    {items.map((one, at) => (
                      <li key={one.id}>
                        <button
                          type="button"
                          data-queue-index={at}
                          // Roving tabindex: the queue is one tab stop, and `j`
                          // and `k` move the focus inside it (§9).
                          tabIndex={at === cursor ? 0 : -1}
                          aria-current={at === cursor ? true : undefined}
                          onClick={() => {
                            setIndex(at)
                          }}
                          style={{
                            appearance: 'none',
                            width: '100%',
                            textAlign: 'left',
                            cursor: 'pointer',
                            background: at === cursor ? 'var(--bg-selected)' : 'none',
                            border: 'none',
                            borderTop: at ? '1px solid var(--border-subtle)' : 'none',
                            padding: 'var(--space-2x) var(--space-3)',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: 'var(--space-2)',
                          }}
                        >
                          <span className="u-mono" style={{ color: 'var(--text-primary)' }}>
                            {one.external_id}
                          </span>
                          <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                            {one.available ? 'still on the source' : 'gone, still remembered'}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                  {/* Button mode, never auto-load: fetching another page into a
                      dense operator table is a decision (§4). */}
                  <div style={{ padding: 'var(--space-2x)' }}>
                    <LoadMore
                      nextCursor={lastPage?.next_cursor ?? null}
                      loading={queue.isFetchingNextPage}
                      onLoad={() => {
                        void queue.fetchNextPage()
                      }}
                      loadedLabel={`${items.length} loaded so far`}
                    />
                  </div>
                </div>
              </Panel>

              <Panel label="Unmatched file">
                {item && (
                  <>
                    <div>
                      <span
                        className="u-mono"
                        style={{ font: 'var(--text-metric-sm)', color: 'var(--text-primary)' }}
                      >
                        {item.external_id}
                      </span>
                      {/* §15 item 5's whole point: this string is a *handle*,
                          and a screen that prints it without saying so has told
                          the operator nothing. */}
                      <p
                        style={{
                          font: 'var(--text-body-xs)',
                          color: 'var(--text-muted)',
                          marginTop: 'var(--space-1)',
                          textWrap: 'pretty',
                        }}
                      >
                        This is the handle the source uses — the media server&rsquo;s own id for the file, not
                        a catalog title id and not anything you can look up in Usher. Open that server&rsquo;s
                        dashboard → Devices → Library and search this id to see the file itself.
                      </p>
                    </div>
                    <dl
                      style={{
                        display: 'grid',
                        gridTemplateColumns: 'auto minmax(0, 1fr)',
                        gap: 'var(--space-2x) var(--space-4)',
                        margin: 0,
                      }}
                    >
                      <Fact label="source">
                        <span className="u-mono">{item.source_id}</span>
                      </Fact>
                      <Fact label="first seen">
                        {item.added_at == null ? (
                          <StateBlock kind="na">
                            No arrival was recorded. A delta walk sees an item without seeing it arrive.
                          </StateBlock>
                        ) : (
                          <span className="u-mono">{item.added_at}</span>
                        )}
                      </Fact>
                      <Fact label="last seen">
                        <span className="u-mono">{item.last_seen_at}</span>
                      </Fact>
                      <Fact label="available">
                        <Badge tone={item.available ? 'good' : 'warn'}>
                          {item.available ? 'still on the source' : 'gone, still remembered'}
                        </Badge>
                      </Fact>
                    </dl>

                    <BackendWork routes="GET /admin/unmatched — add filename, container, resolution, runtime_seconds, library_name and the matcher's candidate scores">
                      Everything above is everything the API gives. A filename alone would resolve most of
                      this queue without a search; container and runtime would let the client rank candidates
                      itself; and the matcher already computes confidence scores it does not return. The
                      fields this panel needs are <span className="u-mono">filename</span>,{' '}
                      <span className="u-mono">container</span>, <span className="u-mono">resolution</span>,{' '}
                      <span className="u-mono">runtime_seconds</span> and{' '}
                      <span className="u-mono">library_name</span>, plus the matcher&rsquo;s existing
                      candidate scores.
                    </BackendWork>
                  </>
                )}
              </Panel>

              <div className="md:col-span-2 xl:col-span-1">
                <Panel label="Find the catalog title">
                  {/* The queue offers no candidates at all, and that absence is a
                      fact about the API rather than a fact about this file. */}
                  <StateBlock
                    kind="never"
                    title="No candidates were offered"
                    meta="GET /admin/unmatched — no candidates, no scores"
                  >
                    The queue proposes nothing to match against, so every resolution on this screen starts
                    from a search you type. The matcher that gave up on this file scored its own candidates
                    and the route does not return them.
                  </StateBlock>

                  <label className="u-visually-hidden" htmlFor={COMBOBOX_ID}>
                    Search the catalog for the matching title
                  </label>
                  <SearchCombobox
                    id={COMBOBOX_ID}
                    value={query}
                    onChange={(next) => {
                      setQuery(next)
                      setActive(-1)
                    }}
                    onSubmit={(picked) => {
                      if ('title_id' in picked) {
                        setCandidate(picked)
                        setOpen(false)
                        setActive(-1)
                      }
                    }}
                    groups={groups}
                    loading={prefix.isFetching || fuzzy.isFetching}
                    open={open}
                    onOpenChange={setOpen}
                    activeIndex={active}
                    onActiveIndexChange={setActive}
                    placeholder="Search the catalog for the matching title"
                    emptyMessage="Neither tier matched. Both search the whole 1.27M-title catalog, skeletons included."
                  />

                  {candidate ? (
                    <TitleRow
                      title={{
                        title_id: candidate.title_id,
                        name: candidate.name,
                        // An absent `year` and a `null` one are different facts
                        // and `exactOptionalPropertyTypes` keeps them apart:
                        // the row prints an em dash for `null` and nothing at
                        // all for a key that was never on the wire.
                        ...(candidate.year === undefined ? {} : { year: candidate.year }),
                      }}
                      trailing={<Badge tone="info">selected</Badge>}
                    />
                  ) : (
                    <p style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                      Nothing is selected yet. The two tiers are two different queries and carry their own
                      group headers; a score means a rank inside one answer and nothing at all between them.
                    </p>
                  )}

                  {resolveError !== null && (
                    <Problem
                      problem={asProblem(resolveError)}
                      {...traceOf(resolveError)}
                      scale="panel"
                      icon={<Icon name="x-circle" size={20} />}
                    />
                  )}

                  <div
                    style={{
                      display: 'flex',
                      gap: 'var(--space-2x)',
                      flexWrap: 'wrap',
                      marginTop: 'auto',
                    }}
                  >
                    <Button
                      variant="primary"
                      size="sm"
                      iconLeft={<Icon name="link" size={16} />}
                      loading={resolving}
                      loadingLabel="Resolving…"
                      disabled={!candidate || !item}
                      onClick={resolveSelected}
                    >
                      {candidate ? `Resolve to ${candidate.name}` : 'Resolve to the selected candidate'}
                    </Button>
                    <Button variant="secondary" size="sm" onClick={() => move(1)}>
                      Skip
                    </Button>
                  </div>
                  <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                    Resolving posts the catalog title&rsquo;s internal id. For an episode, pick the series
                    first and the episode second — the API takes both.
                  </span>
                </Panel>
              </div>
            </div>
            {phone && (
              <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                Stacked in triage order: the queue, then the file, then the search.
              </span>
            )}
          </>
        )}
      </div>
    </>
  )
}

/** 200 rows at a time, and the next 200 is a button rather than a scroll (§4). */
const QUEUE_PAGE_SIZE = 200

const COMBOBOX_ID = 'review-candidate-search'

function Panel({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section
      aria-label={label}
      style={{
        border: '1px solid var(--border-default)',
        borderRadius: 'var(--radius-card)',
        background: 'var(--bg-surface)',
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--space-3)',
        padding: 'var(--space-4)',
        minWidth: 0,
      }}
    >
      <span className="u-eyebrow">{label}</span>
      {children}
    </section>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt style={{ font: 'var(--text-label-sm)', color: 'var(--text-muted)' }}>{label}</dt>
      <dd style={{ margin: 0, minWidth: 0, overflowWrap: 'anywhere' }}>{children}</dd>
    </>
  )
}

function Key({ children }: { children: ReactNode }) {
  return (
    <kbd
      style={{
        font: 'var(--text-mono-xs)',
        border: '1px solid var(--border-control)',
        borderBottomWidth: 2,
        borderRadius: 'var(--radius-xs)',
        padding: '0 4px',
        color: 'var(--text-secondary)',
        background: 'var(--bg-surface)',
      }}
    >
      {children}
    </kbd>
  )
}

function toGroup(
  tier: 'prefix' | 'fuzzy',
  label: string,
  data: SuggestResponse | undefined,
): SuggestGroup | null {
  if (!data || data.results.length === 0) return null
  return {
    tier,
    label,
    // No `tier` on the item: `SuggestResultResponse` carries no
    // `enrichment_state`, and guessing one would print a tier the API never
    // stated.
    items: data.results.map((one) => ({
      title_id: one.title_id,
      name: one.name,
      year: one.year,
    })),
  }
}

/**
 * A text field owns its own keystrokes. `j`, `k` and `s` are letters, so a
 * triage binding that did not check this would make the candidate search
 * untypeable.
 */
function isTextEntry(node: Element | null): boolean {
  if (node === null) return false
  if (node instanceof HTMLTextAreaElement || node instanceof HTMLSelectElement) return true
  if (node instanceof HTMLElement && node.isContentEditable) return true
  if (node instanceof HTMLInputElement) {
    const type = node.type.toLowerCase()
    return (
      type !== 'button' && type !== 'checkbox' && type !== 'radio' && type !== 'submit' && type !== 'reset'
    )
  }
  return false
}

/** `Enter` on a focused control belongs to that control, not to the triage. */
function isActivatable(node: Element | null): boolean {
  return node instanceof HTMLButtonElement || node instanceof HTMLAnchorElement
}

/**
 * `UsherProblem` onto the design system's document. `detail` is passed through
 * verbatim and never parsed; `code` and `status` are always rendered, because an
 * operator pastes those into a log query (patterns.md §3).
 */
function asProblem(error: unknown): ProblemDocument {
  if (!(error instanceof UsherProblem)) {
    return {
      status: 0,
      title: 'The request never reached the server.',
      detail: String(error),
    }
  }
  const errors = fieldErrors(error.errors).map((one) => ({ loc: [one.field], msg: one.message }))
  return {
    status: error.status,
    detail: error.detail,
    ...(error.knownCode === null ? {} : { code: error.knownCode }),
    ...(error.instance === undefined ? {} : { instance: error.instance }),
    ...(error.retryAfter === null ? {} : { retry_after: error.retryAfter }),
    ...(errors.length === 0 ? {} : { errors }),
  }
}
