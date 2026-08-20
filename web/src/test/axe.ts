import axe, { type AxeResults, type RunOptions } from 'axe-core'
import { expect } from 'vitest'

/**
 * The accessibility contract (patterns.md §12) is a requirement, not a checklist,
 * and each of its clauses was a measured failure in the reference client. So it
 * gets asserted per component rather than swept once at the end.
 *
 * Colour-contrast is disabled here and only here: jsdom computes no layout and
 * resolves no CSS custom properties, so axe sees `rgb(0,0,0)` on `rgb(0,0,0)`
 * and reports every pair as failing. Contrast is verified two other ways — the
 * handoff's 103-pair ledger (`docs/contrast.md`) and the Playwright sweep in
 * `e2e/`, which runs against a real browser with the real stylesheet.
 */
const DEFAULT_OPTIONS: RunOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
}

export async function analyse(container: Element, options: RunOptions = {}): Promise<AxeResults> {
  return axe.run(container, { ...DEFAULT_OPTIONS, ...options })
}

/** Fails with the rule id, the impact and the offending markup — not just a count. */
export async function expectNoViolations(container: Element, options: RunOptions = {}) {
  const results = await analyse(container, options)
  const summary = results.violations.map((violation) => {
    const nodes = violation.nodes.map((node) => `      ${node.html}`).join('\n')
    return `  [${violation.impact ?? 'unknown'}] ${violation.id}: ${violation.help}\n${nodes}`
  })
  expect(summary, `axe found ${results.violations.length} violation(s):\n${summary.join('\n')}`).toEqual([])
}
