# Handoff: Usher Console

## Overview

Usher Console is the web client for **Usher** — a self-hosted media catalog server that sits in front
of a media server (Emby today) and adds a canonical catalog of ~1.27M titles, hybrid search,
precomputed similarity, and a server-composed home screen.

One app, two hats, both in this bundle:

- **Viewer** — browse, search, open a title, press Play, track progress. Reads as a premium
  streaming app. Dark theme only, comfortable density. 9 screens.
- **Operator** — the same person connects a media server, runs dataset imports, drains a review
  queue, toggles row providers, reads metrics. Reads as a control room. Light theme by default
  (dark available), compact density. 9 surfaces including a developer drawer.

The governing product rule, and the thing most likely to be lost in implementation:
**the UI never lies about what it knows.** "We have never computed this" is drawn differently from
"we computed this and it is empty". Every number is labelled with what it was measured against.
Nothing is rounded up into a percentage the server refused to supply. If you implement one thing
from this bundle beyond the pixels, implement that — it lives in
`guidelines/patterns.md` §2, §8 and §14, and in the `StateBlock` / `CursorProgress` components.

## About the design files

Everything in this bundle is a **design reference authored in HTML/CSS + browser-transpiled React**.
It is a prototype of intended look and behaviour, **not production code to lift**. The `.jsx` files
run through Babel in the browser off a CDN, have no build step, no tests, no data layer, and mock
payloads in `data.js`.

The job is to **recreate these designs in the target codebase's own environment** — React, Vue,
Svelte, SwiftUI, native, whatever exists — using its established patterns, router, data layer and
component conventions. If no frontend environment exists yet (as of the last upstream sync, the
`anirudhlath/usher` repository contains no frontend at all — 714 files of Python, Markdown, SQL and
config), choose the framework and pick up the two files that are meant to be ported directly:

- `styles.css` + `tokens/*.css` — the token layer. **Port this as-is.** It is plain CSS custom
  properties with no dependencies and it is the contract every screen is built against.
- `implementation/tailwind-theme.css` — the same tokens as a Tailwind v4 CSS-first `@theme` block,
  with `light:` / `compact:` variants and utilities. Use this instead if the target is Tailwind v4.

The component `.jsx` files are readable reference implementations: correct ARIA, correct states,
correct class names. Read them, port the behaviour, discard the loading mechanism.

## Fidelity

**High fidelity.** Colours, type steps, spacing, radii, shadows, durations, easings and z-index are
final and tokenised — recreate them exactly, via the token layer rather than by copying literals.
Component states (rest / hover / press / focus / disabled / loading / error) are all specified and
demonstrated. Copy is final and should be used verbatim; it is written to a voice
(`design-system-rationale.md` § Content fundamentals) and rewriting it usually breaks a correctness
rule.

Two things in the bundle are **not** final:

1. **All imagery is placeholder.** `data.js` in both kits generates deterministic gradient tiles
   standing in for the image proxy. Real artwork comes from `/images/{id}?w=` at exactly four
   widths — 154 / 342 / 780 / 1280. Nothing else about the layout changes when you swap them.
2. **The TMDb logo** on the viewer About screen is a marked empty slot. TMDb's terms require their
   logo next to their disclaimer and it must come from TMDb's own brand page. It is the one asset
   this system cannot originate.

## Screens / views

Open `ui_kits/viewer/index.html` and `ui_kits/operator/index.html` in a browser. The strip along the
top of each is **kit chrome, not part of the design** — it switches screen, width (1440 / 834 / 390),
state, and (operator only) theme and developer drawer.

Every screen is switchable through **ready · loading · empty · error · degraded**; the viewer's
Title and Player add a **skeleton-tier** state (a title that is legitimately sparse because
`enrichment_state` is `skeleton`, which is the majority of 1.27M titles — it must not look broken).

### Shared chrome

| | Viewer | Operator |
|---|---|---|
| Header | Sticky, 56 px (`--height-header`), `--glass-header` + 12 px blur over a scrolled backdrop, 1 px `--border-subtle` bottom. Wordmark, three nav links (Home / Browse / Search) as 32 px pill buttons, `LiveIndicator`, three icon buttons right. | Sticky page header: `--space-4` `--gutter-page` padding, `--text-title` h1, `--text-body-sm` `--text-muted` subtitle capped at 76ch, actions right. |
| Nav | Phone: sticky bottom tab bar, 52 px (`--height-tabbar`) + `env(safe-area-inset-bottom)`, 4 tabs, 20 px icon + 11 px label. | 240 px (`--width-sidebar`) sticky sidebar on `--bg-surface`, 1 px `--border-default` right. 8 items, 16 px icon + `--text-label`, 30 px min height, selected = `--bg-selected`. Counts in `--text-mono-xs`, alarms as a `bad` Badge. Footer: `LiveIndicator` + `v0.9.4 · unauthenticated LAN` in mono. Tablet: 56 px icon rail. Phone: menu button → sheet. |
| Wordmark | `usher` in Instrument Sans 600, 20 px, `-0.03em` tracking, `--text-primary`, with the terminal `.` in `--teal-400` (dark) / `--teal-600` (light). 18 px on phone, 17 px in the sidebar. This wordmark **is** the brand mark; there is no logo. |
| Content | Rails bleed past the page gutter so the next card peeks. | Content column caps at 1216 px (`--width-content`); prose at 640 px (`--width-prose`). |

`BackendWork` is a first-class on-screen label, not a spec comment: dashed `--warn-border`,
`--warn-quiet` fill, `hammer` icon, uppercase eyebrow, and the missing routes printed in mono.

### Viewer (`ui_kits/viewer/`, dark, comfortable)

| Screen | File | Purpose and what must survive porting |
|---|---|---|
| Home | `Home.jsx` | Server-composed rails. Each row prints its own `reason` sentence ("Because you watched Stalker."). Mixed aspect driven by `display_hint`. Continue-watching progress as a 3 px bar on the artwork's bottom edge. `←`/`→` moves focus between cards. Degraded state = three rows dropped, and it says so. |
| Browse | `Browse.jsx` | Keyset paging, no totals, no page numbers, no result count. Tri-state `owned` filter. List and grid densities. Facet panel explains why it is unavailable when no filter is set. 236 px facets at 1440, 200 px at 834, sheet at 390. |
| Search | `Search.jsx` | Two suggest tiers under separate group headers (different queries, not a fallback chain). Mode-narrowing notice. `semantic_coverage` printed against its real denominator. Skeleton results are first-class, not a spinner. |
| Title | `TitleDetail.jsx` | 420 px hero backdrop with poster beside (330 px at 834; at 390 no poster, title over backdrop). Availability per copy. `TargetPicker` after a real pending state. Cast/crew with no headshots. Stale similar row. Images section. |
| Season / episode | `Series.jsx` | Season switcher including Specials (season 0). Per-episode progress. Where `episode_count` disagrees with the returned list, it says so plainly. |
| Person | `Person.jsx` | Raw group labels as the API supplies them, no photo, and the silent-truncation-at-50 warning stated. |
| Collection | `Collection.jsx` | The one screen where a percentage is honest, because `owned_count` and `total_count` are both given. Unowned members at `--unowned-opacity` (0.55). |
| Player / hand-off | `Player.jsx` | Inline playback and external hand-off. Ticket expiry recovers in one tap. Decode failure is told apart from playback failure. **A ticket URL is never rendered.** |
| About | `About.jsx` | The four `/meta/attribution` strings verbatim, the TMDb logo slot, server version, readiness. |

### Operator (`ui_kits/operator/`, light + compact)

| Screen | File | Purpose and what must survive porting |
|---|---|---|
| Overview | `Overview.jsx` | Readiness **with a cause** — `degraded` names the migration gap. Lanes are reported, not gated. Live cursor runs. A "needs a person" list. |
| Sources | `Sources.jsx` | Tri-state probe results (`Tri`: yes / no / unknown, never a bare boolean). `is_administrator: true` rendered as a warn-tone risk surface. `device_id` visible with the reason attached. Three-step connection wizard with a test step. Sync triggers framed by consequence. |
| Bootstrap | `Bootstrap.jsx` | Six phases in mandatory order. Cursor-and-throughput progress, never a percentage. Stall detection at 120 s (`stalled?` — with the question mark; it is an inference). A failed run designed as a normal state. Genome coverage as counts. |
| Review queue | `Review.jsx` | Two-panel matcher. `external_id` explained as the handle it is. Live candidate search. `j` / `k` / `Enter` / `s` triage. An explicit on-screen list of the fields the left panel still needs. |
| Recommendations | `Rows.jsx` | Ten providers in plain language, prefix slugs explained, never-built shown as inactive, deployment-wide cache warning. |
| Pipeline | `Pipeline.jsx` | Queue depth by kind, parked jobs with last error and release. **Entirely REQUIRES BACKEND WORK**, labelled at the top with the four routes needed. |
| Insights | `Insights.jsx` | Native panels for the six daily numbers, the five unbuilt dashboards, the seven unarmed alerts. Every panel prints its metric name so the series is findable in Grafana. "Open in Grafana" is a marked escape hatch — **no iframes**. |
| Configuration | `Config.jsx` | 69 read-only env vars, searchable, grouped, measured defaults explained, secrets as `•••• set` / not set. |
| Developer drawer | `DevDrawer.jsx` | 544 px docked right (`--width-drawer`), full-screen sheet at 390. Request journal with redacted bodies and a trace link; coverage ledger that greens only what the session exercised. `aria-hidden` **and** `inert` when closed. Sits at `--z-devdrawer` (700), **above** modals (410) on purpose. |

## Components

28 components in 10 groups. Each has a `.d.ts` props contract (**the API to implement against**), a
`.prompt.md` with usage rules and anti-patterns, a reference `.jsx`, and a `<group>.card.html`
specimen showing every state. Styling is one CSS file per group, all reached from `styles.css`.

| Group | Components |
|---|---|
| `components/actions/` | **Button**, **IconButton**, **TextLink** |
| `components/forms/` | **Input**, **Select**, **Checkbox**, **Switch**, **FilterChip**, **SearchCombobox** |
| `components/navigation/` | **Tabs** |
| `components/icon/` | **Icon** |
| `components/media/` | **Artwork**, **ProgressBar**, **PosterCard**, **LandscapeCard**, **TitleRow** |
| `components/data/` | **DataTable**, **LoadMore** |
| `components/status/` | **Badge**, **StateBlock**, **LiveIndicator**, **CursorProgress** |
| `components/feedback/` | **Problem**, **Toast** (+ **ToastStack**), **ConfirmDialog**, **Skeleton** |
| `components/playback/` | **TargetPicker** |
| `components/charts/` | **ChartPanel** |

Non-obvious contracts worth reading before you start: `StateBlock` (the four-way absent state),
`CursorProgress` (progress with no denominator), `Problem` (one error component at four scales, keyed
to a closed seven-code taxonomy), `LoadMore` (keyset only), `TargetPicker` (never exposes a ticket
URL), `ChartPanel` (tells "never fired" apart from "measured zero"), `DataTable` (`asCards` phone
fallback).

## Interactions and behaviour

`guidelines/patterns.md` is the authority — 15 numbered sections with redlines. Summary:

**Feel.** Cards lift `translateY(-2px)` + `scale(1.015)` with the shadow stepping 1→2. Controls
**do not move at all** on hover — they take `--hover-overlay` (5.5–6% white; 5% ink in light) and
step their border to `--border-control`. Press is `scale(0.985)` + `--press-overlay`, 80 ms.
Nothing changes hue on hover.

**Focus.** 2 px `--border-focus` ring at 2 px offset, `:focus-visible` only, on **everything** —
cards, rail items, table rows, icon-only buttons. Inside a scroll container use the inset variant so
the ring is never clipped. Over artwork, controls also carry `--border-control`.

**Loading.** No route-level spinners, ever. Skeletons are shaped like the thing that is coming, on
one shared 1400 ms sweep so every surface loads at the same tempo.

**Motion budget.** Nothing that reports state animates longer than 180 ms. Live data arrives as a
**highlight fade, not a movement**: an SSE `title.updated` frame lights the row with
`--live-patch-flash` for 1000 ms (opacity/colour only) and settles. Rows never slide. No springs, no
bounce, no overshoot anywhere.

**Pagination** is keyset. No page numbers, no totals, no "jump to page N", no result counts — the API
does not supply them.

**Destructive / expensive actions** name the consequence, the cost, the duration and the resumability
before the click: "Downloads ~224 MB from IMDb and rewrites the title skeleton. Measured 2 h 40 m on
a cold run. Resumable." Never "Are you sure?".

**202-shaped actions.** Every mutating admin action returns `202 {kind, key}` and there is no route to
look that key up. The toast always names what was queued, that it may have coalesced, and where to
watch it: "Queued a full sync of Living Room. It coalesced with a sync already running. Watch it on
Pipeline."

**Errors** show the code and the server's `detail` verbatim. `detail` is never parsed (the server may
reword it at any release) but it is always shown. Recovery is per-code across seven codes.

**Keyboard** (full table in `patterns.md` §9): `/` or `⌘K` focus search · `↓↑` combobox ·
`←→` rail · `←→ Home End` tablist · `↑↓` table rows, `Enter` opens · `j k Enter s` review triage ·
`⌘\` developer drawer · `Esc` closes exactly one layer, innermost first · `Space` play/pause.
Skip link to `#main` on every screen. On route change, focus moves to the new `<main>`'s heading —
never left on the clicked link.

**Responsive** (full table in `patterns.md` §11): three designed widths, 1440 / 834 / 390. A table
**must not** shrink its font to fit — it becomes cards. Nothing is hidden at a smaller width without
an equivalent path to it.

**Accessibility** (`patterns.md` §12) is a contract, not a checklist: 103 measured contrast pairs and
0 failures (`guidelines/contrast.md`); no colour-only encoding anywhere (hue + icon + word, six fixed
state glyphs); ARIA 1.2 combobox; real `tablist` / `role="switch"` / `role="progressbar"` with
`aria-valuetext` in words when there is no denominator; polite live regions only, nothing assertive,
individual SSE frames never announced; `prefers-reduced-motion` collapses all durations to 1 ms.

**Security in the design** (`patterns.md` §13): playback ticket URLs are secrets — no copy button, no
share, no logging; the dev drawer redacts them before the journal holds them. Credentials are
write-only. `is_administrator: true` is a warn-tone risk surface, not a success.

## State management

Per screen, the state the design assumes:

- **Route + focus**: current route, and a post-navigation focus target (`<main>` heading, `tabIndex={-1}`).
- **Theme + density**: `data-theme="dark|light"` and `data-density="compact"` on a container element.
  Viewer is pinned dark + comfortable; operator defaults to light + compact. No component reads these
  attributes — only `--density-*` tokens change.
- **Per-list keyset cursor**: an opaque cursor string plus `loadingMore` and `exhausted` flags. No
  page index, no total.
- **Request state per surface**: `idle | loading | ready | empty | error | degraded`, plus a distinct
  `skeleton-tier` for a title whose `enrichment_state` is `skeleton`. `empty` and `never computed` are
  **different states** and must not collapse into one branch.
- **SSE connection**: `connected | idle | reconnecting | off`, last event timestamp, and a per-row
  "recently patched" flag with a 1000 ms lifetime. The bus is in-process and lossy by design, so the
  UI treats missed frames as normal and quiet as healthy.
- **Toast queue**: 202 receipts and recoverable failures, polite live region.
- **Layer stack**: which layers are open (listbox → popover → dialog → drawer → sheet) so `Esc`
  closes exactly one.
- **Playback**: selected target, ticket expiry countdown, decode state — the ticket URL itself is held
  where the UI cannot print it.
- **Review triage**: queue position, selected candidate, keyboard cursor.

Data fetching: every screen maps to routes documented upstream in `docs/prd/07-client-api.md`
(viewer) and `docs/prd/08-operations.md` (operator); `GET /events` carries the six SSE types,
`GET /admin/bootstrap/status` the cursor progress, `/meta/attribution` the About strings, and
`/images/{id}?w=` the artwork ladder. See `upstream-source.md` for the repo/branch and screen map.

**Seven things are designed but not implementable today** (`patterns.md` §15 — the running register):
sync history / activity timeline, enable-disable a source, job/queue introspection (the whole
Pipeline screen), releasing a parked job, unmatched-queue enrichment fields, and auth (the API has no
user or auth concept — the console is unauthenticated on the LAN). All are labelled on screen with
`BackendWork`. Do not quietly build a fake around a missing route.

## Design tokens

Source of truth: `tokens/*.css` (plain CSS custom properties), `implementation/tailwind-theme.css`
(Tailwind v4 `@theme`), `guidelines/palette.json` (every token as oklch + hex, for tooling).

**Product code uses semantic aliases only** — never a `--n-*` or `--teal-*` directly. A component
built on aliases is automatically correct in both themes.

### Neutral ramp — warm charcoal, hue 60, one 17-step ramp for both themes

`--n-1000` `#020101` · `--n-950` `#070604` · `--n-900` `#0e0c0a` · `--n-850` `#181513` ·
`--n-800` `#221f1c` · `--n-750` `#2d2926` · `--n-700` `#3d3835` · `--n-600` `#5c5753` ·
`--n-500` `#716a65` · `--n-400` `#968d87` · `--n-350` `#b1a9a3` · `--n-300` `#c3bcb7` ·
`--n-200` `#d7d1cd` · `--n-150` `#e6e2df` · `--n-100` `#f3efed` · `--n-50` `#fbf8f6` ·
`--n-0` `#ffffff`

Warm because the viewer half lives behind film artwork, which is overwhelmingly warm; a cool grey
makes every poster look sick. Chroma 0.006–0.014, peaking mid-ramp.

### Hued ramps (steps 50 / 100 / 200 / 300 / 400 / 500 / 600 / 700 / 800 / 900)

| Ramp | Hue | Role | 400 | 500 | 600 |
|---|---|---|---|---|---|
| teal | 195 | accent, links, focus, selection, brand, `info` | `#1ad1d1` | `#00b0b1` | `#007678` |
| green | 150 | `good` / healthy / owned | `#76cf8a` | `#56ae6c` | `#197539` |
| amber | 80 | `warn` / stalled / degraded | `#e3ad4b` | `#c28e24` | `#865700` |
| red | 25 | `bad` / failed / parked | `#ff958d` | `#dd766f` | `#9c403c` |

**The accent is never a call to action.** Primary buttons are monochrome
(`--action-primary-bg`: `--n-50` on dark, `--n-800` on light). An operator console shows good/warn/bad
constantly; if teal also meant "do this", every screen would carry two competing colour languages.

### Semantic aliases (dark → light)

Surfaces: `--bg-canvas` `n-900`→`n-50` · `--bg-sunken` `n-950`→`n-100` · `--bg-surface` `n-850`→`n-0` ·
`--bg-raised` `n-800`→`n-0` · `--bg-inset` `n-950`→`n-100` · `--bg-letterbox` `n-1000` both ·
`--bg-selected` `teal-900`→`teal-50`.
Text: `--text-primary` `n-50`→`n-800` · `--text-secondary` `n-300`→`n-600` · `--text-muted`
`n-400`→`n-500` (**lowest tone permitted for real content**, ≥5.2:1 everywhere) · `--text-disabled`
`n-600`→`n-350` (only on a control that is also `aria-disabled`) · `--text-link` `teal-400`→`teal-600`.
Borders: `--border-subtle` (dividers) `n-750`→`n-150` · `--border-default` (card edges) `n-700`→`n-200`
· `--border-strong` (table header rule) `n-600`→`n-350` · `--border-control` **the only border allowed
to identify a control, clears 3:1** `n-500`→`n-400` · `--border-focus` `teal-400`→`teal-600`.
Semantics: `--{good,warn,bad,info}-{text,solid,quiet,border}`. `info` is deliberately the same hue as
the accent.
Epistemic states: `--state-never-*` (dashed hairline + muted italic sentence), `--state-empty-*`
(solid hairline + sentence), `--state-na-text` (em dash), `--state-stale-*` (amber hairline).
Enrichment tiers: `--tier-{skeleton,stub,enriched,failed}-{text,bg}` — distinguishable in greyscale by
background step alone.
Full list, both themes, in `tokens/semantic.css`.

### Type

**Instrument Sans** (400–700 variable) for everything; **JetBrains Mono** for identifiers and
measurements only — UUIDv7 ids, `emby:4412` external ids, cursors, codec/container strings,
latencies, counts, timestamps, config keys. Never prose, never a UI label. Both SIL OFL and
self-hostable (`tokens/fonts.css`). All numbers that change in place are `tabular-nums`.

14 steps, each a complete `font` shorthand with line height baked in:

| Token | px / line | Use |
|---|---|---|
| `--text-display-lg` | 56 / 57, 600 | title heroes |
| `--text-display` | 44 / 46, 600 | |
| `--text-display-sm` | 34 / 37, 600 | empty-state headlines |
| `--text-title-lg` | 26 / 31, 600 | |
| `--text-title` | 22 / 28, 600 | screen heading |
| `--text-title-sm` | 18 / 24, 600 | section heading |
| `--text-heading` | 16 / 22, 600 | card / panel header |
| `--text-heading-sm` | 14 / 20, 600 | |
| `--text-body-lg` | 17 / 26, 400 | overview prose |
| `--text-body` | 15 / 22, 400 | default |
| `--text-body-sm` | 14 / 21, 400 | captions |
| `--text-body-xs` | 13 / 19, 400 | compact density floor |
| `--text-label` | 13 / 18, 500 | form labels, badges |
| `--text-label-sm` | 12 / 16, 500 | absolute sans floor |
| `--text-eyebrow` | 11 / 14, 600 | **the only uppercase in the system**, +0.075em |
| `--text-mono` / `-sm` / `-xs` | 14 / 13 / 12 | identifiers, measurements |
| `--text-metric` / `-sm` | 28 / 20, 500 mono | the big number on an Insights panel |

Tracking: display `-0.022em`, title `-0.014em`, body `0`, label `0.005em`, eyebrow `0.075em`, mono
`-0.01em`. **Sentence case everywhere** — headings, buttons, table headers, dialog titles. Title Case
is never used.

### Spacing, radii, hit targets

4 px grid: 1px hairline · 2px · 4 · 8 · 12 · 16 · 20 · 24 · 32 · 40 · 48 · 64 · 80 · 96
(`--space-hair`, `--space-2`, `--space-1` … `--space-24`).

Radii: `--radius-control` 6 · `--radius-card` 10 · `--radius-sheet` 14 · **`--radius-artwork` 4**
(posters stay nearly square so they read as posters, not UI cards) · `--radius-pill` 999 for badges
and filter chips. Touch targets never below 44 px (`--target-touch`).

**Density is a token, not a second design system.** `[data-density="compact"]` changes exactly eight
`--density-*` values and nothing else may branch on it: row height 44→32, control height 36→28,
small control 28→24, pad-x 12→8, pad-y 8→4, cell pad 12/10→8/4, gap 12→8, body step 15→13 px.
Touch overrides density — hit targets stay ≥44 px on touch regardless.

### Layout

Breakpoints 390 / 834 / 1440. `--width-content` 1216 · `--width-prose` 640 · `--width-sidebar` 240
(collapsed 56) · `--width-drawer` 544 · `--height-header` 56 · `--height-tabbar` 52 ·
`--gutter-page` 24 (16 on phone) · `--gutter-rail` 16 · `--card-poster-w` 168 ·
`--card-landscape-w` 280 · `--grid-poster-min` 144.
Aspect ratios come from `kind`: poster 2:3, backdrop/still 16:9, profile 2:3, logo free — the API
carries no width or height. Image proxy widths are exactly 154 / 342 / 780 / 1280; anything else
snaps up.

### Elevation

**Dark lifts by lightness plus a hairline** (a black shadow on near-black is invisible);
**light lifts by shadow**. Both always keep the hairline — shadow is never the only cue that
something is a distinct surface.

`--shadow-1` `0 1px 2px oklch(0 0 0/.40)` · `--shadow-2` `0 2px 4px /.36, 0 6px 16px /.34` ·
`--shadow-3` `0 4px 8px /.36, 0 16px 40px /.44` · `--shadow-4` `0 8px 16px /.36, 0 32px 80px /.52` ·
`--shadow-artwork` `0 2px 6px /.50, 0 12px 28px /.38` (warmer and deeper — a poster is a physical
object). Light-theme equivalents are far softer; see `tokens/elevation.css`.

Text over artwork always sits on a protection gradient (`--gradient-protect-bottom` / `-left`),
never a capsule or blurred pill. Blur/glass appears in exactly two places: the app header over a
scrolled backdrop (12 px) and phone sheets (14 px). Never on a card, a table, or behind body text.
Scrim: `--scrim-modal`.

### Motion

Durations 80 / 120 / 180 / 240 / 320 ms (`--dur-instant` … `--dur-sheet`), plus `--dur-highlight`
1000 ms (live patch fade) and `--dur-shimmer` 1400 ms (skeleton sweep).
Easings: `--ease-standard` `cubic-bezier(.2,0,.2,1)` for almost everything · `--ease-out`
`(0,0,.2,1)` entering · `--ease-in` `(.4,0,1,1)` leaving · `--ease-emphasis` `(.2,0,0,1)` sheets.
Reduced motion collapses every duration to 1 ms, disables the sweep, and removes card lift and press
scale; the patch highlight survives as a colour change with no travel.

### Z-index

Twelve named layers; **no numeric z-index in product code**. `--z-base` 0 · `--z-artwork-overlay` 1 ·
`--z-rail-control` 20 · `--z-sticky-table-head` 60 · `--z-header` 100 · `--z-sidebar` 110 ·
`--z-dropdown` 200 · `--z-drawer-sheet` 300 · `--z-scrim` 400 · `--z-modal` 410 · `--z-popover` 500 ·
`--z-toast` 600 · `--z-devdrawer` 700 · `--z-tooltip` 800 · `--z-skip-link` 900.
The developer drawer outranking modals is deliberate: you have to be able to read the request journal
for the failed call that put the modal on screen.

### Data visualisation

Eight hues, Okabe–Ito derived, authored twice (`-dark`, `-light`) so a series keeps its order and
identity across themes while every mark clears 3:1 on its panel. Series order is fixed; a metric keeps
its colour between panels. `--viz-never-fired` and `--viz-zero` exist so a panel for one of the six
metrics that has never produced a sample cannot look like a panel measuring a real zero.
Percentile tokens: `--viz-p50` / `-p95` / `-p99` + `--viz-band` for the p50–p95 fill.

## Assets

- **Fonts**: Instrument Sans + JetBrains Mono, both SIL OFL, self-hostable. `tokens/fonts.css` has
  the loading strategy. Serve them from the app, not a CDN — this is a self-hosted product.
- **Icons**: **Lucide** (https://lucide.dev), 24 px grid, stroke-based, `currentColor`. Sizes 16
  (inline / compact rows), 20 (controls, nav), 24 (empty states, headers) — no others. Stroke 1.75 at
  16 and 20, 2 at 24; never scale a 24 px glyph down. Six state glyphs are fixed: `check-circle` good,
  `alert-triangle` warn, `x-circle` bad, `info` info, `circle-dashed` never computed, `history` stale.
  The kits load Lucide's UMD build from a CDN and hydrate `<i data-lucide>` placeholders; **in
  production, import per-icon from `lucide-react` and pass the element to `Icon`'s `svg` prop** so
  there is no third-party runtime and the bundle tree-shakes.
- **No emoji, anywhere** — not in copy, not as icons, not in empty states. The only non-alphabetic
  glyphs in text are the em dash for a not-applicable value and `·` as a metadata separator.
- **Brand mark**: the wordmark `usher.` — no logo file exists or is pending.
- **Imagery**: none ships. All artwork in the kits is a generated gradient placeholder.
- **TMDb logo**: required next to their disclaimer by their terms; must come from TMDb's brand page.
  Marked slot on the About screen.

## Files

```
README.md                         this document
design-system-rationale.md        why every decision is what it is — read before changing one
SKILL.md                          front matter for using this system as an agent skill
upstream-source.md                repo, branch, last sync, screen → repo-file map

styles.css                        global entry point (imports only — link this one file)
tokens/                           fonts · palette · semantic · typography · spacing
                                  layout · elevation · motion · layers · base
implementation/tailwind-theme.css  Tailwind v4 @theme + light:/compact: variants + utilities

components/<group>/               <Name>.d.ts.txt  props contract (implement against this)
                                  <Name>.prompt.md  usage rules and anti-patterns
                                  <Name>.jsx.txt   reference implementation
                                  <group>.css  styling for the group
                                  <group>.card.html  specimen sheet, every state

guidelines/contrast.md            the ledger: 103 measured pairs, 0 failing
guidelines/patterns.md            15 sections of cross-cutting behaviour and redlines
guidelines/palette.json           every token as oklch + hex, for tooling
guidelines/*.card.html            foundation specimens (colour, type, space, state, viz, motion)

ui_kits/viewer/index.html         9 viewer screens, dark, comfortable — open this
ui_kits/operator/index.html       9 operator surfaces, light + compact — open this
ui_kits/*/*.jsx.txt               screen sources (loaded by index.html)
ui_kits/*/README.md               per-screen notes
ui_kits/*/data.js                 mock payloads using real API field names
_ds_bundle.js                     compiled component bundle the HTML kits and cards load
```

**Note on the `.txt` suffix.** Source files ship as `Name.jsx.txt` and `Name.d.ts.txt` so the
authoring project does not compile a second copy of the library. They are plain text — drop the
`.txt` when you port them. The HTML kits reference the suffixed names and run as-is.

Suggested order of work: port `tokens/` → build `Icon`, `Button`, `Badge`, `StateBlock`, `Problem`,
`Skeleton` → the two shells → viewer Home / Browse / Title → operator Overview / Sources / Bootstrap
→ the rest. `guidelines/patterns.md` applies from the first screen, not at the end.
