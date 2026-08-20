/**
 * The developer drawer.
 *
 * Three of these are correctness rather than coverage:
 *
 * · **Closed, it is `aria-hidden` AND `inert`**, so its thirty-odd controls are
 *   out of both the accessibility tree and the tab order. One of the two is a
 *   bug either way round.
 * · **The journal never holds a playback ticket**, which is asserted against
 *   `container.innerHTML` rather than against a query, because the failure this
 *   guards is a ticket appearing *somewhere* — in a title attribute, a data
 *   attribute, an unrendered `<pre>` — and not necessarily somewhere a query
 *   would look.
 * · **An unconfigured Tempo emits no anchor**, and says so in a sentence. A
 *   dead link costs a click to discover.
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { renderApp, screen, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { deadLinks } from '@/test/trace'
import { server } from '@/test/server'
import { sources, traceResponse } from '@/test/fixtures'
import { TRACE_HEADER, loadOperationTemplates, request } from '@/api'
import { resetForTests } from '@/api/devlog'
import { PLAYBACK_TICKET, SOURCE_LIVING_ROOM, TITLE_ENRICHED, TRACE_ID } from '@/test/fixtures/ids'
import { DevDrawerProvider } from '@/app/dev-drawer-context'
import { RuntimeConfigContext } from '@/app/runtime-config-context'
import type { RuntimeConfig } from '@/app/runtime-config'
import { LayerStackProvider } from '@/patterns'
import { DevDrawer } from './DevDrawer'

const UNCONFIGURED: RuntimeConfig = { version: '0.1.0', grafanaUrl: null, tempoUrl: null }

beforeEach(() => {
  resetForTests()
})

function renderDrawer(config: RuntimeConfig = UNCONFIGURED) {
  return renderApp(
    <RuntimeConfigContext.Provider value={config}>
      <LayerStackProvider>
        <DevDrawerProvider>
          <button type="button">outside the drawer</button>
          <DevDrawer />
        </DevDrawerProvider>
      </LayerStackProvider>
    </RuntimeConfigContext.Provider>,
    { theme: 'light', density: 'compact' },
  )
}

/** `⌘\`, the binding `dev-drawer-context.tsx` owns. */
async function toggleWithKeyboard(user: ReturnType<typeof renderDrawer>['user']) {
  await user.keyboard('{Meta>}\\{/Meta}')
}

function drawerElement(): HTMLElement {
  return screen.getByLabelText('Developer drawer')
}

/**
 * A row of the coverage ledger. Two of these operations are also named in the
 * prose above the list, so the row is the match that has an `li` ancestor.
 */
function ledgerRow(operation: string): HTMLElement {
  for (const node of screen.getAllByText(operation)) {
    const row = node.closest('li')
    if (row !== null) return row
  }
  throw new Error(`no coverage row rendered for ${operation}`)
}

describe('DevDrawer', () => {
  it('is aria-hidden AND inert when closed, and none of its controls is tabbable', async () => {
    renderDrawer()
    const drawer = drawerElement()

    expect(drawer).toHaveAttribute('aria-hidden', 'true')
    expect(drawer).toHaveAttribute('inert')

    // The controls exist — the drawer is off-canvas, not unmounted, so the
    // journal survives a toggle — and every one of them is inside the inert
    // subtree, which is what takes them out of the tab order.
    const controls = drawer.querySelectorAll('button, a[href], input, select, textarea')
    expect(controls.length).toBeGreaterThan(0)
    for (const control of controls) {
      expect(control.closest('[inert]')).toBe(drawer)
    }

    // And nothing inside it is reachable by role, which is the other half.
    expect(screen.queryByRole('button', { name: 'Close developer drawer' })).toBeNull()
  })

  it('opens on ⌘\\ and closes again on the same key', async () => {
    const { user } = renderDrawer()

    await toggleWithKeyboard(user)
    expect(drawerElement()).toHaveAttribute('aria-hidden', 'false')
    expect(drawerElement()).not.toHaveAttribute('inert')
    expect(screen.getByRole('button', { name: 'Close developer drawer' })).toBeVisible()

    await toggleWithKeyboard(user)
    expect(drawerElement()).toHaveAttribute('aria-hidden', 'true')
    expect(drawerElement()).toHaveAttribute('inert')
  })

  it('closes on Esc, as the innermost layer, and on its own close button', async () => {
    const { user } = renderDrawer()

    await toggleWithKeyboard(user)
    await user.keyboard('{Escape}')
    expect(drawerElement()).toHaveAttribute('aria-hidden', 'true')

    await toggleWithKeyboard(user)
    await user.click(screen.getByRole('button', { name: 'Close developer drawer' }))
    expect(drawerElement()).toHaveAttribute('aria-hidden', 'true')
  })

  it('says the journal is empty rather than showing an empty box', async () => {
    const { user, container } = renderDrawer()
    await toggleWithKeyboard(user)

    expect(screen.getByText('No requests yet')).toBeVisible()
    expect(screen.getByText('0 entries this session')).toBeVisible()

    await expectNoViolations(container)
  })

  it('shows a redacted body and never the playback ticket URL', async () => {
    await loadOperationTemplates()
    await request(`/titles/${TITLE_ENRICHED}/play`, { method: 'POST' })

    const { user, container } = renderDrawer()
    await toggleWithKeyboard(user)

    expect(screen.getByText(`/titles/${TITLE_ENRICHED}/play`)).toBeVisible()
    expect(screen.getByText('POST /titles/{title_id}/play')).toBeVisible()

    // The response really is in the drawer — everything except the two things
    // that are secrets.
    expect(container.innerHTML).toContain('«redacted — 300 s playback ticket»')
    expect(container.innerHTML).toContain('Living Room Emby')

    // The whole rendered subtree, not a query: a ticket in a title attribute
    // would be just as leaked as one in a <pre>.
    expect(container.innerHTML).not.toContain(PLAYBACK_TICKET)
    expect(container.innerHTML).not.toContain('/stream/')
    expect(container.innerHTML).not.toContain('infuse:')

    expect(
      screen.getByText(/Playback ticket URLs and credentials are redacted before the journal/),
    ).toBeVisible()

    await expectNoViolations(container)
  })

  it('redacts a credential out of a request body as well as a ticket out of a response', async () => {
    await loadOperationTemplates()
    await request('/admin/sources', {
      method: 'POST',
      body: {
        kind: 'emby',
        name: 'Loft Emby',
        base_url: 'http://192.168.50.61:8096',
        username: 'usher',
        password: 'hunter2-correct-horse',
      },
    })

    const { user, container } = renderDrawer()
    await toggleWithKeyboard(user)

    expect(container.innerHTML).toContain('&lt;redacted&gt;')
    expect(container.innerHTML).not.toContain('hunter2-correct-horse')
  })

  it('records a transport failure as a journal entry rather than as silence', async () => {
    await loadOperationTemplates()
    // No handler for this path, and `setup.ts` runs MSW with
    // `onUnhandledRequest: 'error'`, so the fetch rejects — which is exactly the
    // "the request never left" case the journal has to keep visible.
    await request('/not/a/route').catch(() => undefined)

    const { user } = renderDrawer()
    await toggleWithKeyboard(user)

    // Once in the list and once in the detail beneath it.
    expect(screen.getAllByText('no response')).toHaveLength(2)
    expect(screen.getByText('no template matched — this path is not in /openapi.json')).toBeVisible()
  })

  it('filters the journal, and says so when the filter matches nothing', async () => {
    await loadOperationTemplates()
    await request('/admin/sources')
    await request(`/admin/sources/${SOURCE_LIVING_ROOM}/status`)

    const { user } = renderDrawer()
    await toggleWithKeyboard(user)

    const filter = screen.getByRole('textbox', { name: 'Filter by path or method' })
    await user.type(filter, 'status')
    expect(screen.getByText(`/admin/sources/${SOURCE_LIVING_ROOM}/status`)).toBeVisible()

    await user.clear(filter)
    await user.type(filter, 'zzzz')
    expect(screen.getByText('No entry matches')).toBeVisible()
    expect(screen.getByText('filter: "zzzz"')).toBeVisible()
  })

  it('renders the trace link as absent with a sentence, and emits no anchor, when Tempo is unconfigured', async () => {
    await loadOperationTemplates()
    await request('/admin/sources')

    const { user, container } = renderDrawer()
    await toggleWithKeyboard(user)

    expect(
      screen.getByText('Tempo is not configured on this deployment, so there is no trace link.'),
    ).toBeVisible()
    expect(screen.queryByRole('link', { name: /trace/i })).toBeNull()
    expect(container.querySelectorAll('a')).toHaveLength(0)
  })

  it('says the response carried no trace id when Tempo is configured and it did not', async () => {
    await loadOperationTemplates()
    await request('/admin/sources')

    const { user, container } = renderDrawer({
      version: '0.1.0',
      grafanaUrl: null,
      tempoUrl: 'https://tempo.lan',
    })
    await toggleWithKeyboard(user)

    expect(
      screen.getByText('This response carried no trace id, so there is nothing to open in Tempo.'),
    ).toBeVisible()
    expect(container.querySelectorAll('a')).toHaveLength(0)

    // No `REQUIRES BACKEND WORK` label here any more. Usher *does* send a
    // `traceresponse` header now, so a panel saying the capability is missing
    // would tell an operator something false — which is worse than saying
    // nothing. The sentence above is the honest one: this particular response
    // carried no id.
    expect(screen.queryByText('Requires backend work')).toBeNull()
    expect(screen.queryByText(/no Usher response carries a trace id today/i)).toBeNull()
  })

  it('links a journal entry into Tempo from the traceresponse header on its response', async () => {
    await loadOperationTemplates()
    // A **200**, not a failure. The header is on every response, and "why was
    // this 200 four seconds slow" is the question the journal exists for.
    server.use(
      http.get('/admin/sources', () =>
        HttpResponse.json(sources, { headers: { [TRACE_HEADER]: traceResponse() } }),
      ),
    )
    await request('/admin/sources')

    const { user, container } = renderDrawer({
      version: '0.1.0',
      grafanaUrl: null,
      tempoUrl: 'https://tempo.lan',
    })
    await toggleWithKeyboard(user)

    const link = screen.getByRole('link', { name: /open trace/i })
    expect(link).toHaveAttribute('href', `https://tempo.lan/explore?traceId=${TRACE_ID}`)
    // The full id beside it, in mono, because an operator pastes it elsewhere.
    expect(screen.getByText(TRACE_ID)).toBeVisible()
    expect(deadLinks(container)).toHaveLength(0)
  })

  it('renders no anchor for a trace id the deployment cannot open', async () => {
    await loadOperationTemplates()
    server.use(
      http.get('/admin/sources', () =>
        HttpResponse.json(sources, { headers: { [TRACE_HEADER]: traceResponse() } }),
      ),
    )
    await request('/admin/sources')

    const { user, container } = renderDrawer()
    await toggleWithKeyboard(user)

    expect(
      screen.getByText('Tempo is not configured on this deployment, so there is no trace link.'),
    ).toBeVisible()
    expect(container.querySelectorAll('a')).toHaveLength(0)
  })

  it('greens an operation only once the session has exercised it, and caps the ledger at 34 of 35', async () => {
    await loadOperationTemplates()
    await request(`/titles/${TITLE_ENRICHED}/play`, { method: 'POST' })

    const { user, container } = renderDrawer()
    await toggleWithKeyboard(user)
    await user.click(screen.getByRole('tab', { name: /API coverage/ }))

    expect(screen.getByText(/1 of 35 operations exercised in this session/)).toBeVisible()
    expect(screen.getByText(/The ceiling is 34 of 35/)).toBeVisible()

    expect(ledgerRow('POST /titles/{title_id}/play')).toHaveClass('u-devdrawer__op--hit')
    expect(ledgerRow('GET /meta/attribution')).not.toHaveClass('u-devdrawer__op--hit')

    // `GET /events` is an EventSource and bypasses the client, so it can never
    // green — and it says so rather than reading as a permanent failure.
    const events = ledgerRow('GET /events')
    expect(events).not.toHaveClass('u-devdrawer__op--hit')
    expect(within(events).getByText('not observable from the client')).toBeVisible()

    await expectNoViolations(container)
  })

  it('keeps coverage when the visible journal is cleared, because they are accumulated apart', async () => {
    await loadOperationTemplates()
    await request('/admin/sources')

    const { user } = renderDrawer()
    await toggleWithKeyboard(user)
    await user.click(screen.getByRole('tab', { name: /API coverage/ }))

    expect(screen.getByText(/1 of 35 operations exercised in this session/)).toBeVisible()
    expect(ledgerRow('GET /admin/sources')).toHaveClass('u-devdrawer__op--hit')
  })
})
