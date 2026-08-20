import { ChartPanel } from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

/** Twelve buckets, frozen. A sample count is discrete, so it is drawn as bars. */
const SERIES = [8, 11, 9, 14, 13, 17, 16, 21, 19, 24, 22, 26]

const PERCENTILES = {
  p50: [120, 118, 131, 126, 122, 129, 124, 133, 127, 121, 125, 130],
  p95: [280, 291, 305, 298, 312, 301, 318, 322, 309, 297, 314, 318],
  p99: [402, 431, 448, 419, 462, 441, 470, 488, 452, 433, 461, 474],
}

export function ChartsSpecimens() {
  return (
    <GroupSection
      id="charts"
      title="Charts"
      blurb="Native panels rather than an embedded Grafana, and inline SVG rather than a charting library — eight fixed hues and one sparkline shape do not justify a dependency, and a third-party renderer would not read these tokens. Every panel names its window and, where one exists, its denominator."
    >
      <Specimen name="ChartPanel/value" width={320}>
        <div className="k-fill">
          <ChartPanel
            title="Catalog size"
            metric="usher.catalog.titles"
            value="1,268,441"
            sub="+4,120 today · counts, not a share of anything"
            series={SERIES}
            grafanaHref="https://grafana.usher.invalid/d/library"
          />
        </div>
      </Specimen>

      <Specimen
        name="ChartPanel/zero"
        width={320}
        note="A measured zero is data. It is drawn in --viz-zero and says “measured zero” in words, so it cannot be read as a series that never fired."
      >
        <div className="k-fill">
          <ChartPanel
            title="Parked jobs"
            metric="usher.jobs.parked"
            value="0"
            state="zero"
            sub="6 h window · a measured zero"
          />
        </div>
      </Specimen>

      <Specimen
        name="ChartPanel/never"
        width={320}
        note="Mandatory when a metric has no samples. A blank panel that looks healthy is the bug this component exists to fix — six of the live deployment's 35 metrics are in this state."
      >
        <div className="k-fill">
          <ChartPanel title="Embeddings refused" metric="usher.search.embeddings.refused" state="never" />
        </div>
      </Specimen>

      <Specimen
        name="ChartPanel/stale"
        width={320}
        note="Samples stopped arriving. Amber border, the history glyph, and how long ago the last one was."
      >
        <div className="k-fill">
          <ChartPanel
            title="Push events"
            metric="usher.source.push.events"
            value="14"
            state="stale"
            lastSampleAgo="41 min ago"
            sub="Living Room · reconnects 2"
          />
        </div>
      </Specimen>

      <Specimen name="ChartPanel/legend" width={320}>
        <div className="k-fill">
          <ChartPanel
            title="Search p95"
            metric="usher.search.duration"
            value="318"
            unit="ms"
            sub="fused mode · last 6 h"
            legend={[
              { label: 'p50', viz: 1 },
              { label: 'p95', viz: 2 },
              { label: 'p99', viz: 4 },
            ]}
            series={SERIES}
            grafanaHref="https://grafana.usher.invalid/d/search"
          />
        </div>
      </Specimen>

      <Specimen
        name="ChartPanel/percentiles"
        width={320}
        note="p50, p95 and p99 with --viz-band filling p50–p95. Percentiles are a different chart from a sample count, so they arrive separately from `series`."
      >
        <div className="k-fill">
          <ChartPanel
            title="Search latency"
            metric="usher.search.duration"
            value="318"
            unit="ms"
            sub="fused mode · last 6 h"
            percentiles={PERCENTILES}
            legend={[
              { label: 'p50', viz: 'p50' },
              { label: 'p95', viz: 'p95' },
              { label: 'p99', viz: 'p99' },
            ]}
          />
        </div>
      </Specimen>

      <Specimen
        name="ChartPanel/denominator"
        width={320}
        note="The denominator is named because the honest one is not the catalog: 0.98 of the enriched titles, never 98% of your library."
      >
        <div className="k-fill">
          <ChartPanel
            title="Semantic coverage"
            metric="usher.search.embeddings"
            value="0.98"
            sub="of 128,400 enriched titles — not of the 1.27M catalog"
            grafanaHref="https://grafana.usher.invalid/d/search"
          />
        </div>
      </Specimen>

      <Specimen
        name="ChartPanel/loading"
        width={320}
        note="§1: the chrome renders immediately and only the value and the sparkline are skeletons. A whole panel replaced by a placeholder loses the metric name, which is the one thing an operator can act on while the number is in flight."
      >
        <div className="k-fill">
          <ChartPanel title="Search p95" metric="usher.search.duration" unit="ms" loading />
        </div>
      </Specimen>
    </GroupSection>
  )
}
