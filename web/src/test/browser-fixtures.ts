import { CONSOLE_BASE } from '@/api/paths'

/**
 * Starts MSW in the browser so the end-to-end suite is deterministic.
 *
 * **Only reachable in the `fixtures` build mode.** `main.tsx` guards the call
 * with `import.meta.env.MODE === 'fixtures'`, which Vite replaces with a
 * literal at build time — so in a production build the condition is `false`,
 * the dynamic `import()` below is unreachable, and rollup drops MSW and every
 * fixture from the graph entirely. Verified by the bundle-size assertion in
 * `e2e/bundle.spec.ts` rather than assumed: a mock server shipped to a
 * self-hosted product would be a genuinely bad outcome, and "tree-shaking
 * probably handled it" is not evidence.
 *
 * Why mock at all, when there is a real backend on :8100 — the design is
 * specified in five states per screen (ready, loading, empty, error, degraded)
 * plus a skeleton tier, and four of those cannot be produced on demand against
 * a real 1.27M-title catalog. A visual-regression baseline also has to be
 * byte-stable, and a live catalog is not.
 */
export async function startBrowserFixtures(): Promise<void> {
  const [{ setupWorker }, { handlers }] = await Promise.all([import('msw/browser'), import('./handlers')])
  const worker = setupWorker(...handlers)
  await worker.start({
    serviceWorker: { url: `${CONSOLE_BASE}/mockServiceWorker.js` },
    // The console's own `config.json` and its hashed assets are real requests
    // to the preview server; only the API is mocked.
    onUnhandledRequest: 'bypass',
    quiet: true,
  })
}
