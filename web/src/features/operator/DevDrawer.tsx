import { useMemo, useState, type ReactElement } from 'react'
import { Badge, Icon, IconButton, Input, StateBlock, Tabs, TextLink } from '@/design-system'
import { exercised, useJournal, type LogEntry } from '@/api'
import { useDevDrawer } from '@/app/dev-drawer-context'
import { useTraceUrl, useRuntimeConfig } from '@/app/runtime-config-context'
import { useLayer } from '@/patterns'
import './DevDrawer.css'

/**
 * The developer drawer — a request journal and an API coverage ledger.
 *
 * **App-global, not a route.** `App.tsx` renders one of these outside both
 * shells, because a receipt for a failed call outlives the screen that made it.
 *
 * **It sits at `--z-devdrawer` (700), above modals at 410, and that is
 * deliberate**: you have to be able to read the journal entry for the failed
 * call that put the modal on screen. It registers with the layer stack as a
 * `drawer` so `Esc` still closes exactly one layer, innermost first — the
 * registration happens here rather than in the provider so it only exists while
 * the drawer is open.
 *
 * **`aria-hidden` AND `inert` when closed**, not one of the two. `aria-hidden`
 * removes it from the accessibility tree and leaves its thirty-odd controls in
 * the tab order; `inert` takes them out of the tab order and, on its own, would
 * leave a screen reader announcing a drawer nobody can see. Both, or the
 * drawer is a tab-order trap on every screen in the product.
 *
 * **Nothing here un-redacts anything.** Credentials and playback ticket URLs
 * are removed at the record boundary in `devlog.ts`, before the journal ever
 * holds them, and this component renders what the journal holds. That ordering
 * is the point: a drawer whose whole purpose is to be read, screenshotted and
 * pasted into a bug report must not be the one place a live 300-second ticket
 * is legible.
 */

/**
 * Usher's 35 operations, spelled as `/openapi.json` declares them so the
 * strings match what `devlog.matchTemplate` records.
 *
 * **The ceiling is 34, not 35.** `GET /events` is an `EventSource`: it never
 * goes through `client.ts` and therefore can never be journalled, so its row
 * says so rather than sitting red forever and reading as a permanent failure.
 */
const OPERATIONS: readonly string[] = [
  'GET /home',
  'GET /browse',
  'GET /search',
  'GET /search/suggest',
  'GET /titles/{title_id}',
  'GET /titles/{title_id}/similar',
  'POST /titles/{title_id}/play',
  'GET /episodes/{episode_id}',
  'POST /episodes/{episode_id}/play',
  'GET /series/{title_id}/seasons',
  'GET /seasons/{season_id}/episodes',
  'GET /people/{person_id}',
  'GET /collections/{collection_id}',
  'GET /images/{image_id}',
  'GET /stream/{ticket}',
  'GET /events',
  'PUT /watch/titles/{title_id}',
  'POST /watch/titles/{title_id}/played',
  'DELETE /watch/titles/{title_id}/played',
  'PUT /watch/episodes/{episode_id}',
  'GET /admin/sources',
  'POST /admin/sources',
  'DELETE /admin/sources/{source_id}',
  'GET /admin/sources/{source_id}/status',
  'POST /admin/sources/{source_id}/sync',
  'GET /admin/unmatched',
  'POST /admin/unmatched/{media_item_id}/resolve',
  'GET /admin/bootstrap/status',
  'POST /admin/bootstrap/{phase}',
  'GET /admin/rows/providers',
  'PUT /admin/rows/providers/{slug}',
  'POST /admin/rows/regenerate',
  'GET /health',
  'GET /health/ready',
  'GET /meta/attribution',
]

/** The one operation no client-side journal can ever see. */
const UNREACHABLE_OPERATION = 'GET /events'

const COVERAGE_CEILING = OPERATIONS.length - 1

export function DevDrawer(): ReactElement {
  const { open, close } = useDevDrawer()
  const { tempoUrl } = useRuntimeConfig()
  const traceUrl = useTraceUrl()
  const [tab, setTab] = useState('journal')
  const [filter, setFilter] = useState('')
  const [selected, setSelected] = useState<number | null>(null)

  /**
   * `Esc` closes exactly one layer and this is the innermost when it is open.
   * Registered here rather than in the provider so an unopened drawer does not
   * swallow the `Esc` a dialog underneath it wanted.
   */
  useLayer('drawer', open, close)

  const journal = useJournal()
  /**
   * Read on every render rather than memoised: `useJournal` is what makes the
   * drawer re-render when `record` runs, and `exercised()` has no subscription
   * of its own to memoise against.
   *
   * **It is accumulated separately from `entries` and that is the point.** The
   * journal is trimmed to 300 records so the list stays readable; deriving
   * coverage from it would make an operation verified an hour ago go red once
   * enough browsing pushed its entry off the end, and coverage that decreases
   * as you test more is worse than none.
   */
  const covered = exercised()

  const rows = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    if (needle === '') return journal
    return journal.filter((entry) => `${entry.method} ${entry.path}`.toLowerCase().includes(needle))
  }, [journal, filter])

  const entry = rows.find((row) => row.id === selected) ?? rows[0]
  const coveredCount = OPERATIONS.filter((operation) => covered.has(operation)).length

  return (
    <aside
      className="u-devdrawer"
      data-open={open || undefined}
      data-density="compact"
      aria-hidden={!open}
      inert={!open}
      aria-label="Developer drawer"
    >
      <div className="u-devdrawer__head">
        <Icon name="terminal" size={16} />
        <span className="u-devdrawer__title">Developer</span>
        <span className="u-devdrawer__hint">⌘\ to toggle</span>
        <span className="u-devdrawer__close">
          <IconButton label="Close developer drawer" icon={<Icon name="x" size={16} />} onClick={close} />
        </span>
      </div>

      <div className="u-devdrawer__body">
        <Tabs
          id="devdrawer"
          value={tab}
          onChange={setTab}
          tabs={[
            { value: 'journal', label: 'Request journal', count: journal.length },
            { value: 'coverage', label: 'API coverage', count: coveredCount },
          ]}
        >
          {tab === 'journal' ? (
            <div className="flex flex-col gap-2">
              <Input
                id="devdrawer-filter"
                label="Filter by path or method"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                lead={<Icon name="list-filter" size={16} />}
                placeholder="/admin, POST, similar…"
              />

              {journal.length === 0 ? (
                <StateBlock kind="empty" title="No requests yet" meta="0 entries this session">
                  Every call this console makes lands here, redacted, with the response it got. Nothing has
                  been requested since the page loaded.
                </StateBlock>
              ) : rows.length === 0 ? (
                <StateBlock kind="empty" title="No entry matches" meta={`filter: "${filter}"`}>
                  {journal.length} entries are in the journal. The filter matches on the method and the path.
                </StateBlock>
              ) : (
                <ul className="u-devdrawer__list">
                  {rows.map((row) => (
                    <li key={row.id}>
                      <button
                        type="button"
                        className="u-devdrawer__entry"
                        aria-current={entry !== undefined && row.id === entry.id}
                        onClick={() => setSelected(row.id)}
                      >
                        <span className="u-devdrawer__t">{clock(row.startedAt)}</span>
                        <span className="u-devdrawer__method">{row.method}</span>
                        <span className="u-devdrawer__path">{row.path}</span>
                        <Status status={row.status} />
                        <span className="u-devdrawer__ms">{row.ms} ms</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              {entry !== undefined && (
                <div className="u-devdrawer__detail">
                  <div className="u-devdrawer__detail-head">
                    <Badge mono outline>
                      {entry.method} {entry.path}
                    </Badge>
                    <Status status={entry.status} />
                    {entry.problem && <Badge tone="warn">problem+json</Badge>}
                  </div>

                  {/* The operation key, spelled exactly as the coverage ledger
                      spells it, so the two halves of this drawer name the same
                      thing and a row that will not green can be traced to the
                      entry that should have greened it. */}
                  <span className="u-eyebrow">Operation</span>
                  <span className="u-devdrawer__template">
                    {entry.template === null
                      ? 'no template matched — this path is not in /openapi.json'
                      : `${entry.method} ${entry.template}`}
                  </span>

                  {entry.request !== undefined && entry.request !== null && (
                    <>
                      <span className="u-eyebrow">Request</span>
                      <pre className="u-devdrawer__pre">{format(entry.request)}</pre>
                    </>
                  )}

                  <span className="u-eyebrow">Response</span>
                  <pre className="u-devdrawer__pre">{format(entry.response)}</pre>

                  <span className="u-devdrawer__shield">
                    <Icon name="shield" size={16} />
                    Playback ticket URLs and credentials are redacted before the journal ever holds them —
                    they are never stored, never logged and never copyable from here.
                  </span>

                  <Trace entry={entry} tempoUrl={tempoUrl} traceUrl={traceUrl} />
                </div>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <p className="u-devdrawer__note">
                {coveredCount} of {OPERATIONS.length} operations exercised in this session. An operation
                greens only when it has actually been called — this is a session log, not a checklist.
              </p>
              <p className="u-devdrawer__note">
                The ceiling is {COVERAGE_CEILING} of {OPERATIONS.length}.{' '}
                <span className="u-mono">{UNREACHABLE_OPERATION}</span> is an{' '}
                <span className="u-mono">EventSource</span> and never goes through the request client, so it
                cannot be journalled here however many frames arrive. The live indicator in the sidebar is the
                evidence for that one.
              </p>
              <ul className="u-devdrawer__coverage">
                {OPERATIONS.map((operation) => {
                  const unreachable = operation === UNREACHABLE_OPERATION
                  const hit = covered.has(operation)
                  return (
                    <li
                      key={operation}
                      className={hit ? 'u-devdrawer__op u-devdrawer__op--hit' : 'u-devdrawer__op'}
                    >
                      <span className="u-devdrawer__op-icon">
                        <Icon name={hit ? 'check-circle' : 'circle-dashed'} size={16} />
                      </span>
                      <span className="u-devdrawer__op-name">{operation}</span>
                      {unreachable && !hit && (
                        <span className="u-devdrawer__op-note">not observable from the client</span>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          )}
        </Tabs>
      </div>
    </aside>
  )
}

/**
 * The trace affordance, in its three real states.
 *
 * **The id is `entry.traceId`, read off the response's `traceresponse`
 * header** — never out of the body. There is no `trace_id` member on any Usher
 * response and there is not going to be one: the id belongs to the transport,
 * `client.ts` parses the header into `LogEntry.traceId` for every recorded
 * call, and successes carry it as well as failures.
 *
 * Absent-not-dead is the rule in both directions: an unconfigured Tempo emits
 * **no anchor at all** and says why, and so does an entry with no trace id
 * (a transport failure that never reached the server, a span that was not
 * recording, or a proxy that stripped the header). A link that goes nowhere is
 * worse than no link, because it costs a click to find out.
 */
function Trace({
  entry,
  tempoUrl,
  traceUrl,
}: {
  entry: LogEntry
  tempoUrl: string | null
  traceUrl: (traceId: string) => string | null
}): ReactElement {
  const { traceId } = entry
  const href = traceId === null ? null : traceUrl(traceId)

  if (href !== null) {
    return (
      <span className="u-devdrawer__trace">
        <TextLink href={href} external>
          Open trace
        </TextLink>
        <span className="u-mono">{traceId}</span>
      </span>
    )
  }

  return (
    <span className="u-devdrawer__trace-absent">
      {tempoUrl === null
        ? 'Tempo is not configured on this deployment, so there is no trace link.'
        : 'This response carried no trace id, so there is nothing to open in Tempo.'}
    </span>
  )
}

/**
 * Status as hue **plus** the number, and never hue alone. A transport failure
 * is recorded as status 0 — "the request never left" and "the server said
 * nothing" look identical to somebody watching a spinner, so it gets its own
 * words rather than a zero.
 */
function Status({ status }: { status: number }): ReactElement {
  if (status === 0) {
    return (
      <Badge tone="bad" mono>
        no response
      </Badge>
    )
  }
  const tone = status >= 500 ? 'bad' : status >= 400 ? 'warn' : status === 202 ? 'info' : 'good'
  return (
    <Badge tone={tone} mono>
      {status}
    </Badge>
  )
}

/** `HH:MM:SS.mmm`, built rather than localised so the column never reflows. */
function clock(at: number): string {
  const date = new Date(at)
  const pad = (value: number, width = 2) => String(value).padStart(width, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(
    date.getMilliseconds(),
    3,
  )}`
}

/** Already-redacted content, pretty-printed. A string body is printed as it is. */
function format(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    // A body carrying a cycle is not worth taking the drawer down for.
    return String(value)
  }
}
