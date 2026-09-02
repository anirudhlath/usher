---
paths:
  - "web/**"
  - ".github/workflows/**"
---

# The Usher Console (`web/`)

React 19 + Vite + Tailwind v4, built into the image and served by Usher's own
container at `/console`. **`web/CONVENTIONS.md` is the contract and is
authoritative for mechanism — read it before writing any file here.** This file
carries only what a session arriving from the Python side gets wrong, and does
not restate it.

## The gate is not the Python gate

`web/` is outside both Python gate tools — `[tool.ruff] extend-exclude` names
it, and `[tool.mypy] files = ["src", "tests"]` never does — so the five commands
in `CLAUDE.md`'s gate say nothing at all about this directory. A console change
that passes every one of them can still fail CI three ways. The console's own
gate, run from `web/`:

```bash
npm run verify   # typecheck && lint && format:check && test && build — CI's `console` job
npm run e2e      # Playwright, functional + a11y — CI's `console-e2e` job
npm run e2e:visual   # the 120 screenshot comparisons — CI's `console-visual` job
```

`npm run verify` does **not** include either Playwright suite; the two `e2e`
lines are separate CI jobs and neither is reachable from `verify`.

`e2e:docker` and `e2e:visual:docker` reproduce those jobs inside the pinned
`mcr.microsoft.com/playwright:v1.62.1-noble` image. They are for reproducing CI
exactly and for skipping `playwright install --with-deps`, **not** because the
host is untrustworthy: the screenshot baselines are portable, and a bare host
and that image agree to the byte. If the pixels disagree, that is evidence about
the product, not about the runner — see the comment above `console-visual` in
`.github/workflows/ci.yml` for the two wrong diagnoses that cost.

## Three things nothing checks for you

- **The `design-system/` import boundary is a review obligation, not a red.**
  A component under `design-system/` may not import from `api/`, `features/` or
  `app/`. The 12 `import-linter` contracts are Python-only and cannot see this,
  so unlike every layering rule on the backend, breaking it fails no gate step.
- **`src/api/schema.d.ts` is generated**, by `npm run gen:types` against a
  running server's `/openapi.json`. A hand-edit survives until the next run.
- **The image is built from `web/dist`**, in the Dockerfile's `node:26-alpine`
  stage. CI's `image` job asserts `/app/web/dist/index.html` and
  `/app/web/dist/assets` exist inside the built image, so a console change that
  builds locally but not in that stage fails there and nowhere earlier.

## The honesty rules are correctness bugs, not style

`CONVENTIONS.md`'s "Honesty rules" section is the part an agent optimising for a
tidy screen breaks first: four absent states with four treatments, no fabricated
denominators, keyset only (no totals, no page numbers), a playback ticket URL is
a secret that is never rendered or logged, errors print `code`/`status`/`detail`
verbatim and never parse `detail`, `202` means queued, and the UI must be fully
correct if zero SSE frames ever arrive. Read them there before changing a screen.
