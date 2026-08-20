# Usher Console — implementation conventions

Read this before writing any file under `web/`. It is the contract between the
design handoff and this codebase; where the two disagree, the handoff wins on
_appearance and behaviour_ and this file wins on _mechanism_.

The design handoff is unpacked at
`/var/tmp/usher-design-handoff/design_handoff_usher_console/`. Its authorities:

| File                                   | What it governs                                             |
| -------------------------------------- | ----------------------------------------------------------- |
| `guidelines/patterns.md`               | 15 numbered sections of cross-cutting behaviour, with MUSTs |
| `README.md`                            | tokens, screens, components, assets                         |
| `design-system-rationale.md`           | why each decision is what it is                             |
| `components/<group>/<Name>.d.ts.txt`   | **the props contract — implement exactly this**             |
| `components/<group>/<Name>.prompt.md`  | usage rules and anti-patterns                               |
| `components/<group>/<Name>.jsx.txt`    | reference implementation: correct ARIA, states, class names |
| `components/<group>/<group>.card.html` | specimen sheet showing every state                          |
| `ui_kits/{viewer,operator}/*.jsx.txt`  | the 18 finished screens                                     |

A copy of `patterns.md`, `contrast.md`, the rationale and the handoff README
lives in `web/docs/` so it ships with the repo.

## Layout

```
web/
  src/
    design-system/        the reusable library. Knows NOTHING about Usher's API.
      tokens/*.css        ported verbatim from the handoff — do not edit values
      styles/             the Tailwind v4 @theme mapping
      components/<group>/ <Name>.tsx · <Name>.test.tsx · <group>.css · index.ts
    api/                  transport, hooks, generated schema, SSE, request journal
    patterns/             cross-cutting behaviour from patterns.md (layers, focus, keyset, toasts)
    features/
      viewer/<Screen>/    dark, comfortable
      operator/<Screen>/  light, compact
    app/                  router, shells, providers, runtime config
    kit/                  the component gallery (also the visual-regression target)
    test/                 setup, render helpers, axe, MSW server + handlers + fixtures
    styles/app.css        the single stylesheet the app links
  e2e/                    Playwright specs
  docs/                   the handoff's guideline documents
```

**The `design-system/` boundary is real.** A component under `design-system/`
may not import from `api/`, `features/` or `app/`. It takes data as props. This
is what makes the library reusable and what makes the component tests fast and
free of MSW. `features/` maps API shapes onto those props.

## Styling

- **The handoff's CSS is ported as-is and is the styling mechanism.** Components
  render the handoff's own `u-*` class names (`u-btn u-btn--primary u-btn--sm`).
  Do not restyle a component in Tailwind utilities; if a rule is missing, add it
  to that group's `.css` file in the handoff's own style.
- **Tailwind utilities are for screen layout only** — grid, flex, gap, sizing on
  `features/` and `app/` markup. Never for colour, type or radius: those are
  tokens, and a literal is a bug.
- **No `--n-*` or `--teal-*` in product code.** Semantic aliases only
  (`--text-muted`, `--bad-border`). A component built on aliases is automatically
  correct in both themes.
- **No numeric `z-index` anywhere.** Use `var(--z-modal)` and friends.
- Theme and density are attributes on `<html>`: `data-theme="dark|light"`,
  `data-density="compact"`. **No component may read them.** Only `--density-*`
  tokens change; nothing else branches on density.

## TypeScript

- `strict` plus `exactOptionalPropertyTypes` and `noUncheckedIndexedAccess`.
  **No `any`, no non-null `!`, no `as` casts to silence the compiler.** The
  `exactOptionalPropertyTypes` setting is deliberate: the API's
  absent-vs-`null`-vs-`[]` distinction (patterns.md §2) is a correctness rule,
  and this makes collapsing it a type error.
- Props interfaces are **copied from the handoff's `.d.ts.txt` verbatim**,
  including the doc comments, then extended only where React 19 requires it
  (`ref`, `ReactNode` vs `JSX.Element` return types). If the contract says
  `variant?: 'primary' | 'secondary'`, do not add a third variant.
- Every component directory has an `index.ts` re-exporting the component and its
  props type. Import through it, never through the file.
- Prefer `type` imports (`verbatimModuleSyntax` is on).

## Components

- Function components. `ref` is a normal prop in React 19 — no `forwardRef`.
- Spread the rest of the native props (`...rest`) onto the root element for every
  component whose contract extends an HTML attributes interface. A consumer must
  be able to pass `data-testid`, `aria-describedby` and `id` without a change here.
- **Every state in the `.card.html` specimen must be reachable through props.**
  If the specimen shows a loading button, `loading` is a prop, not a screen-local
  `useState`.
- Copy is **final and used verbatim**. Rewriting a sentence usually breaks a
  correctness rule — the words carry the epistemic distinctions.

## Tests

Every component gets `<Name>.test.tsx` beside it, covering, at minimum:

1. **Its contract** — each variant/size/state prop renders the class the CSS
   expects. This is what stops a silent visual regression.
2. **Its behaviour** — interactions from the `.prompt.md`, driven with
   `userEvent`, never `fireEvent`.
3. **Its accessibility** — `await expectNoViolations(container)` from
   `@/test/axe`, plus explicit assertions for whichever §12 clause the component
   owns (roles, `aria-*`, keyboard, focus order). Query by role and accessible
   name; `getByTestId` is a last resort.
4. **Its anti-patterns** — where `.prompt.md` says "never do X", assert X cannot
   happen. `TargetPicker` must have no code path that prints `target.url`;
   `IconButton` must not be constructible without a label.

Helpers: `renderComponent` (design system) and `renderApp` (anything needing a
router or query client) from `@/test/render`. Both accept `theme` and `density`.

## Honesty rules that are correctness bugs when broken

These come from `patterns.md` and the product brief. They are not style.

- **Four absent states, four treatments** — never computed / computed-and-empty /
  stale / not applicable. A single grey dash for all four is forbidden. `StateBlock`'s
  `meta` names the field that proves the claim; do not drop it to reduce clutter.
- **No fabricated denominators.** Bootstrap gets counts and throughput, never a
  percentage. Lists get "72 loaded so far", never "72 of 400". The one legitimate
  percentage in the product is collection completion.
- **Keyset only.** No page numbers, totals, result counts, or "jump to page N".
  `next_cursor === null` must produce a sentence, because a silent stop is
  indistinguishable from a bug.
- **A playback ticket URL is a secret.** No copy button, no share, no logging, no
  rendering. The request journal redacts it before it is stored.
- **Errors show `code`, `status` and the server's `detail` verbatim, and never
  parse `detail`.** Recovery is a lookup over the closed seven-code vocabulary.
- **202 means queued.** Never "Done", never "Saved", never a bare checkmark.
- **The UI must be fully correct if zero SSE frames ever arrive.** Live updates
  are delight, never mechanism.
- **Anything needing an endpoint that does not exist is labelled
  `REQUIRES BACKEND WORK` on screen** with the missing routes printed in mono.
  Do not quietly build a fake around a missing route. The register is
  patterns.md §15.

## Commands

```bash
npm run dev          # Vite at /console/, proxying the API to $USHER_ORIGIN (default :8100)
npm test             # vitest
npm run typecheck    # tsc -b
npm run lint         # oxlint
npm run format       # prettier
npm run e2e          # playwright
npm run verify       # all of the above plus a production build
```
