import { expect, test } from '@playwright/test'
import { visit } from './helpers'

/**
 * Media tiles must keep their size in the two layouts that host them, and until
 * this spec neither layer had a test: the gallery renders every `PosterCard` in
 * its own `Specimen` box, where both failures below are absent by construction.
 *
 * The cards carried a fixed `width` (`--card-poster-w`) with the flex default
 * `flex-shrink:1` and no grid override, so:
 *   · in a flex **rail** (`.u-rail__track`, the Home rows, the Person
 *     filmography, the TitleDetail similar-titles rail) a row too wide to fit
 *     shrank every card below `--card-poster-w` instead of overflowing — ragged
 *     widths, and a scroll-snap carousel that never scrolled;
 *   · in a **grid** (Browse, Collection) the same fixed width overflowed the
 *     narrower `1fr` track (168 into a 148 column), so tiles overlapped.
 *
 * Both are measured against a real browser: jsdom computes no layout, so the
 * component tests cannot see either one.
 */

test.describe('media tiles keep their size', () => {
  test('poster cards in a rail hold --card-poster-w and the rail scrolls', async ({ page }) => {
    // Force the narrowest designed width so a multi-card row is guaranteed to
    // exceed the viewport — that is the only condition under which a shrinking
    // card would shrink, so the assertion below is not vacuous.
    await page.setViewportSize({ width: 390, height: 844 })
    await visit(page, '/')

    // `--card-poster-w` resolved to px, without assuming the root font size.
    const expected = await page.evaluate(() => {
      const probe = document.createElement('div')
      probe.style.cssText = 'position:absolute;visibility:hidden;width:var(--card-poster-w)'
      document.body.append(probe)
      const width = probe.getBoundingClientRect().width
      probe.remove()
      return width
    })

    const cardWidths = await page
      .locator('.u-rail__track .u-card--poster')
      .evaluateAll((els) => els.map((el) => el.getBoundingClientRect().width))
    expect(cardWidths.length).toBeGreaterThan(0)
    for (const width of cardWidths) expect(width).toBeCloseTo(expected, 0)

    // At least one rail must genuinely overflow — proof the shrink condition was
    // exercised rather than every row happening to fit.
    const overflow = await page
      .locator('.u-rail__track')
      .evaluateAll((els) => els.map((el) => el.scrollWidth - el.clientWidth))
    expect(Math.max(...overflow)).toBeGreaterThan(1)
  })

  test('poster cards in the browse grid do not overflow their column', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await visit(page, '/browse?density=grid')

    const boxes = await page.locator('main .u-card--poster').evaluateAll((els) =>
      els.map((el) => {
        const r = el.getBoundingClientRect()
        return { left: r.left, right: r.right, top: Math.round(r.top) }
      }),
    )
    expect(boxes.length).toBeGreaterThan(1)

    // Group by row, then assert consecutive cards in a row never overlap. A card
    // wider than its track pushes its right edge past the next card's left edge.
    const rows = new Map<number, { left: number; right: number }[]>()
    for (const b of boxes) {
      const row = rows.get(b.top) ?? []
      row.push(b)
      rows.set(b.top, row)
    }
    for (const row of rows.values()) {
      row.sort((a, b) => a.left - b.left)
      for (let i = 1; i < row.length; i++) {
        expect(row[i].left).toBeGreaterThanOrEqual(row[i - 1].right - 0.5)
      }
    }
  })
})
