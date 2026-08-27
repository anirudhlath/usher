# Patterns and redlines

Phase 4. The cross-cutting behaviour a developer implements from, with the accessibility contract for
each. Everything here is already expressed in `components/` and `ui_kits/`; this is the specification
those files satisfy, written so it can be followed without reading them.

Conventions in this document: **MUST** is not negotiable, **SHOULD** has a stated exception,
`monospace` marks a real field, route or token name.

---

## 1. Loading

**No route-level spinners.** The reference client replaced the whole route with a centred spinner and
it flashed on every navigation. Loading is expressed three ways, chosen by *what* is pending:

| Pending thing | Treatment | Why |
|---|---|---|
| A route's data (browse page, table, rail) | `Skeleton` shaped like the final layout | measured 140–320 ms p95 — a skeleton that matches the layout reads as "arriving", a spinner reads as "restarting" |
| One action (`/play`, source probe, sync trigger) | `Button loading` + `loadingLabel` | measured 1–5 s. The page is not reloading; one thing is pending, and the pending thing should say so where it was clicked |
| A background refresh (SSE patch, poll) | nothing, then a 1000 ms highlight fade | the user did not ask; do not interrupt |

**Skeleton shapes per surface**

| Surface | Shape | Count |
|---|---|---|
| Home | `shape="rail"` per row, row title as a 20 px bar | 3 rows, 6 cards (2 on phone) |
| Browse list / any operator table | `shape="table"` | 8 rows |
| Browse grid | `shape="rail"` reflowed to the grid's column count | one screen's worth |
| Title detail | full-bleed block for the backdrop, then `shape="hero"` | 1 |
| Episode list | `shape="table"` with a 16:9 block in the leading cell | 4 |
| Insights panel | the panel chrome renders immediately; only the value and sparkline are skeletons | — |

**Rules**

- A skeleton MUST be `aria-hidden`; the region that owns it MUST carry `aria-busy="true"` and a
  visually-hidden "Loading …" label.
- Skeletons MUST NOT change size when real content lands. If the real height is unknown, use the
  minimum, never the maximum.
- One shared 1400 ms sweep (`--gradient-skeleton`). Reduced motion drops the sweep, keeps the shape.
- A cached-and-revalidating surface (`/home` is cached 30 s with an ETag) MUST show the cached content,
  never a skeleton. Stale-then-fresh, not blank-then-fresh.
- Home composes fast and is cached, so its skeleton is a first-paint artefact. Do not design *for* it;
  design so it is correct when it happens.

---

## 2. Absent, empty, never, unavailable

Four facts, four treatments (`StateBlock`). Choosing the wrong one is a correctness bug, not a
styling preference.

| Fact | API signal | Treatment | Voice |
|---|---|---|---|
| **Never computed** | `computed_at: null`, `facets.computed: false`, a metric with no samples, `expanded_query: null`, `watch_state: null` | dashed hairline, italic sentence, mono `meta` naming the field | "We have never computed similar titles for this one." |
| **Computed and empty** | `neighbors: []` with `computed_at` set, `items: []` | solid hairline on `--bg-sunken` | "Computed 3 days ago. Nothing scored close enough to show." |
| **Stale** | `stale: true`, a metric whose last sample is old | amber hairline, content still shown | "Computed before the scoring blend changed. Shown as they were." |
| **Not applicable** | field absent from the payload (`cast`, `crew`, `images`, `groups`) | em dash + one clause, no border | "— Collections are films only." |

**Rules**

- A single grey dash for all four is forbidden.
- `meta` names the field that proves the claim. It is the product's honesty made visible; do not drop
  it to reduce clutter.
- **Absent ≠ null.** An absent key means "not applicable to this record". A present `null` means "we
  looked and there is nothing". `cast` absent on a skeleton title is not the same as `cast: []`.
- Stale content is SHOWN, never hidden. `stale: true` on similar titles means the list is real and its
  inputs moved; suppressing it would be a bigger lie than showing it.
- `facets.computed: false` carries a `reason`: `"unpredicated"` → "Counts are only computed once a
  filter is set."; `"not_requested"` → "Facets were not requested." Two different sentences.

---

## 3. Errors

One component, four scales (`Problem`). The API's error set is **closed at seven codes**, so recovery
is a lookup, not a judgement.

| code | HTTP | Scale | Recovery |
|---|---|---|---|
| `not_found` | 404 | page | back + search. **No retry.** |
| `validation_failed` | 422 | inline per field, from `errors[].loc` / `.msg` | fix the field |
| `method_not_allowed` | 405 | panel | none — developer error, show it plainly |
| `invalid_cursor` | 400 | **never rendered** | silently restart the list from the top |
| `source_unavailable` | 503 | panel | retry, honour `Retry-After` |
| `not_playable` | 409 | panel | **no retry button.** Offer "See other copies" |
| `ticket_invalid` | 404 | inline strip | one tap re-requests and plays |

**Rules**

- `detail` MUST be shown and MUST NOT be parsed. It is prose the server may reword at any release, and
  it is frequently the only thing that explains what happened.
- Every rendered problem shows `code` and `status` in mono. An operator pastes those into a log query.
- `instance` is shown when present. It is the route that failed.
- **Trace link.** When the response carried a trace id, `Problem` MUST render "Open trace" into Tempo.
  This single link is what separates a console from a settings page.
- Retry MUST respect `Retry-After` and MUST disable itself while a retry is in flight.
- Toast scale is for failures the user did not trigger on this screen. It is `aria-live="polite"`,
  never `assertive`.
- A page-scale error MUST move focus to its heading and MUST leave the app chrome intact — the user is
  not trapped.

---

## 4. Keyset pagination

There are no page numbers, no totals, no result counts, and no "jump to page N" anywhere in this
product. A response gives `items` and an opaque `next_cursor`, which is `null` on the last page.

**Rules**

- `next_cursor === null` MUST produce a sentence: "That is everything we have for this filter."
  A silent stop is indistinguishable from a bug.
- Progress labels count **loaded**, never remaining: "72 loaded so far". There is no denominator.
- Changing a filter, sort or tab invalidates outstanding cursors. The client MUST drop the in-flight
  request, discard accumulated items and restart from the top. `invalid_cursor` MUST NOT reach the UI.
- **Auto-load vs button.** Viewer grids and rails auto-load at 600 px of approach (an
  `IntersectionObserver` sentinel). Operator tables keep the explicit button: fetching another 200
  rows into a dense table should be a decision.
- The sentinel MUST be inside the scroll container and MUST NOT be the last row (auto-loading on the
  last row makes the final page unreachable by keyboard).
- **Virtualisation** starts at 200 rendered rows. Below that, do not virtualise — it costs correct
  find-in-page and correct focus order for no gain. Above it, use a windowing list with a fixed row
  height taken from `--density-row-h`, and keep the real `<table>` semantics (row role, `aria-rowindex`).
- **Scroll restoration on back-navigation.** Cursors are not durable, so restoration is: remember the
  scroll offset and the number of loaded pages; on return, re-request from the top, and once page one
  paints, if the remembered offset is beyond it, keep fetching until either the offset is reachable or
  three pages have loaded, then restore. If it cannot be reached, restore to the top rather than
  jumping somewhere arbitrary. Never use `scrollIntoView`.

---

## 5. Destructive and expensive actions

One confirm pattern (`ConfirmDialog`) that names the **consequence**, the **cost**, the **duration**
and the **reversibility**. "Are you sure?" is banned.

The `facts` grid is the pattern. Every expensive action fills these four rows:

| Row | Example |
|---|---|
| what it does / downloads | `~224 MB from IMDb (regenerated daily)` |
| measured duration | `2 h 40 m on a cold run` |
| what it writes | `title skeletons, ~1.27M rows` |
| resumable / reversible | `yes — from the stored cursor` |

**Applies to:** source deletion, every bootstrap phase, full sync, delta sync, row regeneration,
releasing parked jobs.

**Rules**

- Durations MUST be measured, not estimated. If there is no measurement, say "not measured on this
  deployment" — never invent a range.
- `requireTyped` (type the source name) is reserved for source deletion, the only irreversible action:
  watch state survives, availability does not.
- Focus lands on the confirm button. `Esc` cancels, scrim click cancels, focus returns to the trigger.
- The dialog is `role="dialog" aria-modal="true"` with a labelled heading and a focus trap.
- Destructive confirm uses `danger-solid`; expensive-but-safe uses `primary`. An import is not a
  deletion and MUST NOT be red.

---

## 6. 202-shaped actions

Every mutating admin action returns `202 {kind, key}` and **there is no route to look that key up**.
The idiom is therefore: name what was queued, print the key, say whether it coalesced, and point at
where the result will appear.

```
Queued a full sync of Living Room
A full walk of the library. 41 minutes last time.
It coalesced with a job already running — nothing new was started.
key sync:full:0191f4c2-8a7e-7c31-b0d9-2f6a1e4c8b55
→ Watch it on Pipeline
```

**Rules**

- The word is **"Queued"**. Never "Done", never "Saved", never a checkmark alone.
- The key is printed in mono and selectable. Nothing can query it yet; an operator pastes it into a log
  search, so it MUST be copyable.
- Coalescing MUST be stated when known. "Nothing new was started" prevents an operator triggering it
  four more times.
- The destination link MUST go to the surface where evidence will appear (Pipeline for jobs, Bootstrap
  for imports, the source's own activity for syncs) even when that surface is itself
  REQUIRES BACKEND WORK — the pointer is honest about where to look.
- `aria-live="polite"`. Toasts persist until dismissed for 202s; they are receipts, not flashes.

---

## 7. Live data (SSE)

`GET /events` carries six event types. The bus is **in-process and lossy by design**: frames are
dropped when nobody is listening, on buffer overflow, and on restart.

**The governing rule: the UI MUST be fully correct if zero events ever arrive.** Live updates are
delight, never mechanism. Anything that only works because a frame arrived is a bug.

| Event | Payload | UI behaviour |
|---|---|---|
| `title.updated` | `title_id`, `episode_id?`, `fields[]` | patch that title in place, highlight fade. This is how enrichment lands on an open skeleton |
| `watchstate.updated` | `title_id`, `episode_id?`, `position_seconds`, `played`, `observed_at` | update progress. Fires for other devices too |
| `row.invalidated` | `slug` | refetch that row only. Never delivered to a `?titles=` subscriber |
| `sync.progress` | `source`, `kind`, `items_seen/matched/unmatched` | operator surfaces only |
| `bootstrap.progress` | the whole run — `dataset`, `phase` (the owning step), `requested_phase`, `status`, `revision`, `position`, `rows_seen`, `rows_written`, `error`, `started_at`, `heartbeat_at`, `finished_at` | operator surfaces. Patch the run in place. **No percent, no denominator** |
| `resync_required` | `reason` | discard local state, refetch, say so in the connection indicator |

**Frame arrival**

- A patched element gets `--live-patch-flash` for 1000 ms, opacity only. It MUST NOT move, resize or
  reorder — moving a row under a pointer that is about to click it is hostile.
- A patch MUST NOT steal focus and MUST NOT close an open menu, dialog or drawer.
- Reordering that a frame implies is deferred until the surface is idle (no hover, no focus inside) or
  until the next navigation. A rail never reshuffles while it is being read.
- No counters that tick, no "3 new items" pill, no firehose log. Frames are invisible except where they
  change a value someone is looking at.

**Connection state** (`LiveIndicator`): `connected` · `idle` · `reconnecting` · `off`.
A heartbeat comment arrives every 20 s, so **an idle stream is healthy** and says so —
"Live · quiet · nothing has changed since 14:22". Idle MUST NOT be drawn as a warning.
Reconnect with exponential backoff; announce only `reconnecting` and `resync_required` to
assistive tech (`aria-live="polite"`), never individual frames.

---

## 8. Progress without a denominator

Bootstrap and sync report a cursor. `GET /admin/bootstrap/status` returns `rows_seen`, `rows_written`
and `position`, and deliberately no total.

**A progress bar with an invented denominator is forbidden.** The idiom (`CursorProgress`) is:

- an indeterminate sweep that means "alive", not "n% complete";
- `rows_seen`, `rows_written`, `rows/sec`, `elapsed`, `last heartbeat`, `position` — six real numbers;
- `rows/sec` derived client-side from two polls, shown as `—` until there are two;
- the sentence "No completion estimate — the server reports a cursor, not a percentage.";
- `position` printed verbatim in mono: it is the resume point.

**Stall detection is the design's job.** `heartbeat_at` older than **120 s** renders "Stalled?" — with
the question mark, because the API states a timestamp and the inference is ours. The sweep stops and
turns amber; the copy says the run may have died and is resumable from this position.

**Live, not polled — and the fallback is visible.** `bootstrap.progress` carries the whole run and
fires on every committed batch *and* on start, completion and failure, so a connected stream needs no
cadence at all: a frame patches its dataset's card in place, and only a terminal frame costs a refetch,
because `titles` and the genome counts are not on the frame. While the stream is `connected` or `idle`
the screen MUST NOT poll, and says so.

**When the stream is `off` or `reconnecting`, fall back to the 10 s poll and name the mode.** §7's rule
is that the UI is fully correct if zero events ever arrive, and this is where that is paid: with
`usher work` in its own container the frames reach a `NullEventPublisher` and no client is ever told.
A fallback nobody can see is indistinguishable from a screen that has quietly stopped updating, so the
indicator says which of the two is running — never a bare "idle".

⚠️ **A poll gate of "only while something is `running`" is self-defeating after a trigger, and it
shipped that way.** `POST /admin/bootstrap/{phase}` returns 202 before the worker has claimed the job,
so the one refetch the mutation invalidates sees nothing running, the gate evaluates `false`, and the
query goes dormant for good. Measured on a real deployment: a `--phase all` press at 00:25:24 was
followed by a single status read and then **91 seconds of silence**, while all eight datasets imported
and completed. Any gate on this screen must be able to open on a state the screen has not observed
yet.

A `failed` run is a **normal, designed state**: status word in bad tone, `error` verbatim, position
retained, and the trigger relabelled "Resume".

---

## 9. Keyboard

| Key | Context | Action |
|---|---|---|
| `/` or `⌘K` / `Ctrl+K` | anywhere | focus search, open the suggest combobox |
| `↓` `↑` | combobox | move `aria-activedescendant` through options; `Esc` closes the listbox, a second `Esc` clears the field |
| `←` `→` | rail | move focus between cards; the rail scrolls to keep focus visible |
| `←` `→` `Home` `End` | tablist | roving tabindex, activate on move |
| `↑` `↓` | table / list | move row focus; `Enter` opens |
| `j` `k` | review queue | next / previous unmatched item |
| `Enter` | review queue | resolve to the selected candidate |
| `s` | review queue | skip |
| `⌘\` / `Ctrl+\` | anywhere | toggle the developer drawer |
| `Esc` | layered | closes exactly one layer, innermost first: listbox → popover → dialog → drawer → sheet |
| `Space` | player | play / pause |
| `Tab` | everywhere | never trapped except inside an open modal |

**Rules**

- Every screen starts with a skip link (`.u-skip-link`, `--z-skip-link`) to `#main`.
- **Focus on route change**: focus moves to the new `<main>`'s heading (`tabIndex={-1}`, focus, no
  scroll animation) and the route name is announced in a polite live region. Focus MUST NOT be left on
  the link that was clicked.
- `:focus-visible` only, on **everything** — cards, rails, table rows, icon buttons. 2 px ring at 2 px
  offset; inside a scroll container use the inset variant so the ring is never clipped.
- Focus MUST be visible against artwork: controls over an image carry `--border-control` as well.
- A card is one focusable button with a composed accessible name ("Stalker, 1979, partly watched").
  Do not nest a play button inside a focusable card.
- The developer drawer is `aria-hidden` **and** `inert` when closed, so its 30-odd controls are not in
  the tab order.

---

## 10. Density

One system, two moods, eight tokens. `[data-density="compact"]` changes only `--density-*`:
row height 44 → 32, control height 36 → 28, cell padding, gap, and the body step (15 → 13 px).

**Rules**

- Nothing except `--density-*` may branch on density. No component reads the attribute.
- Viewer surfaces are always comfortable. Operator surfaces default to compact.
- **Touch overrides density.** On a touch surface, hit targets stay ≥44 px regardless — use
  `IconButton touch`. Compact is a pointer-and-keyboard mode.
- Compact MUST NOT go below `--text-body-xs` (13 px) for anything a person reads a sentence in, or
  12 px for mono values.

---

## 11. Responsive

Three designed widths: **1440**, **834**, **390**.

| Element | 1440 | 834 | 390 |
|---|---|---|---|
| Viewer nav | header, inline links | header, inline links | bottom tab bar, 52 px + safe area |
| Operator nav | 240 px sidebar | 56 px icon rail | menu button → sheet |
| Browse facets | 236 px panel | 200 px panel | "Filters and facets" button → sheet |
| Admin tables (6–10 cols) | full table, sticky head | full table, horizontal scroll within the card | **stacked key/value cards** (`DataTable asCards`) |
| Rails | 168 px posters, 6 visible | 168 px, 4 visible | 132 px, 2.3 visible, snap scroll |
| Title hero | 420 px, poster beside backdrop | 330 px, poster beside | 260 px, no poster, title over backdrop |
| Review matcher | three panels side by side | queue + item, candidates below | one column, stacked in triage order |
| Dev drawer | 544 px docked right | 544 px docked right | full-screen sheet |

**Rules**

- A table MUST NOT shrink its font to fit. It becomes cards.
- Carousels get real touch scroll with `scroll-snap-type: x proximity` and no arrow buttons on touch.
- The phone header does not overflow — the reference client's did. Only the wordmark and two icon
  buttons live there; everything else is in the tab bar or a sheet.
- Nothing is hidden at a smaller width without an equivalent path to it.

---

## 12. Accessibility contract

Everything below is a requirement, and each was a measured failure in the reference client.

- **Contrast**: every sanctioned pair is in `guidelines/contrast.md` — 103 pairs, 0 failing.
  `--text-muted` is the lowest tone permitted for real content. `--text-disabled` may appear only on a
  control that is also `aria-disabled`.
- **No colour-only encoding**: every state is hue **+ icon + word**. The six state glyphs are fixed:
  `check-circle` good, `alert-triangle` warn, `x-circle` bad, `info` info, `circle-dashed` never
  computed, `history` stale.
- **Icon-only buttons**: `IconButton` cannot be constructed without `label`, which becomes both the
  accessible name and the tooltip.
- **Switches**: real `role="switch"`, `aria-checked`, bound label, plus a description that explains an
  opaque provider slug in plain language.
- **Combobox**: ARIA 1.2 pattern — `role="combobox"`, `aria-expanded`, `aria-controls`,
  `aria-autocomplete="list"`, `aria-activedescendant`, option ids, `Esc` to close. Both suggest tiers
  get group headers, because they are different queries and not a fallback chain.
- **Tabs**: real `tablist` / `tab` / `tabpanel`, roving tabindex, arrow keys, `Home`/`End`.
- **Tables**: `<caption>` (visually hidden is fine), `scope="col"`, `aria-sort` on sortable headers,
  `aria-selected` on the selected row.
- **Progress**: `role="progressbar"` with `aria-valuetext` in words. Where there is no denominator,
  `aria-valuenow` is omitted and `aria-valuetext` says so.
- **Live regions**: connection state and 202 receipts are `polite`. Nothing in this product is
  `assertive`. Individual SSE frames are never announced.
- **Motion**: `prefers-reduced-motion: reduce` collapses every duration to 1 ms, removes card lift and
  press scale, and stops the skeleton sweep and the heartbeat pulse. The patch highlight survives as a
  colour change with no travel.
- **Zoom**: no fixed pixel heights on text containers; 200 % zoom at 1440 must not clip. `rem` for type,
  `ch`/`ex` for measure.
- **Player**: keyboard-operable controls with names, captions button always present even when no track
  exists (it then explains that none was supplied).

---

## 13. Security and privacy in the design

- **Playback ticket URLs are secrets.** No copy button, no share affordance, no visible URL, no logging.
  `TargetPicker` has no code path that can print `target.url`; the developer drawer redacts it to
  `«redacted — 300 s playback ticket»` before the journal ever holds it.
- **Credentials** are write-only in the UI. The password field states where the value goes ("stored
  encrypted on the server, never returned by the API"). Configuration shows secrets as `•••• set`.
- **`is_administrator: true` is a risk surface, not a success.** Warn tone, with the sentence "Usher
  holds an administrator session on this server."
- **`device_id` is deliberately visible**, with the reason attached: it is how you find and revoke
  Usher's session in Emby's own dashboard.
- **The console is unauthenticated** and reachable on the LAN. Any auth surface is
  REQUIRES BACKEND WORK and stays out of the main flow.

---

## 14. Numbers and voice

- Every number carries its denominator or does not ship. `semantic_coverage` is rendered as "0.98 — of
  128,400 enriched titles, not of the 1,268,441-row catalog", never as a share of the library.
- Bootstrap gets counts and throughput, never a percentage.
- Lists get "72 loaded so far", never "72 of 400".
- **The one legitimate percentage in the product** is collection completion, because `owned_count` and
  `total_count` are both given by the API.
- Monospace for identifiers and measurements, never for prose or labels. Tabular numerals for anything
  that changes in place.
- Sentence case everywhere; `ALL CAPS` only as an 11 px tracked eyebrow. No emoji, no exclamation marks.

---

## 15. REQUIRES BACKEND WORK — the register

Designed, labelled on screen, and not implementable today.

| # | What | Route(s) needed | Where it appears |
|---|---|---|---|
| 1 | Sync history / activity timeline | `GET /admin/sources/{id}/runs` | Sources, Overview |
| 2 | Enable / disable a source | `PATCH /admin/sources/{id}` | Sources |
| 3 | Job & queue introspection | `GET /admin/jobs?kind=&state=`, `GET /admin/jobs/{key}`, `GET /admin/jobs/stats` | Pipeline, every 202 receipt |
| 4 | Release a parked job | `POST /admin/jobs/{id}/release` | Pipeline |
| 5 | Unmatched item detail | add `filename`, `container`, `resolution`, `runtime_seconds`, `library_name`, and the matcher's existing candidate scores to `GET /admin/unmatched` | Review queue |
| 6 | Provisioned dashboards & alerts | ship the five dashboards and seven alert rules as JSON in the compose stack | Insights |
| 7 | Auth | no user or auth concept exists in the API at all | out of the main flow |

Rule for all seven: the surface is designed and visibly labelled, so nobody builds a client against an
endpoint that does not exist, and nobody has to redesign the screen when it does.
