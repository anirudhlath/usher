import { expect, test } from '@playwright/test'
import { visit } from './helpers'

/**
 * The design system's stylesheet reaches the browser.
 *
 * This exists because it once did not, and nothing noticed. `src/styles/app.css`
 * imported the handoff's own `design-system/styles.css`, whose entire content is
 * `@import` rules — and CSS requires `@import` to precede all other rules, so
 * once `@import "tailwindcss"` was inlined ahead of them every one was dropped.
 * The build said so, as eleven warnings among its normal output. The unit suite
 * was unaffected: jsdom computes no layout and resolves no custom properties, so
 * 900-odd component tests passed against a product that rendered as unstyled
 * black-on-white. The axe sweep passed too, for the same reason — browser
 * defaults have excellent contrast.
 *
 * So the guard has to be exactly this: a **real browser**, resolving a **real
 * token**, on the **built** bundle. Anything cheaper is what already failed to
 * catch it.
 */
test.describe('the design system reaches the browser', () => {
  test('semantic tokens resolve to real values', async ({ page }) => {
    await visit(page, '/')

    const tokens = await page.evaluate(() => {
      const style = getComputedStyle(document.documentElement)
      return {
        canvas: style.getPropertyValue('--bg-canvas').trim(),
        textPrimary: style.getPropertyValue('--text-primary').trim(),
        borderSubtle: style.getPropertyValue('--border-subtle').trim(),
        zModal: style.getPropertyValue('--z-modal').trim(),
        radiusCard: style.getPropertyValue('--radius-card').trim(),
      }
    })

    // One from each token file that has to have loaded: semantic, layers,
    // spacing. An empty string is what a dropped `@import` produces, and it is
    // the failure this test is for.
    expect(tokens.canvas, '--bg-canvas is undefined: the token layer did not load').not.toBe('')
    expect(tokens.textPrimary).not.toBe('')
    expect(tokens.borderSubtle).not.toBe('')
    expect(tokens.zModal).toBe('410')
    expect(tokens.radiusCard).toBe('10px')
  })

  test('the theme attribute actually re-resolves the tokens', async ({ page }) => {
    await visit(page, '/')

    const read = () =>
      page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--bg-canvas').trim())

    const dark = await read()
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'))
    const light = await read()

    // Both halves matter. Equal values would mean the light block never loaded —
    // which a token layer that loaded only its first file would also produce.
    expect(dark).not.toBe('')
    expect(light).not.toBe('')
    expect(light, 'the light theme resolves to the dark value: tokens/semantic.css is incomplete').not.toBe(
      dark,
    )
  })

  test('a component stylesheet loaded, not only the tokens', async ({ page }) => {
    // `goto` rather than `visit`: the gallery renders `Skeleton` specimens on
    // purpose, and every one of them sits in a region carrying
    // `aria-busy="true"` — which is exactly what `visit` waits for the absence
    // of. On this one route a busy region is the subject, not a pending load.
    await page.goto('/console/kit')
    // By name, not by level: the gallery renders `Problem`'s page-scale
    // specimen, which legitimately carries its own `<h1>`, so a bare level-1
    // query is ambiguous here and nowhere else in the product.
    await expect(page.getByRole('heading', { name: /component gallery/i })).toBeVisible()

    // `.u-btn` is a plain CSS class from `components/actions/actions.css`; if the
    // component layer were missing it would still be *in the markup* and simply
    // have no rules, which is precisely how the original defect hid.
    const button = page.locator('.u-btn').first()
    await expect(button).toBeVisible()

    const height = await button.evaluate((node) => getComputedStyle(node).height)
    expect(height, '.u-btn has no height: the component layer did not load').not.toBe('auto')
    expect(parseFloat(height)).toBeGreaterThan(20)
  })

  /**
   * The brand typefaces are the ones actually rendering.
   *
   * Same defect class as the dropped `@import` above, and it shipped for the
   * same reason: the woff2 files were in the bundle, fingerprinted and served,
   * so every check that asked "are the fonts there" said yes. Nothing asked
   * whether a glyph had ever come from one. `@fontsource-variable` registers
   * its family as **`Instrument Sans Variable`**; `--font-sans` named
   * `Instrument Sans`, which matched no `@font-face` at all, so the whole
   * product rendered in each machine's system fallback.
   *
   * That is invisible to every other layer. jsdom has no font stack, axe reads
   * contrast rather than metrics, and the computed `font-family` string is the
   * *declaration* — it reads back exactly the same whether the family resolved
   * or fell through. The only evidence that separates the two is a `FontFace`
   * reaching `loaded`, which happens only when the page actually demanded it.
   *
   * It surfaced as 120 red screenshots that I misread as a CI rendering
   * quirk. It was not: the fallback simply differs per machine, and once the
   * real face loads a bare host and the pinned container agree to the pixel.
   */
  test('the brand typefaces are the ones rendering, not a system fallback', async ({ page }) => {
    await page.goto('/console/kit')
    await expect(page.getByRole('heading', { name: /component gallery/i })).toBeVisible()

    const faces = await page.evaluate(async () => {
      await document.fonts.ready
      return [...document.fonts].map((face) => ({ family: face.family, status: face.status }))
    })

    // Only the subsets a page actually needs are fetched, so this asserts "at
    // least one face of this family loaded" rather than all of them. Latin is
    // the one every screen demands; cyrillic and vietnamese legitimately stay
    // `unloaded` forever.
    for (const family of ['Instrument Sans Variable', 'JetBrains Mono Variable']) {
      const declared = faces.filter((face) => face.family === family)
      expect(
        declared.length,
        `no @font-face declared for ${family}: the package import is gone`,
      ).toBeGreaterThan(0)
      expect(
        declared.some((face) => face.status === 'loaded'),
        `${family} is declared but no subset ever loaded: --font-sans/--font-mono names a family no @font-face provides, so this page is rendering in a system fallback`,
      ).toBe(true)
    }
  })
})
