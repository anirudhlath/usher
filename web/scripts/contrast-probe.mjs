import { chromium } from '@playwright/test'
import AxeBuilder from '@axe-core/playwright'

const BASE = 'http://127.0.0.1:4173/console/kit'

const browser = await chromium.launch()
for (const [theme, density] of [
  ['dark', 'comfortable'],
  ['light', 'compact'],
]) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  await page.goto(`${BASE}?theme=${theme}&density=${density}`)
  await page.getByRole('heading', { name: /component gallery/i }).waitFor()

  const results = await new AxeBuilder({ page }).withRules(['color-contrast']).analyze()
  console.log(`\n===== ${theme} / ${density} =====`)
  for (const violation of results.violations) {
    for (const node of violation.nodes) {
      const d = node.any?.[0]?.data ?? {}
      console.log(
        [
          (d.contrastRatio ?? '?').toString().padStart(5),
          'need',
          (d.expectedContrastRatio ?? '?').toString().padStart(6),
          'fg',
          (d.fgColor ?? '?').padEnd(22),
          'bg',
          (d.bgColor ?? '?').padEnd(22),
          (d.fontSize ?? '').padEnd(16),
          node.html.slice(0, 70),
        ].join(' '),
      )
    }
  }
  await context.close()
}
await browser.close()
