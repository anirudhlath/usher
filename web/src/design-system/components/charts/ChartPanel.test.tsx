import { describe, expect, it, vi } from 'vitest'
import { renderComponent, screen } from '@/test/render'
import { expectNoViolations } from '@/test/axe'
import { ChartPanel } from './index'

const SERIES = [8, 11, 9, 14, 13, 17, 16, 21, 19, 24, 22, 26]

/** Operator surfaces are light and compact; this component only ever appears on one. */
const OPERATOR = { theme: 'light', density: 'compact' } as const

describe('ChartPanel — contract', () => {
  it('prints the title, the metric name, the value, the unit and the window', () => {
    const { container } = renderComponent(
      <ChartPanel
        title="Search p95"
        metric="usher.search.duration"
        value="318"
        unit="ms"
        sub="fused mode · last 6 h"
        series={SERIES}
      />,
      OPERATOR,
    )
    expect(screen.getByRole('heading', { name: 'Search p95' })).toBeInTheDocument()
    expect(container.querySelector('.u-panel__metric')).toHaveTextContent('usher.search.duration')
    expect(container.querySelector('.u-panel__value')).toHaveTextContent('318')
    expect(container.querySelector('.u-panel__unit')).toHaveTextContent('ms')
    expect(screen.getByText('fused mode · last 6 h')).toBeInTheDocument()
  })

  it('draws the sparkline as inline SVG bars — one per sample, no charting library', () => {
    const { container } = renderComponent(
      <ChartPanel title="Catalog size" metric="usher.catalog.titles" value="1,268,441" series={SERIES} />,
      OPERATOR,
    )
    const svg = container.querySelector('svg')
    expect(svg).not.toBeNull()
    expect(svg).toHaveAttribute('aria-hidden', 'true')
    expect(container.querySelectorAll('rect.u-spark__rect')).toHaveLength(SERIES.length)
  })

  it('keeps a metric’s colour: the sparkline takes the hue of its series', () => {
    const { container } = renderComponent(
      <ChartPanel
        title="Push events"
        metric="usher.source.push.events"
        value="14"
        series={SERIES}
        legend={[{ label: 'events', viz: 3 }]}
      />,
      OPERATOR,
    )
    expect(container.querySelector('rect.u-spark__rect')?.getAttribute('style')).toContain('var(--viz-3)')
    expect(container.querySelector('.u-panel__key')?.getAttribute('style')).toContain('var(--viz-3)')
  })

  it('draws percentiles with the p50–p95 band and the three percentile hues', () => {
    const { container } = renderComponent(
      <ChartPanel
        title="Search p95"
        metric="usher.search.duration"
        value="318"
        unit="ms"
        percentiles={{ p50: [90, 110, 100], p95: [300, 318, 310], p99: [700, 820, 760] }}
        legend={[
          { label: 'p50', viz: 'p50' },
          { label: 'p95', viz: 'p95' },
          { label: 'p99', viz: 'p99' },
        ]}
      />,
      OPERATOR,
    )
    expect(container.querySelector('polygon.u-spark__band')).not.toBeNull()
    const lines = Array.from(container.querySelectorAll('polyline.u-spark__line')).map((line) =>
      line.getAttribute('style'),
    )
    expect(lines.join(' ')).toContain('var(--viz-p50)')
    expect(lines.join(' ')).toContain('var(--viz-p95)')
    expect(lines.join(' ')).toContain('var(--viz-p99)')
    const keys = Array.from(container.querySelectorAll('.u-panel__key')).map((key) =>
      key.getAttribute('style'),
    )
    expect(keys.join(' ')).toContain('var(--viz-p95)')
  })

  it('links out to Grafana as a marked external escape hatch', async () => {
    const onOpenGrafana = vi.fn<() => void>()
    const { user } = renderComponent(
      <ChartPanel
        title="Catalog size"
        metric="usher.catalog.titles"
        value="1,268,441"
        grafanaHref="/grafana/d/library"
        onOpenGrafana={onOpenGrafana}
      />,
      OPERATOR,
    )
    const link = screen.getByRole('link', { name: /Open in Grafana/ })
    expect(link).toHaveTextContent('Open in Grafana (opens in a new tab)')
    expect(link).toHaveAttribute('href', '/grafana/d/library')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer noopener')
    await user.click(link)
    expect(onOpenGrafana).toHaveBeenCalledTimes(1)
  })

  it('renders the chrome immediately and skeletons only the value and the sparkline', () => {
    const { container } = renderComponent(
      <ChartPanel
        title="Catalog size"
        metric="usher.catalog.titles"
        loading
        grafanaHref="/grafana/d/library"
      />,
      OPERATOR,
    )
    // Chrome: the metric name is what an operator can act on while the number is still in flight.
    expect(screen.getByRole('heading', { name: 'Catalog size' })).toBeInTheDocument()
    expect(container.querySelector('.u-panel__metric')).toHaveTextContent('usher.catalog.titles')
    expect(screen.getByRole('link', { name: /Open in Grafana/ })).toBeInTheDocument()
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull()
    expect(screen.getByText('Loading Catalog size …')).toHaveClass('u-visually-hidden')
    expect(container.querySelectorAll('.u-skel').length).toBeGreaterThan(0)
  })
})

describe('ChartPanel — never fired is not a measured zero', () => {
  it('draws a metric that has never produced a sample as dashed, italic and named', () => {
    const { container } = renderComponent(
      <ChartPanel title="Embeddings refused" metric="usher.search.embeddings.refused" state="never" />,
      OPERATOR,
    )
    expect(container.querySelector('.u-panel--never')).not.toBeNull()
    expect(screen.getByText('No sample has ever arrived for this metric.')).toBeInTheDocument()
    // In mono, so it can be pasted into Grafana's metric browser.
    expect(container.querySelector('.u-panel__never code')).toHaveTextContent(
      'usher.search.embeddings.refused',
    )
    expect(container.querySelector('.u-panel__value')).toBeNull()
    expect(container.querySelector('line.u-spark__never')).not.toBeNull()
  })

  it('draws a measured zero as data, in words as well as in hue', () => {
    const { container } = renderComponent(
      <ChartPanel
        title="Parked jobs"
        metric="usher.jobs.parked"
        value="0"
        state="zero"
        sub="6 h window"
        series={[0, 0, 0]}
      />,
      OPERATOR,
    )
    expect(container.querySelector('.u-panel--never')).toBeNull()
    expect(container.querySelector('.u-panel__value')).toHaveTextContent('0')
    expect(container.querySelector('.u-panel__value--zero')).not.toBeNull()
    expect(screen.getByText('measured zero')).toBeInTheDocument()
    expect(container.querySelector('rect.u-spark__rect')?.getAttribute('style')).toContain('var(--viz-zero)')
  })

  it('makes the two states distinguishable without reading a colour', () => {
    const { container: never } = renderComponent(
      <ChartPanel title="Embeddings refused" metric="usher.search.embeddings.refused" state="never" />,
      OPERATOR,
    )
    const neverText = never.textContent ?? ''
    const { container: zero } = renderComponent(
      <ChartPanel title="Parked jobs" metric="usher.jobs.parked" value="0" state="zero" />,
      OPERATOR,
    )
    const zeroText = zero.textContent ?? ''

    expect(neverText).toContain('No sample has ever arrived for this metric.')
    expect(zeroText).toContain('measured zero')
    expect(zeroText).not.toContain('No sample has ever arrived')
    expect(neverText).not.toContain('measured zero')
  })

  it('marks a stale metric in amber and says how old the last sample is', () => {
    const { container } = renderComponent(
      <ChartPanel
        title="Push events"
        metric="usher.source.push.events"
        value="14"
        state="stale"
        lastSampleAgo="41 min ago"
        sub="Living Room · reconnects 2"
      />,
      OPERATOR,
    )
    expect(container.querySelector('.u-panel--stale')).not.toBeNull()
    expect(screen.getByText(/last sample 41 min ago/)).toBeInTheDocument()
    expect(container.querySelector('[data-icon="history"]')).not.toBeNull()
  })
})

describe('ChartPanel — accessibility (§12)', () => {
  it('is a labelled region with a real heading and a decorative chart', async () => {
    const { container } = renderComponent(
      <ChartPanel
        title="Catalog size"
        metric="usher.catalog.titles"
        value="1,268,441"
        sub="+4,120 today · counts, not a share of anything"
        series={SERIES}
        grafanaHref="/grafana/d/library"
      />,
      OPERATOR,
    )
    expect(screen.getByRole('region', { name: 'Catalog size' })).toBeInTheDocument()
    await expectNoViolations(container)
  })

  it('has no axe violations in the never-fired state', async () => {
    const { container } = renderComponent(
      <ChartPanel title="Embeddings refused" metric="usher.search.embeddings.refused" state="never" />,
      OPERATOR,
    )
    await expectNoViolations(container)
  })

  it('has no axe violations while loading, or on a viewer-side dark surface', async () => {
    const { container } = renderComponent(<ChartPanel title="Queued" metric="usher.jobs.queued" loading />)
    await expectNoViolations(container)
  })
})

describe('ChartPanel — anti-patterns', () => {
  it('never invents a percentage for a count', () => {
    const { container } = renderComponent(
      <ChartPanel
        title="Catalog size"
        metric="usher.catalog.titles"
        value="1,268,441"
        sub="+4,120 today · counts, not a share of anything"
        series={SERIES}
      />,
      OPERATOR,
    )
    expect(container.textContent).not.toContain('%')
  })

  it('offers no instance selector — panels aggregate instance away', () => {
    renderComponent(
      <ChartPanel title="Push events" metric="usher.source.push.events" value="14" series={SERIES} />,
      OPERATOR,
    )
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByText(/instance/i)).not.toBeInTheDocument()
  })
})
