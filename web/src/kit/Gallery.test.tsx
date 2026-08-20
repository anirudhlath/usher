import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/render'
import Gallery from './Gallery'

/**
 * The gallery's own contract is not how it looks — that is the Playwright
 * baseline's job, and jsdom resolves no custom properties anyway. What is worth
 * asserting here is the part `e2e/kit.spec.ts` depends on and would fail on
 * confusingly: one section per group under a stable id, one addressable name per
 * specimen, and no id used twice on a page that holds all 28 components at once.
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

describe('the component gallery', () => {
  it.each(GROUPS)('has a section for %s under a stable id', (group) => {
    const { container } = renderApp(<Gallery />, { route: '/kit' })
    expect(container.ownerDocument.querySelector(`section#group-${group}`)).not.toBeNull()
  })

  it('gives every specimen its own name, so a spec addresses one rendering', () => {
    const { container } = renderApp(<Gallery />, { route: '/kit' })
    const names = Array.from(container.querySelectorAll('[data-specimen]')).map((node) =>
      node.getAttribute('data-specimen'),
    )
    expect(names.length).toBeGreaterThan(100)
    expect(new Set(names).size).toBe(names.length)
  })

  it('uses no id twice — every aria reference on this page has one target', () => {
    const { container } = renderApp(<Gallery />, { route: '/kit' })
    const ids = Array.from(container.querySelectorAll('[id]')).map((node) => node.id)
    const duplicates = ids.filter((id, index) => ids.indexOf(id) !== index)
    expect(duplicates).toEqual([])
  })

  it('reads the appearance out of the query string', () => {
    renderApp(<Gallery />, { route: '/kit?theme=light&density=compact', theme: 'dark' })
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(document.documentElement.getAttribute('data-density')).toBe('compact')
  })

  it('falls back to dark and comfortable for an unrecognised appearance', () => {
    renderApp(<Gallery />, { route: '/kit?theme=sepia&density=roomy', theme: 'light' })
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(document.documentElement.getAttribute('data-density')).toBeNull()
  })
})
