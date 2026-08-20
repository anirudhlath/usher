import { defineConfig, devices } from '@playwright/test'

/**
 * The three designed widths, and nothing else.
 *
 * `guidelines/patterns.md` §11 specifies this product at **1440 / 834 / 390**,
 * so those are the three projects here — a browser matrix would multiply the
 * job for coverage the design does not claim, while a width matrix is exactly
 * the coverage it does. Chromium only for the same reason: the accessibility
 * sweep runs against axe rather than against a particular engine's behaviour.
 */
const WIDTHS = [
  { name: 'desktop-1440', viewport: { width: 1440, height: 900 } },
  { name: 'tablet-834', viewport: { width: 834, height: 1112 } },
  { name: 'phone-390', viewport: { width: 390, height: 844 } },
] as const

const PORT = 4173
const BASE_URL = `http://127.0.0.1:${PORT}`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list']],
  timeout: 30_000,
  expect: {
    // Screenshots are compared against a committed baseline. The tolerance is
    // deliberately tight: this suite exists to catch a token that stopped
    // resolving, and a generous threshold hides exactly that.
    toHaveScreenshot: { maxDiffPixelRatio: 0.01, animations: 'disabled' },
  },
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: WIDTHS.map((width) => ({
    name: width.name,
    use: { ...devices['Desktop Chrome'], viewport: width.viewport },
  })),
  /**
   * A **built** bundle, not the dev server — so this suite exercises the
   * `base: '/console/'` rewriting that only happens at build time. A
   * dev-server run passes with a broken `base` and the container then serves a
   * blank page, which is precisely the failure worth catching here.
   *
   * `--mode fixtures` starts MSW in the browser instead of reaching a backend.
   * The design specifies five states per screen — ready, loading, empty, error,
   * degraded — plus a skeleton tier, and four of those cannot be produced on
   * demand against a real 1.27M-title catalog; a visual baseline also has to be
   * byte-stable, and a live catalog is not. The production build has no MSW in
   * it at all, and `e2e/bundle.spec.ts` asserts that rather than assuming it.
   */
  webServer: {
    // `--host 127.0.0.1` is load-bearing, not tidiness. `vite preview` otherwise
    // binds `localhost`, which on a dual-stack host resolves to `::1` first —
    // so the `url` below (an IPv4 literal, because Playwright's `baseURL` has to
    // be one) never answers, and the suite dies on a 180 s webServer timeout
    // before a single test runs. Same family as the IPv6 healthcheck note the
    // previous client's nginx carried.
    command: `npm run build:fixtures && npx vite preview --outDir dist-fixtures --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `${BASE_URL}/console/`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
})
