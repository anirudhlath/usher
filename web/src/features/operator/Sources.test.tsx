import { afterEach, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { ToastProvider } from '@/patterns'
import { ToastStack } from '@/features/shared/ToastStack'
import { renderApp, screen, within } from '@/test/render'
import { server } from '@/test/server'
import { expectNoViolations } from '@/test/axe'
import { problemHandler, sourceUnavailable, validationFailed } from '@/test/fixtures'
import { SOURCE_LIVING_ROOM, TRACE_ID } from '@/test/fixtures/ids'
import { TEMPO_URL, deadLinks, traceLinks, withTempo } from '@/test/trace'
import { ROUTES } from '@/app/routes'
import SourcesScreen from './Sources'

function render() {
  return renderApp(
    <ToastProvider>
      <SourcesScreen />
      <ToastStack />
    </ToastProvider>,
    { theme: 'light', density: 'compact', route: ROUTES.sources },
  )
}

/** The key/value row for one probe field, so an assertion cannot pick up a neighbour's badge. */
function fieldRow(label: string): HTMLElement {
  const node = screen.getByText(label).closest('div')
  if (!(node instanceof HTMLElement)) throw new Error(`no field row for ${label}`)
  return node
}

function toasts(): HTMLElement {
  return screen.getByRole('region', { name: 'Notifications' })
}

afterEach(() => {
  server.events.removeAllListeners()
})

describe('Sources', () => {
  it('lists the configured sources and selects the first one', async () => {
    render()

    expect(await screen.findByRole('heading', { level: 1, name: 'Sources' })).toBeInTheDocument()
    expect(await screen.findByText('Loft Emby')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Living Room Emby' })).toBeInTheDocument()
  })

  it('shows the loading state as a table-shaped skeleton in a busy region', () => {
    const { container } = render()

    expect(screen.getByText('Loading sources …')).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
  })

  it('does not probe on load — the round trip is an explicit action', async () => {
    const requested: string[] = []
    server.events.on('request:start', ({ request }) => {
      requested.push(new URL(request.url).pathname)
    })
    render()

    await screen.findByText('Loft Emby')
    expect(requested).toContain('/admin/sources')
    expect(requested.some((path) => path.endsWith('/status'))).toBe(false)

    // And it says so, with the field that proves it.
    expect(screen.getByText('Not probed')).toBeInTheDocument()
    expect(screen.getByText('status: not requested')).toBeInTheDocument()
    expect(within(fieldRow('reachable')).getByText('unknown')).toBeInTheDocument()
    expect(within(fieldRow('authenticated')).getByText('unknown')).toBeInTheDocument()
    expect(within(fieldRow('push_available')).getByText('unknown')).toBeInTheDocument()
  })

  it('probes on demand and keeps "we asked and it said no" apart from "we have not asked"', async () => {
    const { user } = render()

    // The unreachable source answers `authenticated: false` and
    // `push_available: null` in the same body — two different facts.
    await user.click(await screen.findByText('Loft Emby'))
    expect(screen.getByRole('heading', { level: 2, name: 'Loft Emby' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Probe now' }))

    expect(await within(fieldRow('reachable')).findByText('no')).toBeInTheDocument()
    expect(within(fieldRow('authenticated')).getByText('no')).toBeInTheDocument()
    expect(within(fieldRow('push_available')).getByText('unknown')).toBeInTheDocument()
    expect(
      screen.getByText('Connection refused after 5.0 s (http://192.168.50.61:8096/System/Info)'),
    ).toBeInTheDocument()
    expect(within(fieldRow('server_version')).getByText('— never answered')).toBeInTheDocument()
  })

  it('renders is_administrator: true in warn tone with its sentence, never as a success', async () => {
    const { user } = render()

    await screen.findByText('Loft Emby')
    await user.click(screen.getByRole('button', { name: 'Probe now' }))

    const row = fieldRow('is_administrator')
    const badge = await within(row).findByText('yes')
    expect(badge).toHaveClass('u-badge--warn')
    expect(badge).not.toHaveClass('u-badge--good')
    expect(screen.getByText('Usher holds an administrator session on this server.')).toBeInTheDocument()
  })

  it('shows device_id with the reason it is shown', async () => {
    render()

    expect(await screen.findByText('usher-4f2a9c1e-living-room')).toBeInTheDocument()
    expect(
      screen.getByText('Find this in Emby’s own dashboard under Devices to revoke Usher’s session.'),
    ).toBeInTheDocument()
  })

  it('walks the connection wizard through a test step, states where the password goes, and never echoes it', async () => {
    const { user } = render()

    await screen.findByText('Loft Emby')
    await user.click(screen.getByRole('button', { name: 'Connect a media server' }))

    const dialog = screen.getByRole('dialog', { name: 'Connect a media server' })
    expect(within(dialog).getByText('step 1 of 3')).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('Name'), 'Attic')
    await user.type(within(dialog).getByLabelText('Base URL'), 'http://attic.lan:8096')
    await user.click(within(dialog).getByRole('button', { name: 'Next' }))

    expect(within(dialog).getByText('step 2 of 3')).toBeInTheDocument()
    const password = within(dialog).getByLabelText('Password')
    expect(password).toHaveAttribute('type', 'password')
    expect(
      within(dialog).getByText('Sent once, stored encrypted on the server, and never returned by the API.'),
    ).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('Username'), 'usher')
    await user.type(password, 'hunter2')
    // Write-only: the value is never read back into markup.
    expect(screen.queryByText('hunter2')).toBeNull()

    await user.click(within(dialog).getByRole('button', { name: 'Review' }))
    expect(within(dialog).getByText('step 3 of 3')).toBeInTheDocument()
    expect(within(dialog).getByText('Not tested yet')).toBeInTheDocument()
    expect(
      within(dialog).getByText(
        'GET /admin/sources/{id}/status needs an id — this source does not have one yet',
      ),
    ).toBeInTheDocument()
    expect(within(dialog).getByText('not measured on this deployment')).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Connect and start the first sync' }))

    const receipt = await within(toasts()).findByRole('status')
    expect(receipt.textContent).toContain('Queued a full sync of Living Room Emby')
    expect(within(receipt).getByText(`key sync:full:${SOURCE_LIVING_ROOM}`)).toBeInTheDocument()
  })

  it('frames a sync by consequence, states an unmeasured duration, and raises a Queued receipt', async () => {
    const { user } = render()

    await screen.findByText('Loft Emby')
    await user.click(screen.getByRole('button', { name: 'Full sync' }))

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Run a full sync of Living Room Emby?')).toBeInTheDocument()
    expect(within(dialog).getByText('every item the source reports')).toBeInTheDocument()
    expect(within(dialog).getByText('not measured on this deployment')).toBeInTheDocument()
    expect(within(dialog).getByText('202 with a key you cannot yet query')).toBeInTheDocument()

    const confirm = within(dialog).getByRole('button', { name: 'Queue full sync' })
    expect(confirm).toHaveClass('u-btn--primary')
    await user.click(confirm)

    const receipt = await within(toasts()).findByRole('status')
    expect(receipt.textContent).toContain('Queued')
    expect(receipt.textContent).not.toContain('Done')
    expect(within(receipt).getByText(`key sync:full:${SOURCE_LIVING_ROOM}`)).toBeInTheDocument()
    expect(within(receipt).getByRole('link', { name: /Watch it on Pipeline/ })).toBeInTheDocument()
  })

  it('reserves type-to-confirm and the red button for the one irreversible action', async () => {
    const { user } = render()

    await screen.findByText('Loft Emby')
    await user.click(screen.getByRole('button', { name: 'Delete' }))

    const dialog = screen.getByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: 'Delete this source' })
    expect(confirm).toHaveClass('u-btn--danger-solid')
    expect(confirm).toBeDisabled()
    expect(within(dialog).getByText('watch state — it survives a source deletion')).toBeInTheDocument()

    await user.type(within(dialog).getByRole('textbox'), 'Living Room Emby')
    expect(confirm).toBeEnabled()
    await user.click(confirm)

    const notice = await within(toasts()).findByRole('status')
    expect(notice.textContent).toContain('Removed Living Room Emby')
  })

  it('labels both missing routes REQUIRES BACKEND WORK and prints them', async () => {
    const { container } = render()

    await screen.findByText('Loft Emby')
    const labels = container.querySelectorAll('.u-backendwork')
    expect(labels).toHaveLength(2)
    expect(screen.getAllByText('Requires backend work')).toHaveLength(2)
    expect(screen.getByText('GET /admin/sources/{id}/runs?limit=&cursor=')).toBeInTheDocument()
    expect(screen.getByText('PATCH /admin/sources/{id}')).toBeInTheDocument()
  })

  it('states the empty case as a fact about this deployment, with a way out of it', async () => {
    server.use(http.get('/admin/sources', () => HttpResponse.json([])))
    render()

    expect(await screen.findByText('No media server is connected')).toBeInTheDocument()
    expect(screen.getByText(/The catalog works without one/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Connect a media server' }).length).toBeGreaterThan(0)
  })

  it('renders a failed list read as a panel-scale Problem with code, status and detail', async () => {
    server.use(problemHandler('get', '/admin/sources', sourceUnavailable('/admin/sources')))
    render()

    expect(await screen.findByText('code source_unavailable')).toBeInTheDocument()
    expect(screen.getByText('HTTP 503')).toBeInTheDocument()
    expect(screen.getByText('Living Room Emby did not answer within 5.0 s.')).toBeInTheDocument()
  })

  describe('the trace link (patterns.md §3)', () => {
    function renderWith(tempoUrl: string | null) {
      return renderApp(
        withTempo(
          <ToastProvider>
            <SourcesScreen />
            <ToastStack />
          </ToastProvider>,
          tempoUrl,
        ),
        { theme: 'light', density: 'compact', route: ROUTES.sources },
      )
    }

    function failList(traceId?: string) {
      server.use(
        problemHandler(
          'get',
          '/admin/sources',
          sourceUnavailable('/admin/sources'),
          traceId === undefined ? {} : { traceId },
        ),
      )
    }

    it('opens the trace in Tempo when the response carried one', async () => {
      failList(TRACE_ID)
      const { container } = renderWith(TEMPO_URL)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })

    it('emits no anchor at all when Tempo is unconfigured', async () => {
      failList(TRACE_ID)
      const { container } = renderWith(null)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('emits no anchor when the response carried no traceresponse header', async () => {
      failList()
      const { container } = renderWith(TEMPO_URL)
      await screen.findByText('code source_unavailable')

      expect(traceLinks(container)).toHaveLength(0)
      expect(deadLinks(container)).toHaveLength(0)
    })

    it('carries the link inside the connect wizard, which is its own component', async () => {
      server.use(
        problemHandler('post', '/admin/sources', validationFailed('/admin/sources'), {
          traceId: TRACE_ID,
        }),
      )
      const { container, user } = renderWith(TEMPO_URL)

      await screen.findByText('Loft Emby')
      await user.click(screen.getByRole('button', { name: 'Connect a media server' }))
      const dialog = screen.getByRole('dialog', { name: 'Connect a media server' })
      await user.type(within(dialog).getByLabelText('Name'), 'Attic')
      await user.type(within(dialog).getByLabelText('Base URL'), 'http://attic.lan:8096')
      await user.click(within(dialog).getByRole('button', { name: 'Next' }))
      await user.type(within(dialog).getByLabelText('Username'), 'usher')
      await user.type(within(dialog).getByLabelText('Password'), 'hunter2')
      await user.click(within(dialog).getByRole('button', { name: 'Review' }))
      await user.click(within(dialog).getByRole('button', { name: 'Connect and start the first sync' }))

      await screen.findByText('code validation_failed')
      expect(traceLinks(container)[0]?.getAttribute('href')).toContain(TRACE_ID)
    })
  })

  it('has no accessibility violations', async () => {
    const { container } = render()

    await screen.findByText('Loft Emby')
    await expectNoViolations(container)
  })

  it('has no accessibility violations with the wizard open', async () => {
    const { container, user } = render()

    await screen.findByText('Loft Emby')
    await user.click(screen.getByRole('button', { name: 'Connect a media server' }))
    await expectNoViolations(container)
  })
})
