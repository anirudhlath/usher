import AxeBuilder from '@axe-core/playwright'
import { expect, type Page } from '@playwright/test'

export const CONSOLE = '/console'

/**
 * Navigate and wait for the app to be *settled*, not merely loaded.
 *
 * `networkidle` is not usable here: the console holds an open SSE connection to
 * `GET /events` for as long as it is mounted, so the network is never idle and
 * every wait would time out. What settles instead is the skeleton — patterns.md
 * §1 requires the region owning one to carry `aria-busy="true"`, so its absence
 * is a real signal that data has arrived rather than a sleep.
 */
export async function visit(page: Page, path: string): Promise<void> {
  await page.goto(`${CONSOLE}${path}`)
  await expect(page.locator('main')).toBeVisible()
  await expect(page.locator('[aria-busy="true"]')).toHaveCount(0, { timeout: 10_000 })
}

/**
 * The accessibility sweep, run against a real browser with the real stylesheet.
 *
 * This is where **colour-contrast** is actually checked. The component tests
 * disable that rule because jsdom computes no layout and resolves no custom
 * properties, so it reports every pair as failing; here the tokens are resolved
 * and the 103-pair ledger in `docs/contrast.md` has something to be measured
 * against.
 */
export async function sweep(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze()

  const readable = results.violations.map((violation) => ({
    rule: violation.id,
    impact: violation.impact,
    help: violation.help,
    nodes: violation.nodes.map((node) => node.html),
  }))
  expect(readable, JSON.stringify(readable, null, 2)).toEqual([])
}

/**
 * Freeze everything that would make a screenshot differ between two identical
 * runs. Without this a visual baseline fails on the skeleton sweep's phase, on
 * a caret blink, and on any elapsed-time label the operator surfaces render.
 */
export async function freeze(page: Page): Promise<void> {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.addStyleTag({
    content: `*, *::before, *::after {
      animation: none !important;
      transition: none !important;
      caret-color: transparent !important;
    }`,
  })
  // The webfonts ship `font-display: swap`, so a screenshot taken before they
  // arrive is a picture of the fallback stack — same viewport width, different
  // wrapping, a page tens of pixels taller or shorter. That is a race the
  // baselines cannot absorb, and it is the likelier explanation for the
  // `feedback` group flaking at three workers than webfont *bandwidth* was.
  await page.evaluate(() => document.fonts.ready)
}
