# Usher Console — design system

The visual and interaction system for **Usher Console**, the web client for Usher: a self-hosted
media catalog server that sits in front of a media server (Emby today) and adds a canonical catalog
of ~1.27M titles, hybrid search, precomputed similarity, and a server-composed home screen.

One app, two hats:

- **The viewer** browses, searches, opens a title, presses Play, tracks progress. This has to feel
  like a premium streaming app.
- **The operator** — the same person — connects a media server, runs dataset imports, drains a
  review queue, toggles row providers, and reads metrics. This has to feel like a control room.

The system's governing idea is inherited from the reference client it replaces: **it never lies
about what it knows.** "We have never computed this" is drawn differently from "we computed this and
it is empty". Every number is labelled with what it was measured against. Nothing is rounded up into
a percentage the server refused to supply. The job here was to keep that honesty and make it
beautiful — polished is not allowed to mean vague.

---

## Sources

| Source | What was read | Notes |
|---|---|---|
| **https://github.com/anirudhlath/usher** | `docs/prd/00-overview.md`, `docs/specs/2026-07-28-usher-v1-design.md`, full file tree (714 files) | The backend. Python/FastAPI + PostgreSQL + pgvector. Explore `docs/prd/07-client-api.md` (the client API), `docs/prd/08-operations.md` (operator surfaces) and `docs/prd/10-telemetry-and-dashboards.md` (the 35 metrics and five specified dashboards) before designing anything new against this system — they are the authoritative field-level contract. |
| Written product brief | API shapes, error taxonomy, measured latencies, provider inventory, metric list, screen list | Supplied with this project. |

**There is no frontend in the repository.** No `.tsx`, `.jsx`, `.css`, `.html`, no logo, no typeface,
no colour values — the repo is 714 files of Python, Markdown, SQL and config. Usher was built as a
backend with "not a UI" written into its non-goals, and no client has ever existed.

So this system is **new work grounded in the API contract**, and its visual decisions are its own
rather than inherited from anything. Concretely:

1. **The brand mark is the word `usher.`** set in Instrument Sans 600, lowercase, with a teal
   terminal dot (see the Brand card). No mark was drawn or reconstructed from memory, and none is
   pending — the wordmark is the identity.
2. **Instrument Sans and JetBrains Mono are this brand's typefaces**, picked for the job rather than
   substituted for something that exists. Both are SIL OFL and self-hostable, which matches an
   MIT-licensed self-hosted product.
3. **Lucide is this brand's icon set**, for the same reason.

`github.md` records the source association for one-click upstream sync. The one asset this system
cannot originate is **TMDb's logo**, which their terms require next to their disclaimer and which must
come from TMDb's own brand page — the About screen carries a marked slot for it.

---

## Phase 1 — Foundations

Delivered as (a) this document, (b) the token layer under `tokens/`, and (c) the real implementation
target: a Tailwind v4 CSS-first `@theme` block at `implementation/tailwind-theme.css`.

### Confirmed decisions

| Decision | Answer |
|---|---|
| Accent | Teal `#00b0b1` — kept. |
| Primary buttons | Monochrome. The accent is never a call to action. |
| Type | Instrument Sans + JetBrains Mono. Both SIL OFL, self-hostable, and this brand's own choice — there was no prior typeface. |
| Observability | Native Insights surface + a marked "Open in Grafana" escape hatch. No iframes. |
| Operator density | `compact` by default. Viewer stays comfortable. |
| **Light theme scope** | **Operator surfaces only.** The viewer half (Home, Browse, Search, Title, Person, Collection, Player) is dark-only — a media browser in daylight is not a real use case, and dropping it removes a whole class of artwork-over-white problems. Every token still carries a light value, so the two halves share one system and the constraint is a product rule, not a technical one. |

### Colour — rationale

**Warm charcoal neutrals, hue 60.** The viewer half of this product spends its life behind film
artwork, which is overwhelmingly warm. A cool-blue-shifted grey (the default reflex, and what most
dashboards ship) makes every poster look slightly sick and makes the app read as "admin tool with
posters in it". A warm ramp at very low chroma (0.006–0.014) sits under artwork without tinting it
and still reads as neutral in a table of numbers. Chroma peaks in the middle of the ramp, where the
eye can see it, and falls off at both ends so the darkest surface is not brown and the lightest text
is not cream.

**One ramp, two themes.** `--n-1000 … --n-0` is a single 17-step ramp. Dark reads up from `n-950`;
light reads down from `n-0`. There is no second palette to keep in sync, and a component that uses
semantic aliases is automatically correct in both.

**Teal accent (hue 195), and it is deliberately not a status.** The accent carries links, focus,
selection, the brand, and the `info` semantic — nothing else. It is *not* used for primary actions:
a Play button, a Connect button and a Resolve button are all high-contrast **monochrome**
(`--action-primary-bg`). Two reasons. First, an operator console shows good/warn/bad constantly; if
the accent also means "do this", every screen has two competing colour languages. Second, monochrome
primaries are what make Linear and Vercel read as serious, and the register here is "instrument", not
"consumer app with a brand colour".

Teal specifically because it is far from all four semantic hues, reads as instrumentation rather than
entertainment, and is not the signature colour of any adjacent product (Plex amber, Jellyfin violet,
Emby green) — this product must not look like a clone of the thing it sits in front of.

**Semantics: four hues, and `info` is the accent.** `good` = green 150, `warn` = amber 80,
`bad` = red 25, `info` = teal 195. An informational note and a link are the same register of voice,
so they get the same hue rather than a fifth one. Colour is never the only carrier: every state
badge is hue **+ icon + word**, and the three tiers of `enrichment_state` are distinguishable in
greyscale by their background step alone.

**Contrast is published, not claimed.** `guidelines/contrast.md` measures **103 pairs** — every pair
the system sanctions — and **0 fail**. The reference client's palette measurably failed (its
most-used text tone at 4.34:1 on the page and 3.98:1 on cards, a secondary tone used 96 times at
2.83:1, one content tone at 1.75:1). The lowest tone sanctioned for real content here is
`--text-muted`, at **≥5.2:1 on every surface in both themes**. `--text-disabled` is the only tone
below AA and it may only appear on a control that is also `aria-disabled`.

Borders are split on purpose: `--border-subtle/default/strong` are decorative dividers and card
edges and are explicitly *not* sanctioned contrast pairs; `--border-control` is the only border
allowed to be the thing that identifies a control, and it clears 3:1 on every surface.

**Data visualisation** is a separate eight-hue set, Okabe–Ito derived, authored twice (`-dark`,
`-light`) so a chart keeps its series order across themes while every mark clears 3:1 against the
panel it sits on. Series order is fixed; a metric keeps its colour between panels. `--viz-never-fired`
and `--viz-zero` exist so a panel for one of the six metrics that has never produced a sample cannot
look identical to a panel measuring a real zero.

### Type

**Instrument Sans** for everything, **JetBrains Mono** for identifiers and measurements. A single
sans keeps a 56 px hero and a 12 px table cell in the same voice; Instrument Sans holds up at both
because of a tall x-height and low stroke contrast.

The mono rule is a hard rule inherited from the reference client and worth keeping: **monospace is
for things you compare character by character** — UUIDv7 title ids, `emby:4412` external ids,
opaque cursors, codec/container strings, latencies, counts, timestamps, config keys. Never for
prose, never for a UI label. All numbers that change in place are `tabular-nums`.

Fourteen steps, each carrying its own line height (`--text-*` are complete `font` shorthands). Floor
is 12 px for sans, 12 px for mono, and 13 px (`--text-body-xs`) for anything a human reads a sentence
in. Uppercase appears only in `--text-eyebrow` (11/600, +0.075em) and never in a sentence.

### Spacing, radii, density

4 px grid, with 1 px and 2 px escapes for hairlines and icon nudges. Radii: controls 6, cards 10,
sheets 14, **artwork 4** — posters stay nearly square so they read as posters and not as UI cards.
Touch targets never below 44 px.

**Density is a token, not a second design system.** `[data-density="compact"]` changes exactly
eight values (`--density-*`): row height 44→32, control height 36→28, cell padding, gap, and the body
step. Nothing else in the system is permitted to branch on density. The viewer runs comfortable; the
operator's tables, the review queue and the developer drawer run compact — on the same components.

### Elevation and borders

Dark theme lifts by **lightness plus a hairline**; a black shadow on a near-black canvas is
invisible, so a raised surface must also get lighter. Light theme lifts by **shadow**, since the
surface is already white. Both keep the hairline: shadow is never the only cue that something is a
distinct surface. Five steps (`--shadow-0…4`) plus `--shadow-artwork`, which is warmer and deeper
because a poster needs to read as a physical object.

Text over artwork always sits on a **protection gradient** (`--gradient-protect-bottom` /
`-left`), never in a capsule or a blurred pill — capsules over a backdrop look like a mobile OS,
gradients look like cinema. Glass/blur is used in exactly two places: the app header over a
scrolled backdrop, and sheet backgrounds on phone. Nowhere else.

### Motion

Two rules. **Nothing that reports state animates for longer than 180 ms** — an operator reading a
queue depth should never wait on a transition to learn that a number changed. And **live data
arrives as a highlight fade, not a movement**: an SSE `title.updated` frame lights the row
(`--live-patch-flash`, 1000 ms, opacity only) and settles. It never slides, because moving a row
under a pointer that is about to click it is hostile.

Durations 80/120/180/240/320 ms plus the 1000 ms highlight. Easings: `--ease-standard` for almost
everything, `--ease-out`/`--ease-in` for entering and leaving, `--ease-emphasis` for sheets. No
springs, no bounce, no overshoot anywhere — this product measures things.

`prefers-reduced-motion: reduce` collapses every duration to 1 ms, disables the skeleton sweep, and
removes card lift and press scale. The highlight fade survives as a colour change with no travel.

### Z-index

Twelve named layers, `--z-base` (0) to `--z-skip-link` (900). No numeric z-index in product code.
The **developer drawer (700) deliberately outranks modals (410)**: you have to be able to read the
request journal for the failed call that put the modal on screen. Only tooltips (800) and the skip
link (900) go higher.

---

## Content fundamentals

The voice is **measured, specific, and never breezy**. It comes from the reference client's habit of
writing absent states as sentences and labelling every number with its denominator — with the lab
notes rewritten as product copy.

**Rules**

- **Sentence case everywhere.** Headings, buttons, table headers, dialog titles. Title Case is never
  used. `ALL CAPS` appears only as an 11 px tracked eyebrow.
- **Second person for the viewer, imperative for the operator.** "Because you watched Stalker." /
  "Connect a media server." Never "we" for the product's own machinery — say what happened, not who
  did it: "Enrichment failed" not "We couldn't enrich this".
- **Absent states are sentences, not dashes.** ✅ "We have never computed similar titles for this
  one." ✅ "Computed 3 days ago. Nothing scored close enough to show." ❌ "—" ❌ "No data".
- **Every number carries its denominator, or it does not ship.** ✅ "4,120 rows seen · 3,988 written ·
  1,240/s · last heartbeat 4 s ago" ❌ "63% complete" (the server refuses to supply a denominator for
  bootstrap progress). ✅ "Semantic coverage 0.98 — of the 128,400 enriched titles, not of the
  catalog." ❌ "98% of your library is searchable."
- **Name the consequence before the click.** "Downloads ~224 MB from IMDb and rewrites the title
  skeleton. Measured 2 h 40 m on a cold run. Resumable." Not "Are you sure?"
- **Queued is not done.** "Queued a full sync of Living Room. It coalesced with a sync already
  running. Watch it on Pipeline." — the 202 idiom, always naming what was queued and where to watch.
- **Errors show the code and the detail verbatim.** `detail` is prose the server may reword at any
  release, so it is never parsed — but it is always shown, because it is often the only thing that
  tells an operator what happened.
- **No exclamation marks. No emoji, ever.** Not in copy, not as icons, not in empty states. The one
  non-alphabetic glyph the system uses in text is the em dash for a not-applicable value.
- **Identifiers are never explained away.** An `external_id` of `emby:4412` is shown as
  `emby:4412`, in mono, copyable, described for what it is: the handle you use to find the file on
  your own server.

**Word choices**: *title* (not "movie" when it may be a series), *source* (not "server" — Usher is
also a server), *copy* (one `MediaItem` — "this title has three copies"), *skeleton / stub /
enriched* (verbatim from `enrichment_state`), *owned* (a copy exists on a source), *parked* (a job
that failed five times), *stalled?* — with the question mark, because a heartbeat older than 120 s is
an inference the design makes, not a fact the API states.

---

## Visual foundations

**Overall register:** a dark room with instruments in it. Warm near-black canvas, artwork as the only
saturated thing on a viewer screen, mono numbers, hairlines everywhere, one teal accent used
sparingly enough that it always means something.

- **Backgrounds.** Flat surfaces. No gradient page backgrounds, no mesh, no noise, no texture, no
  pattern. Gradients exist for exactly two jobs: protecting text over artwork, and fading the edge
  of a horizontal rail. Full-bleed imagery appears in exactly one place — the title-detail hero
  backdrop — and it is always cropped, never letterboxed into a card.
- **Imagery.** Warm, unfiltered, as the provider supplied it. No duotone, no grain, no colour wash,
  no brand overlay. Artwork is the product's only source of colour and the design does not compete
  with it. Aspect ratios come from `kind` (`poster` 2:3, `backdrop`/`still` 16:9, `profile` 2:3,
  `logo` free) since the API carries no width or height. Widths are exactly 154 / 342 / 780 / 1280.
- **Cards.** `--bg-surface`, 1 px `--border-default`, `--radius-card` (10), `--shadow-1` at rest.
  Artwork cards are the exception: no border, `--radius-artwork` (4), `--shadow-artwork`, and the
  image goes edge to edge.
- **Hover.** Cards lift 2 px and scale 1.5% (`--hover-card-lift`, `--hover-card-scale`) with the
  shadow stepping 1→2. Controls **do not move at all** — they take a translucent white overlay
  (`--hover-overlay`, 5.5%) and a border step to `--border-control`. Rows in a table take
  `--bg-raised`. Nothing changes hue on hover.
- **Press.** Scale 0.985 plus a dark overlay (`--press-overlay`). 80 ms. No colour change.
- **Focus.** A 2 px `--border-focus` ring at 2 px offset, on **everything**, including cards,
  carousel items, table rows and icon-only buttons. `:focus-visible` only. Inside a scroll container
  the inset variant is used so the ring is never clipped.
- **Borders.** Hairlines are the primary structural device — this system separates with a 1 px line
  far more often than with space or shadow, which is what lets the operator surfaces get dense
  without getting muddy. Dividers are `--border-subtle`; card edges `--border-default`; the rule
  under a table header `--border-strong`; anything identifying a control `--border-control`.
- **Transparency and blur.** Only: the app header over a scrolled hero (`--glass-header`, 12 px
  blur), phone sheets (14 px), the modal scrim, and hover/press overlays. Never on a card, never on
  a table, never behind body text.
- **Corner radii.** 6 controls / 10 cards / 14 sheets / 4 artwork / pill for badges and filter chips.
- **Skeletons.** Shaped like the thing that is coming, never a centred spinner — the reference
  client's route-level spinner flashed on every navigation and is the single worst thing about it.
  One shared 1400 ms sweep (`--gradient-skeleton`) so every surface loads at the same tempo.
- **Layout.** Fixed app header (56 px) and, on operator surfaces, a fixed 240 px sidebar. Phone gets
  a 52 px bottom tab bar plus safe-area padding. Content column caps at 1216 px; prose at 640 px.
  Rails bleed past the page gutter so the next card peeks.
- **Progress.** 3 px bar, `--progress-fill` monochrome over a 22% white track, sitting on the bottom
  edge of the artwork — never a ring, never a percentage label on a card.

---

## Iconography

**Lucide** (https://lucide.dev), 24 px grid, stroke-based, `currentColor`.

Chosen, not substituted — Usher had no icon set. Lucide fits because it is stroke-based (it
sits correctly next to hairline borders), it has real coverage of the nouns this product needs
(`server`, `database`, `activity`, `git-branch`, `play`, `list-video`, `scan-search`, `radio`,
`heart-pulse`, `terminal`), and it ships as individual SVGs so only what is used gets copied.

**Rules**

- Sizes: **16** inline with body text and in compact rows, **20** in controls and nav, **24** in
  empty states and headers. No other sizes.
- Stroke: `1.75` at 16 and 20, `2` at 24. Never scale a 24 px glyph down — use the smaller size.
- Colour is always `currentColor`. An icon never carries a hue its neighbouring text does not.
- Icon-only buttons **always** have an `aria-label` and a tooltip with the same words.
- **No emoji, anywhere.** No Unicode symbols as icons either, with two exceptions: the em dash `—`
  for a not-applicable value, and `·` as a metadata separator.
- State icons are fixed so colour is never the only carrier: `check-circle` good, `alert-triangle`
  warn, `x-circle` bad, `info` info, `circle-dashed` never-computed, `history` stale.
- **How it is loaded here:** the UI kits load Lucide's UMD build from a CDN and hydrate
  `<i data-lucide="…">` placeholders, so the real icon set is used rather than paths drawn from memory.
  In production, import per-icon from `lucide-react` and pass the element to `Icon`'s `svg` prop — that
  keeps the console free of a third-party runtime dependency and tree-shakes to only what is used.
  `assets/` is empty because the repository contains no imagery, logo or icon set to copy.

---


## Components

28 components in ten groups. Every one has a `.d.ts` props contract and a `.prompt.md` with usage
rules and anti-patterns; each directory has a `@dsCard` sheet showing its states. Styling lives in a
sibling CSS file per group, all reached from `styles.css`, so a consumer links one stylesheet.

| Group | Components | What it exists for |
|---|---|---|
| `components/actions/` | **Button**, **IconButton**, **TextLink** | Monochrome primary; real loading states for the 1–5 s routes; icon buttons that cannot exist without an accessible name. |
| `components/forms/` | **Input**, **Select**, **Checkbox**, **Switch**, **FilterChip**, **SearchCombobox** | 3:1 control boundaries, field errors from `errors[].msg`, the tri-state `owned` filter, and a real ARIA 1.2 combobox over the two suggest tiers. |
| `components/navigation/` | **Tabs** | Roving tabindex and real aria wiring — the season switcher, source detail, dev-drawer panes. |
| `components/icon/` | **Icon** | Lucide at 16 / 20 / 24 with the six fixed state glyphs. |
| `components/media/` | **Artwork**, **ProgressBar**, **PosterCard**, **LandscapeCard**, **TitleRow** | The image-proxy width ladder and its three failure sentences; watch progress with no invented denominator; portrait, landscape and text-forward cards off one `RowCard`. |
| `components/data/` | **DataTable**, **LoadMore** | Dense operator tables with a phone card fallback, and the only pagination idiom the API allows: keyset, no totals. |
| `components/status/` | **Badge**, **StateBlock**, **LiveIndicator**, **CursorProgress** | Tiers and ownership; the four-way absent state; "quiet is healthy" for SSE; throughput-and-position progress for work the server won't put a percentage on. |
| `components/feedback/` | **Problem**, **Toast** (+ **ToastStack**), **ConfirmDialog**, **Skeleton** | One error component at four scales with per-code recovery and a trace link; the 202-queued idiom; consequence-framed confirms; layout-shaped skeletons instead of route spinners. |
| `components/playback/` | **TargetPicker** | Copies across sources, decode state, deep-link hand-off, ticket expiry as a one-tap recovery. Never exposes a ticket URL. |
| `components/charts/` | **ChartPanel** | Native Insights panel chrome that tells "never fired" apart from "measured zero". |

### Intentional additions

- **Icon** — a wrapper the source does not define, needed because an icon set had to be chosen (Lucide) and swapping it should touch one file.
- **ToastStack** — the positioned live region for Toast; not a separate design, just the container.


## UI kits

Two products, two kits, one system. Each `index.html` is an interactive click-through with a
screen / width / state switcher along the top (that strip is kit chrome, not part of the design).

| Kit | Screens | Theme & density |
|---|---|---|
| `ui_kits/viewer/` | Home · Browse · Search · Title · Season/episode · Person · Collection · Player/hand-off · About & attribution | Dark only, comfortable |
| `ui_kits/operator/` | Overview · Sources (+ connection wizard) · Bootstrap · Review queue · Recommendations · Pipeline · Insights · Configuration · Developer drawer | Light by default (dark available), compact |

Each kit has its own README describing every screen. Every screen can be switched through **ready,
loading, empty, error and degraded**; the viewer adds a **skeleton-tier** state for Title and Player.

## Index

| Path | What it is |
|---|---|
| `styles.css` | Global entry point. Imports only — link this one file. |
| `tokens/fonts.css` | Webfont loading (Instrument Sans, JetBrains Mono) and why those two. |
| `tokens/palette.css` | Base ramps: 17-step warm neutral, four hued ramps, two data-viz sets. oklch authored, hex in comments. |
| `tokens/semantic.css` | The layer product code actually uses. Dark by default, `[data-theme="light"]` complete. Includes the epistemic-state and enrichment-tier tokens. |
| `tokens/typography.css` | 14-step scale with baked line heights, tracking, mono rules. |
| `tokens/spacing.css` | Space scale, radii, hit targets, and the `[data-density="compact"]` switch. |
| `tokens/layout.css` | Breakpoints (390 / 834 / 1440), widths, rail and card geometry, the image width ladder. |
| `tokens/elevation.css` | Shadows per theme, scrims, blur, protection gradients, skeleton sweep. |
| `tokens/motion.css` | Durations, easings, hover/press feel, reduced-motion collapse. |
| `tokens/layers.css` | Twelve named z-index layers. |
| `tokens/base.css` | Reset plus focus, link, selection and scrollbar defaults. |
| `implementation/tailwind-theme.css` | **The implementation target.** Tailwind v4 `@theme` + `@theme inline` + `light:`/`compact:` variants + utilities. |
| `guidelines/contrast.md` | The contrast ledger: 103 measured pairs, 0 failing. |
| `guidelines/patterns.md` | **Phase 4.** Patterns and redlines: loading, absent states, errors, keyset paging, confirms, 202s, live data, cursor progress, keyboard, density, responsive, accessibility, and the REQUIRES BACKEND WORK register. |
| `SKILL.md` | Agent Skill front matter, for using this system outside this project. |
| `guidelines/palette.json` | Every token as oklch + hex, for tooling. |
| `guidelines/*.card.html` | 28 specimen cards — foundations plus five pattern cards (Design System tab). |
| `ui_kits/viewer/` | Viewer UI kit — 9 screens, dark, comfortable. See its README. |
| `ui_kits/operator/` | Operator UI kit — 9 surfaces, light + compact, includes the developer drawer. See its README. |
| `components/` | 28 components in 10 groups, each with `.d.ts`, `.prompt.md` and a card. |
| `thumbnail.html` | Project tile. |
| `github.md` | Upstream source association for sync. |

### Phases

All four phases are delivered. Foundations (`tokens/`, `implementation/`, `guidelines/contrast.md`),
the component library (`components/`), the screens (`ui_kits/`), and the patterns and redlines
(`guidelines/patterns.md`).

### REQUIRES BACKEND WORK — running register

Carried forward so nothing designed on top of a non-existent endpoint gets built by accident:

1. **Sync history / activity timeline** on Sources — sync runs exist in the database and CLI only;
   no HTTP route reads them.
2. **Enable/disable a source** — no route exists.
3. **Job/queue introspection** — every mutating admin action returns `202 {kind, key}` and there is
   no route to look that key up. The whole Pipeline screen depends on this.
4. **Release a parked job** — no route.
5. **Unmatched queue enrichment** — the left panel of the matcher has only `external_id`. Filename,
   container/resolution, and candidate suggestions are all wanted, and none exist.
6. **Auth** — the API has no user or auth concept at all. The console is unauthenticated on the LAN.
   Any auth surface is out of the main flow and marked.
