import { expect, test, type Page } from '@playwright/test'
import { CONSOLE, freeze, sweep } from './helpers'

/**
 * The component gallery — one accessibility sweep and one screenshot per group,
 * in both themes and both densities.
 *
 * This is where **colour contrast** is actually verified. The component tests run
 * in jsdom, which resolves no custom properties and computes no layout, so they
 * disable that axe rule; here the tokens resolve against the real stylesheet and
 * `docs/contrast.md`'s 103-pair ledger has something to be measured against.
 *
 * Widths are not handled here. `playwright.config.ts` already runs three viewport
 * projects — 1440 / 834 / 390, the three the design specifies — and Playwright
 * appends the project name to every snapshot path, so each expectation below is
 * three baselines.
 *
 * **The baselines are not committed by this spec's author.** Run
 * `npx playwright test e2e/kit.spec.ts --update-snapshots` once, after the tree
 * is complete; a baseline taken against a half-finished tree is a baseline of the
 * wrong thing.
 */

const GROUPS = [
  'icon',
  'actions',
  'forms',
  'navigation',
  'media',
  'data',
  'status',
  'feedback',
  'playback',
  'charts',
] as const

interface Appearance {
  theme: 'dark' | 'light'
  density: 'comfortable' | 'compact'
}

const APPEARANCES: readonly Appearance[] = [
  { theme: 'dark', density: 'comfortable' },
  { theme: 'dark', density: 'compact' },
  { theme: 'light', density: 'comfortable' },
  { theme: 'light', density: 'compact' },
]

const DEFAULT_APPEARANCE: Appearance = { theme: 'dark', density: 'comfortable' }

/**
 * `visit()` from the helpers is deliberately not used: it waits for every
 * `aria-busy="true"` to disappear, and this page renders two permanently busy
 * regions on purpose — `Skeleton/region` and `ChartPanel/loading` are states the
 * gallery exists to show. What settles here instead is the last group's section,
 * because every specimen mounts in one synchronous pass.
 */
async function openGallery(page: Page, appearance: Appearance): Promise<void> {
  await page.goto(`${CONSOLE}/kit?theme=${appearance.theme}&density=${appearance.density}`)
  await expect(page.getByRole('heading', { level: 1, name: /component gallery/i })).toBeVisible()
  await expect(page.locator(`#group-${GROUPS[GROUPS.length - 1]}`)).toBeVisible()

  // The two attributes are `AppearanceProvider`'s, on `<html>`. `data-density` is
  // only ever set for compact — comfortable is what the tokens already are.
  await expect(page.locator('html')).toHaveAttribute('data-theme', appearance.theme)
  if (appearance.density === 'compact') {
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')
  } else {
    await expect(page.locator('html')).not.toHaveAttribute('data-density', 'compact')
  }
}

test.describe('component gallery', () => {
  /**
   * The sweep is only worth what the page was showing when it ran. A closed
   * dialog and a closed listbox have no ARIA left to get wrong, so this asserts
   * the stateful specimens are genuinely open before anything else runs.
   */
  test('the stateful specimens are open, so the sweep has something to check', async ({ page }) => {
    await openGallery(page, DEFAULT_APPEARANCE)

    await expect(page.locator('[data-specimen="ConfirmDialog/open"] [role="dialog"]')).toBeVisible()
    await expect(page.locator('[data-specimen="ConfirmDialog/destructive"] [role="dialog"]')).toBeVisible()
    await expect(page.locator('[data-specimen="ConfirmDialog/closed"] [role="dialog"]')).toHaveCount(0)

    const listbox = page.locator('[data-specimen="SearchCombobox/open"] [role="listbox"]')
    await expect(listbox).toBeVisible()
    await expect(listbox.getByRole('option')).toHaveCount(3)
    await expect(page.locator('[data-specimen="SearchCombobox/closed"] [role="listbox"]')).toHaveCount(0)
  })

  /**
   * `click()` rather than `check()`: the radios are controlled by the query
   * string, so React reverts the DOM property at the end of the event and sets
   * it from the next render. `check()` asserts the property immediately and
   * would fail on a strip that works. The outcome worth asserting is the two
   * attributes anyway — they are what the whole appearance system is.
   */
  test('the control strip switches theme and density', async ({ page }) => {
    await openGallery(page, DEFAULT_APPEARANCE)

    await page.getByRole('radio', { name: 'Light' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

    await page.getByRole('radio', { name: 'Compact' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-density', 'compact')

    await page.getByRole('radio', { name: 'Dark' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')

    await page.getByRole('radio', { name: 'Comfortable' }).click()
    await expect(page.locator('html')).not.toHaveAttribute('data-density', 'compact')

    // The appearance is addressable: the strip and the URL are one mechanism.
    await expect(page).toHaveURL(/theme=dark&density=comfortable/)
  })

  for (const appearance of APPEARANCES) {
    const label = `${appearance.theme}-${appearance.density}`

    test.describe(label, () => {
      test('has no accessibility violations', async ({ page }) => {
        await openGallery(page, appearance)
        await sweep(page)
      })

      for (const group of GROUPS) {
        test(`${group} matches its baseline`, async ({ page }) => {
          await openGallery(page, appearance)
          // Kills the skeleton sweep, the spinner, the live dot and the caret.
          // Without it a baseline fails on its own animation phase.
          await freeze(page)
          await expect(page.locator(`#group-${group}`)).toHaveScreenshot(`${group}-${label}.png`)
        })
      }
    })
  }
})
