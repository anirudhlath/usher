/**
 * Configuration.
 *
 * Two assertions here are correctness rather than coverage. **A secret never
 * renders its value** — not a value, not a length, not a prefix — and the row
 * model is built so there is no field one could come out of. And **the missing
 * route is labelled rather than faked**: no `GET /admin/config` is invented, so
 * the Current column says what a real read proves and says "not served" for
 * everything else.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp, screen, waitFor, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { degradedReadiness } from '@/test/handlers'
import Config from './Config'
import { CONFIG, SETTING_COUNT } from './Config.settings'

const noop = () => {}

/** The stub `setup.ts` installs answers `false` to everything; this narrows it. */
function setViewport(width: 'phone' | 'desktop') {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: width === 'phone' ? query.includes('max-width: 833px') : query.includes('min-width: 1440px'),
    media: query,
    onchange: null,
    addListener: noop,
    removeListener: noop,
    addEventListener: noop,
    removeEventListener: noop,
    dispatchEvent: () => false,
  }))
}

afterEach(() => setViewport('desktop'))

function renderConfig() {
  return renderApp(<Config />, { theme: 'light', density: 'compact' })
}

/**
 * The row a key is printed in. Read off the cell rather than through
 * `getByRole('row', { name })`, and it takes the *first cell with a `tr`
 * ancestor* because a key also appears in the prose below the table.
 */
function rowFor(key: string): HTMLElement {
  for (const cell of screen.getAllByText(key)) {
    const row = cell.closest('tr')
    if (row !== null) return row
  }
  throw new Error(`no table row rendered for ${key}`)
}

const SECRET_KEYS = CONFIG.filter((row) => row.secret).map((row) => row.key)

describe('Config', () => {
  it('lists every catalogued setting, grouped and counted', async () => {
    const { container } = renderConfig()

    expect(await screen.findByRole('heading', { level: 1, name: 'Configuration' })).toBeVisible()
    // Derived, not written down. The count read `69` in six places on this
    // screen and five here until four console settings were added to
    // `Settings` and every one of them became wrong at once. The number that
    // matters is pinned against the backend by
    // `tests/unit/test_console_settings_catalogue.py`, not by a literal here.
    expect(CONFIG).toHaveLength(SETTING_COUNT)
    expect(screen.getByText(`${SETTING_COUNT} of ${SETTING_COUNT} shown`)).toBeVisible()

    // Every setting plus the header row.
    expect(screen.getAllByRole('row')).toHaveLength(SETTING_COUNT + 1)
    expect(screen.getByRole('table', { name: 'Configuration' })).toBeVisible()

    await expectNoViolations(container)
    // 20 s rather than the 5 s default, and for this case only. axe walks every
    // node, and this screen is the largest single table in the product — 73
    // rows plus their descriptions. Isolated it finishes in well under a
    // second; under the full suite's parallelism it crossed 5 s and failed as a
    // timeout, which reads as a defect in the screen rather than as a slow
    // sweep. Raising the *global* timeout would hide a real hang somewhere
    // else, so the allowance is scoped to the case that earned it.
  }, 20_000)

  it('has no editor, and says why there is none rather than disabling one', async () => {
    renderConfig()
    await screen.findByRole('heading', { level: 1, name: 'Configuration' })

    expect(
      screen.getByText(/There is no editor on this screen and there is no write route behind one/),
    ).toBeVisible()

    // The only controls on the screen are the two that filter it, plus the
    // drawer toggle in the header. Nothing writes.
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
    expect(screen.getByRole('textbox', { name: 'Find a setting' })).toBeVisible()
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: /save|apply|edit/i })).toBeNull()

    // The drawer toggle is the shell's, not this screen's — hoisted into
    // `OpsHeader` so eight operator screens do not each carry a copy.
    expect(screen.getByRole('button', { name: 'Developer drawer (⌘\\)' })).toBeVisible()
  })

  it('never renders a secret’s value, its length or a prefix of it', async () => {
    const { container } = renderConfig()
    await waitFor(() => expect(within(rowFor('USHER_DATABASE_URL')).getByText('•••• set')).toBeVisible())

    expect(SECRET_KEYS).toEqual([
      'USHER_DATABASE_URL',
      'USHER_SECRET_KEY',
      'USHER_TMDB_API_KEY',
      'USHER_EMBEDDING_API_KEY',
      'USHER_LLM_API_KEY',
    ])

    for (const key of SECRET_KEYS) {
      const text = rowFor(key).textContent ?? ''
      // Two renderings only, and both are words.
      expect(text).toMatch(/•••• set|not set|not served/)
      // A DSN, a bearer token or a "12 characters" hint would each be a leak.
      expect(text).not.toContain('://')
      expect(text).not.toMatch(/\bcharacters?\b/)
      expect(text).not.toMatch(/\bBearer\b/)
      expect(text).not.toMatch(/\bsk-/)
    }

    // The reference client printed the whole DSN in this column.
    expect(container.innerHTML).not.toContain('postgresql+asyncpg://')
  })

  it('shows a secret as set only where a real read proves it, and names the read', async () => {
    renderConfig()
    await waitFor(() => expect(within(rowFor('USHER_DATABASE_URL')).getByText('•••• set')).toBeVisible())

    expect(
      within(rowFor('USHER_DATABASE_URL')).getByText('checks.database on GET /health/ready'),
    ).toBeVisible()
    expect(within(rowFor('USHER_SECRET_KEY')).getByText('•••• set')).toBeVisible()

    // Nothing proves the optional credentials either way, so nothing claims to.
    expect(within(rowFor('USHER_TMDB_API_KEY')).getByText('not served')).toBeVisible()
    expect(within(rowFor('USHER_LLM_API_KEY')).getByText('not served')).toBeVisible()
  })

  it('reports the lane switches from readiness, and only in the direction the read supports', async () => {
    renderConfig()
    await waitFor(() =>
      expect(within(rowFor('USHER_WORKER_ENABLED')).getByText('lanes.worker')).toBeVisible(),
    )

    // The default and the observation agree, and both are printed: a screen
    // that showed only one of them could not tell them apart.
    expect(within(rowFor('USHER_WORKER_ENABLED')).getAllByText('true')).toHaveLength(2)
    expect(within(rowFor('USHER_PUSH_ENABLED')).getByText('lanes.push names a running lane')).toBeVisible()
  })

  it('claims nothing about push when no lane is running, because an empty list proves nothing', async () => {
    // `lanes.push: []` is equally consistent with the switch being off and with
    // no source supporting push, so the screen makes neither claim.
    server.use(degradedReadiness())
    renderConfig()

    await waitFor(() => expect(within(rowFor('USHER_WORKER_ENABLED')).getByText('false')).toBeVisible())
    expect(within(rowFor('USHER_PUSH_ENABLED')).getByText('not served')).toBeVisible()
  })

  it('says no route returns the running configuration, and labels REQUIRES BACKEND WORK', async () => {
    renderConfig()
    await screen.findByRole('heading', { level: 1, name: 'Configuration' })

    expect(screen.getByText('No route returns the running configuration')).toBeVisible()
    expect(
      screen.getByText('/openapi.json declares 33 paths and none of them is a settings read'),
    ).toBeVisible()

    expect(screen.getByText('Requires backend work')).toBeVisible()
    expect(
      screen.getByText(
        `a read-only projection of Settings: ${SETTING_COUNT} keys with the value this process read, every SecretStr rendered as a boolean and never as a value or a length`,
      ),
    ).toBeVisible()

    // Every row nothing serves says so, rather than reprinting the default in
    // the Current column and calling it the truth. Derived from the catalogue:
    // whatever has no `observed`, less the four `/health/ready` proves at
    // runtime — the two required secrets and the two lane switches. Writing the
    // number down is how it silently stopped being true when four settings were
    // added to `Settings`.
    const PROVED_BY_READINESS = 4
    const unserved = CONFIG.filter((row) => row.observed === undefined).length - PROVED_BY_READINESS
    await waitFor(() => expect(screen.getAllByText('not served')).toHaveLength(unserved))
  })

  it('explains a measured default rather than printing a bare number', async () => {
    const { user } = renderConfig()
    await screen.findByRole('heading', { level: 1, name: 'Configuration' })

    await user.type(screen.getByRole('textbox', { name: 'Find a setting' }), 'ef_search')
    await waitFor(() => expect(screen.getByText(`1 of ${SETTING_COUNT} shown`)).toBeVisible())

    const row = rowFor('USHER_SEARCH_HNSW_EF_SEARCH')
    expect(within(row).getByText(/40 → 0.700, 100 → 0.858, 200 → 0.917, 400 → 0.967/)).toBeVisible()
    expect(within(row).getByText('measured')).toBeVisible()

    // And the standing explanation is on the screen whatever is filtered.
    expect(
      screen.getByText(/because recall@10 measured 0.858 at 100, 0.917 at 200 and 0.967 at 400/),
    ).toBeVisible()
  })

  it('searches by name and by what a setting controls', async () => {
    const { user } = renderConfig()
    await screen.findByRole('heading', { level: 1, name: 'Configuration' })

    const search = screen.getByRole('textbox', { name: 'Find a setting' })
    await user.type(search, 'rrf')
    await waitFor(() => expect(screen.getByText(`1 of ${SETTING_COUNT} shown`)).toBeVisible())
    expect(rowFor('USHER_SEARCH_RRF_K')).toBeVisible()

    await user.clear(search)
    await user.type(search, 'licensing')
    await waitFor(() => expect(screen.getByText(`1 of ${SETTING_COUNT} shown`)).toBeVisible())
    expect(rowFor('USHER_ENRICH_CACHE_MAX_AGE_DAYS')).toBeVisible()
  })

  it('groups by subsystem', async () => {
    const { user } = renderConfig()
    await screen.findByRole('heading', { level: 1, name: 'Configuration' })

    await user.selectOptions(screen.getByRole('combobox', { name: 'Subsystem' }), 'search')
    await waitFor(() => expect(screen.getByText(`5 of ${SETTING_COUNT} shown`)).toBeVisible())
    expect(rowFor('USHER_SEARCH_RRF_K')).toBeVisible()
    expect(screen.queryByText('USHER_SSE_QUEUE_SIZE')).toBeNull()
  })

  it('says so in a sentence when nothing matches', async () => {
    const { user, container } = renderConfig()
    await screen.findByRole('heading', { level: 1, name: 'Configuration' })

    await user.type(screen.getByRole('textbox', { name: 'Find a setting' }), 'zzzz')

    expect(await screen.findByText('No setting matches')).toBeVisible()
    expect(screen.getByText('query: "zzzz"')).toBeVisible()
    expect(screen.getByText(`0 of ${SETTING_COUNT} shown`)).toBeVisible()
    expect(screen.queryByRole('table')).toBeNull()

    await expectNoViolations(container)
  })

  it('becomes stacked cards at 390 rather than shrinking the type', async () => {
    setViewport('phone')
    const { container } = renderConfig()
    await screen.findByRole('heading', { level: 1, name: 'Configuration' })

    expect(screen.getByRole('group', { name: 'Configuration' })).toBeVisible()
    expect(screen.queryByRole('table')).toBeNull()

    await expectNoViolations(container)
  })
})
