# Usher Console

The web client for [Usher](../README.md), in the same repository and the same
container. Two halves for the same person: a **viewer** for browsing, searching
and playing, and an **operator** console for connecting a media server, running
imports, draining the review queue and reading metrics.

React 19 · TypeScript 6 (strict) · Vite 8 · Tailwind v4 (CSS-first) ·
TanStack Query 5 · React Router 7.

```bash
npm ci
npm run dev          # Vite on :5173, proxying the API to $USHER_ORIGIN (default :8100)
npm run verify       # typecheck · lint · format · unit tests · production build
npm run e2e          # Playwright at 1440 / 834 / 390, with an axe sweep at each
```

## Where it is served, and why not at `/`

At **`/console`**, by Usher's own FastAPI app — `src/usher/api/console.py`. Not
at `/`, because the API mounts all seventeen routers with no prefix and already
owns nineteen root path segments: a client route for a title detail page would
collide with `GET /titles/{title_id}`. Giving the API an `/api` prefix instead
would be a breaking change to a public contract for the benefit of the client
generated from it. Plex and Emby both use `/web/` for the same reason. `GET /`
redirects.

**One origin, no proxy, and that is a correctness property rather than a
convenience.** Usher mints playback ticket URLs from the incoming `Host` header
and ships no CORS middleware. The previous client ran behind its own nginx
rewriting `/api/*` to `/*`; a proxy that dropped the port from that header
produced ticket URLs pointing at a different service on this box — invisibly,
because a browser re-issues them same-origin and only an external player
following the `deep_link` ever noticed. With no proxy there is no header to get
wrong. `src/api/paths.ts` carries the shared list of API root segments, and a
backend test reads it out of this TypeScript so a new router cannot be added
without the dev proxy learning about it.

## Layout

```
src/
  design-system/   the reusable library. Knows NOTHING about Usher's API.
    tokens/*.css   ported verbatim from the handoff — do not edit values
    components/    28 components in 10 groups; <Name>.tsx · <Name>.test.tsx · <group>.css
  api/             transport, one hook per operation, generated schema, SSE, request journal
  patterns/        cross-cutting behaviour: Esc layer stack, focus, toasts, appearance
  features/        screens — viewer/ (dark, comfortable) and operator/ (light, compact)
  app/             router, shells, providers, runtime config
  kit/             the component gallery — dev and e2e only, absent from a production build
  test/            setup, render helpers, axe, MSW server + handlers + fixtures
e2e/               Playwright specs
docs/              the handoff's guideline documents, shipped with the repo
```

**The `design-system/` boundary is real.** A component there may not import from
`api/`, `features/` or `app/` — it takes data as props. That is what makes the
library reusable, keeps its tests free of MSW, and stops an API shape leaking
into a visual contract. `features/` is where an Usher DTO is mapped onto those
props.

## The rules that are correctness, not style

`docs/patterns.md` is the authority — fifteen numbered sections with redlines,
and `CONVENTIONS.md` is the contract between it and this codebase. Read both
before changing a screen. The ones most likely to be lost:

- **The UI never lies about what it knows.** "Never computed", "computed and
  empty", "stale" and "not applicable" are four different facts with four
  treatments, and a single grey dash for all four is forbidden. `StateBlock`'s
  `meta` names the field that proves the claim.
- **No number ships without its denominator.** Bootstrap progress is a cursor
  and a throughput, never a percentage — the server reports a position, not a
  fraction. Lists say "72 loaded so far", never "72 of 400". The one legitimate
  percentage in the product is collection completion, because the API gives
  both terms.
- **Keyset pagination only.** No totals, no page numbers, no result counts.
  `next_cursor === null` produces a sentence, because a silent stop is
  indistinguishable from a bug.
- **A playback ticket URL is a secret.** Never displayed, copied, shared or
  logged. `TargetPicker` enforces this in the type system: every render path
  takes a `DisplayTarget`, which is `PlayTarget` with `url` omitted, so
  printing it is a compile error rather than a review finding.
- **202 means queued.** Every mutating admin action answers `202 {kind, key}`
  and no route can look that key up, so a receipt names what was queued, prints
  the key selectably, states coalescing, and points at where evidence will
  appear. Never "Done", never a bare checkmark.
- **The UI must be fully correct if zero SSE frames arrive.** The bus is
  in-process and lossy by design. Live updates are delight, never mechanism.
- **Anything needing a route that does not exist is labelled `REQUIRES BACKEND
WORK` on screen**, with the missing routes in mono. Seven such surfaces are
  designed and unimplementable today; the register is `docs/patterns.md` §15.
  Do not quietly build a fake around a missing route.

## Testing

Four layers, each covering what the one below cannot:

| Layer      | Tool                            | What it catches                                                                  |
| ---------- | ------------------------------- | -------------------------------------------------------------------------------- |
| Component  | Vitest + Testing Library        | props → markup, keyboard models, ARIA, the anti-patterns each `.prompt.md` names |
| Screen     | + MSW over the real `client.ts` | RFC 9457 parsing, keyset paging, the five states per screen                      |
| Gallery    | Playwright + axe                | colour contrast against the real stylesheet                                      |
| End to end | Playwright at 1440 / 834 / 390  | the built bundle, `base: '/console/'`, responsive markup switches                |
| Visual     | Playwright screenshots          | a pixel baseline per component group — see below                                 |

MSW is used rather than a hand-stubbed `fetch` so tests exercise the real
transport — its `problem+json` content-type sniff, its status-0 path for a
request that never left. Contrast is disabled in the jsdom layer and only there:
jsdom computes no layout and resolves no custom properties, so it reports every
pair as failing; the browser layer is where the handoff's 103-pair ledger
(`docs/contrast.md`) is actually measured against.

The e2e suite runs a `--mode fixtures` build, which starts MSW in the browser.
The production build contains no mock server and CI asserts that rather than
assuming tree-shaking handled it.

**The 120 screenshot comparisons are tagged `@visual` and run as their own CI
job**, `console-visual`, in the same pinned `mcr.microsoft.com/playwright`
image `console-e2e` uses.

⚠️ **They caught a real defect and were disbelieved for it — read this before
distrusting the suite again.** All 120 failed with one signature: identical
width, different height (919→886, 823→801, 653→633), reproducing byte-for-byte
on every retry and on three separate machines. That was diagnosed as "a pixel
baseline is only comparable within one rendering environment", and the job was
moved into a pinned container, then out of CI entirely, then onto a self-hosted
runner. Three changes to the harness, none to the code.

The screenshots were right. `--font-sans` named `Instrument Sans`, but
`@fontsource-variable` registers the family as **`Instrument Sans Variable`**,
so no `@font-face` ever matched: the woff2 shipped in the bundle, was
fingerprinted and served, and never rendered a glyph. Every environment fell
through to its own system stack — the same page measured 18,666 px tall on a
bare host and 18,540 in the container, from byte-identical CSS. With the family
name corrected both give 18,568, and baselines generated in the container pass
on a bare host unchanged.

Two things generalise. **A test failing identically everywhere is evidence
about the code, not about the runners** — the non-portability theory predicted
flakiness and then explained away perfect reproducibility, which should have
killed it. And **the pixels were the only layer that could see this**: jsdom has
no font stack, axe reads contrast rather than metrics, and the computed
`font-family` string reads back the same whether the family resolved or fell
through. `e2e/stylesheet.spec.ts` now asserts a `FontFace` actually reaches
`loaded`, so the cheap layer catches it next time.

The threshold is deliberately not a lever — 1% is what let a 33 px shift
register as a failure rather than as noise.

```bash
npm run e2e                        # axe sweeps, tokens, fonts, behaviour
npm run e2e:visual                 # the 120 screenshots, serialised
npm run e2e:visual:docker          # the same, in the pinned image CI uses
npm run e2e:visual:docker:update   # regenerate after an intentional change
```

`--workers=1` on the visual scripts is not caution: at three workers the
`feedback` group flaked and then passed in isolation twice in a row. The
explanation recorded at the time — browsers racing the preview server for
webfonts — cannot have been right, because no webfont was loading at all then;
it is left unexplained rather than re-rationalised. `freeze()` now awaits
`document.fonts.ready` before every screenshot, which closes the race that
genuinely does exist now that `font-display: swap` has a real face to swap in.
A deterministic 4.2 min still beats a flaky 38 s for a suite whose whole job is
to notice a changed pixel.

## Regenerating the API types

```bash
USHER_ORIGIN=http://localhost:8100 npm run gen:types
```

`src/api/schema.d.ts` is generated from the live `/openapi.json`. The script
runs `openapi-typescript` in a throwaway `npx` sandbox pinned to TypeScript 5.9
because that package peers on TS ^5 and this repository is on TS 6 — neither
lands in `node_modules`, and the workaround exists only in that one line.
