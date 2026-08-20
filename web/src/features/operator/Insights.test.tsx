/**
 * Insights.
 *
 * The assertion this file exists for is the last one in the first block: a
 * panel whose metric has **never fired** and a panel whose value is a
 * **measured zero** must be told apart by something other than colour. They
 * looked identical in the reference client, and a metrics screen that cannot
 * distinguish "nothing has ever reported this" from "this is zero" is worse
 * than no metrics screen.
 */

import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { renderApp, screen, waitFor, within } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { server } from '@/test/server'
import { degradedReadiness } from '@/test/handlers'
import { RuntimeConfigContext } from '@/app/runtime-config-context'
import type { RuntimeConfig } from '@/app/runtime-config'
import Insights from './Insights'

const UNCONFIGURED: RuntimeConfig = { version: '0.1.0', grafanaUrl: null, tempoUrl: null }

function renderInsights(config: RuntimeConfig = UNCONFIGURED) {
  return renderApp(
    <RuntimeConfigContext.Provider value={config}>
      <Insights />
    </RuntimeConfigContext.Provider>,
    { theme: 'light', density: 'compact' },
  )
}

/** The six metrics PRD 10 declares and Prometheus has never seen a sample of. */
const NEVER_FIRED = [
  'usher.bootstrap.rows',
  'usher.bootstrap.batch.duration',
  'usher.bootstrap.phase.duration',
  'usher.bootstrap.failures',
  'usher.curation.rows',
  'usher.curation.dropped',
]

describe('Insights', () => {
  it('prints every panel’s metric name, because that is how the series is found in Grafana', async () => {
    const { container } = renderInsights()

    expect(await screen.findByRole('heading', { level: 1, name: 'Insights' })).toBeVisible()
    expect(screen.getByText('35 metrics emitted')).toBeVisible()
    expect(screen.getByText('6 have never fired')).toBeVisible()

    // The other way into the drawer, beside ⌘\.
    expect(screen.getByRole('button', { name: 'Developer drawer (⌘\\)' })).toBeVisible()

    for (const metric of [
      ...NEVER_FIRED,
      'usher.source.push.connected',
      'usher.jobs.parked',
      'http.server.duration',
      'usher.sse.connections',
    ]) {
      expect(screen.getAllByText(metric).length).toBeGreaterThan(0)
    }

    await expectNoViolations(container)
  })

  it('tells a never-fired panel from a measured zero in words, not only in colour', async () => {
    // `lanes.push: []` on a 503 that still carries a readiness document. The
    // push gauge is genuinely zero here — a fact, not an absence.
    server.use(degradedReadiness())
    renderInsights()

    const push = await screen.findByRole('region', { name: 'Push connectivity' })
    await waitFor(() => expect(within(push).getByText('0 of 1')).toBeVisible())

    // The measured zero says so in words and is not drawn as never-fired.
    expect(within(push).getByText('measured zero')).toBeVisible()
    expect(push).not.toHaveClass('u-panel--never')
    expect(within(push).queryByText('No sample has ever arrived for this metric.')).toBeNull()

    // The never-fired panel says the opposite thing, also in words, and carries
    // no value at all — a zero here would be the lie the whole screen is
    // organised against.
    const never = screen.getByRole('region', { name: 'Curated rows generated' })
    expect(within(never).getByText('No sample has ever arrived for this metric.')).toBeVisible()
    expect(never).toHaveClass('u-panel--never')
    expect(within(never).queryByText('measured zero')).toBeNull()

    expect(screen.getAllByText('No sample has ever arrived for this metric.')).toHaveLength(6)
  })

  it('renders panel chrome immediately and skeletons only the value', async () => {
    renderInsights()

    // §1: the metric name is on screen while the number is still in flight,
    // because it is the one thing an operator can act on before the value lands.
    expect(screen.getByText('usher.source.push.connected')).toBeVisible()
    expect(screen.getByText('Loading Push connectivity …')).toBeInTheDocument()

    await waitFor(() => expect(screen.getByText('1 of 1')).toBeVisible())
  })

  it('says the number is unknown rather than zero when readiness answers something else', async () => {
    // A 503 whose body is not a readiness document — a proxy's own error page.
    server.use(http.get('/health/ready', () => HttpResponse.json({ error: 'upstream' }, { status: 503 })))
    renderInsights()

    expect(
      await screen.findByText(
        'GET /health/ready did not answer with a readiness document, so this number is unknown rather than zero.',
      ),
    ).toBeVisible()
    // Not "0", which would be a claim the read cannot support.
    expect(screen.queryByText('measured zero')).toBeNull()
  })

  it('renders Grafana as absent with a sentence, and emits no anchor, when it is unconfigured', async () => {
    const { container } = renderInsights()
    await screen.findByRole('heading', { level: 1, name: 'Insights' })

    expect(
      screen.getByText(/Grafana is not configured on this deployment, so there is no link to it/),
    ).toBeVisible()
    expect(screen.queryByRole('link', { name: /Grafana/ })).toBeNull()
    expect(container.querySelectorAll('a')).toHaveLength(0)
  })

  it('links out to Grafana in a new tab when it is configured, and never frames it', async () => {
    renderInsights({ version: '0.1.0', grafanaUrl: 'https://grafana.lan', tempoUrl: null })
    await screen.findByRole('heading', { level: 1, name: 'Insights' })

    const link = screen.getByRole('link', { name: /Open Grafana/ })
    expect(link).toHaveAttribute('href', 'https://grafana.lan')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'))

    // The escape hatch is a link and the reason is on screen: Grafana refuses
    // to be framed, so an iframe would be a blank box rather than a panel.
    expect(screen.getByText(/frame-ancestors/)).toBeVisible()
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('lists the five unbuilt dashboards by name and labels the gap REQUIRES BACKEND WORK', async () => {
    const { user, container } = renderInsights()
    await screen.findByRole('heading', { level: 1, name: 'Insights' })

    await user.click(screen.getByRole('tab', { name: /Dashboards/ }))

    for (const name of [
      'Library & Catalog',
      'Taste & Watching',
      'Pipeline',
      'Performance',
      'Cost & Compliance',
    ]) {
      expect(screen.getByText(name)).toBeVisible()
    }
    expect(screen.getAllByText('not built')).toHaveLength(5)

    expect(screen.getAllByText('Requires backend work').length).toBeGreaterThan(0)
    expect(
      screen.getAllByText('ship the five dashboards and seven alert rules as JSON in the compose stack')
        .length,
    ).toBeGreaterThan(0)

    // Not one of the five exists, so not one of them is linked: a per-dashboard
    // link would be a dead link.
    expect(container.querySelectorAll('a')).toHaveLength(0)

    await expectNoViolations(container)
  })

  it('lists the seven alert rules and states that none is armed', async () => {
    const { user, container } = renderInsights()
    await screen.findByRole('heading', { level: 1, name: 'Insights' })

    await user.click(screen.getByRole('tab', { name: /Alerts/ }))

    for (const name of [
      'ingest stalled',
      'push down',
      'jobs parking',
      'enrichment SLA missed',
      'provider degraded',
      'disk projection',
      'cost anomaly',
    ]) {
      expect(screen.getByText(name)).toBeVisible()
    }
    expect(screen.getAllByText('never armed')).toHaveLength(7)
    expect(screen.getByText('0 alert rules provisioned')).toBeVisible()

    await expectNoViolations(container)
  })

  it('states the three label hazards where somebody hunting a series will read them', async () => {
    renderInsights()
    await screen.findByRole('heading', { level: 1, name: 'Insights' })

    expect(screen.getByRole('heading', { level: 2, name: 'Finding a series' })).toBeVisible()
    expect(screen.getByText(/every process restart mints a new one/)).toBeVisible()
    expect(screen.getByText(/Two vocabularies under one label name/)).toBeVisible()
    expect(screen.getByText('{service_name="docker"}')).toBeVisible()

    // It is outside the tabs, so it does not disappear when the tab changes.
    expect(screen.getByRole('tab', { name: /Daily numbers/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('carries no percentage: nothing on this screen has an invented denominator', async () => {
    const { container } = renderInsights()
    await waitFor(() => expect(screen.getByText('1 of 1')).toBeVisible())

    // "1 of 1" is a real denominator — the sources that support push. There is
    // no share of anything anywhere else.
    expect(container.textContent).not.toMatch(/\d%/)
  })
})
