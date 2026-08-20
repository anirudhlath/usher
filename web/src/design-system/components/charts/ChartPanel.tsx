import type { ReactElement, ReactNode } from 'react'
import clsx from 'clsx'
import { Icon } from '../icon'
import { Skeleton, SkeletonRegion } from '../feedback'

/**
 * Panel chrome for the native Insights surface — the six-or-so numbers an operator checks daily,
 * with a marked escape hatch to Grafana for everything else. (Decision: native panels, not an
 * iframe. Grafana's theme will never match this one, and an iframe cannot participate in the
 * console's own error/trace idioms.)
 *
 * The state prop carries the thing a blank panel currently gets wrong:
 * · ok    — a real measured value
 * · zero  — a measured zero, drawn in --viz-zero so it reads as data
 * · never — the metric has never produced a sample. Dashed panel, italic sentence, metric name in
 *           mono. Six of the live deployment's 35 metrics are in this state.
 * · stale — samples stopped arriving. Amber border plus "last sample 41 min ago".
 *
 * Panels must aggregate away `instance`: every process restart mints a new one and ten dead series
 * already exist. One metric carries a UUID in `source`, another a human name — never join on it.
 *
 * The chart is inline SVG built from the data. No charting library: eight fixed hues, one sparkline
 * shape and a percentile band do not justify a dependency, and a third-party renderer would not
 * read the theme's tokens.
 */
export interface ChartPanelProps {
  title: string
  /** The metric name, printed in mono. This is how an operator finds it in Grafana. */
  metric?: string
  value?: string | number
  unit?: string
  /** One sentence naming the window and the denominator. */
  sub?: string
  state?: 'ok' | 'zero' | 'never' | 'stale'
  /** Sparkline values. Bars, not a smoothed line — sample counts are discrete. */
  series?: number[]
  /**
   * p50 / p95 / p99 drawn together, with `--viz-band` filling p50–p95. Percentiles are a different
   * chart from a sample count, so they arrive separately rather than as `series`.
   */
  percentiles?: { p50?: number[]; p95?: number[]; p99?: number[] }
  legend?: { label: string; viz: number | string }[]
  children?: ReactNode
  lastSampleAgo?: string
  grafanaHref?: string
  onOpenGrafana?: () => void
  /**
   * patterns.md §1: the panel chrome renders immediately and only the value and the sparkline are
   * skeletons. A whole panel replaced by a placeholder loses the metric name, which is the one
   * thing an operator can act on while the number is still in flight.
   */
  loading?: boolean
}

const SPARK_W = 100
const SPARK_H = 34

function vizToken(viz: number | string): string {
  return `var(--viz-${viz})`
}

function scale(values: number[], max: number): { x: number; y: number }[] {
  const step = values.length > 1 ? SPARK_W / (values.length - 1) : SPARK_W
  return values.map((value, index) => ({
    x: index * step,
    y: max > 0 ? SPARK_H - (value / max) * SPARK_H : SPARK_H,
  }))
}

function toPath(points: { x: number; y: number }[]): string {
  return points.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ')
}

/** Bars, because a sample count is discrete and a smoothed line invents values between samples. */
function Bars({ series, fill }: { series: number[]; fill: string }): ReactElement {
  const max = Math.max(...series)
  const slot = SPARK_W / series.length
  const width = Math.max(slot * 0.68, 0.4)
  return (
    <svg
      className="u-spark__svg"
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      {series.map((value, index) => {
        const height = max > 0 ? Math.max(1, (value / max) * SPARK_H) : 1
        return (
          <rect
            key={index}
            className="u-spark__rect"
            x={index * slot + (slot - width) / 2}
            y={SPARK_H - height}
            width={width}
            height={height}
            style={{ fill }}
          />
        )
      })}
    </svg>
  )
}

function Percentiles({
  percentiles,
}: {
  percentiles: { p50?: number[]; p95?: number[]; p99?: number[] }
}): ReactElement {
  const p50 = percentiles.p50 ?? []
  const p95 = percentiles.p95 ?? []
  const p99 = percentiles.p99 ?? []
  const max = Math.max(...p50, ...p95, ...p99, 0)
  const low = scale(p50, max)
  const high = scale(p95, max)
  const band = low.length > 0 && high.length > 0 ? toPath([...low, ...[...high].reverse()]) : ''
  return (
    <svg
      className="u-spark__svg"
      viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      {band && <polygon className="u-spark__band" points={band} />}
      {p99.length > 0 && (
        <polyline
          className="u-spark__line"
          points={toPath(scale(p99, max))}
          style={{ stroke: 'var(--viz-p99)' }}
        />
      )}
      {p95.length > 0 && (
        <polyline className="u-spark__line" points={toPath(high)} style={{ stroke: 'var(--viz-p95)' }} />
      )}
      {p50.length > 0 && (
        <polyline className="u-spark__line" points={toPath(low)} style={{ stroke: 'var(--viz-p50)' }} />
      )}
    </svg>
  )
}

export function ChartPanel({
  title,
  metric,
  value,
  unit,
  sub,
  state = 'ok',
  series,
  percentiles,
  legend = [],
  children,
  lastSampleAgo,
  grafanaHref,
  onOpenGrafana,
  loading = false,
}: ChartPanelProps): ReactElement {
  /**
   * A metric keeps its colour between panels, so the sparkline takes the hue of the series it
   * belongs to — the first legend entry when there is one, `--viz-1` otherwise. A measured zero is
   * drawn in `--viz-zero` so it cannot be mistaken for a series that never fired.
   */
  const firstLegend = legend[0]
  const seriesFill = state === 'zero' ? 'var(--viz-zero)' : vizToken(firstLegend ? firstLegend.viz : 1)

  return (
    <section
      className={clsx(
        'u-panel',
        state === 'never' && 'u-panel--never',
        state === 'stale' && 'u-panel--stale',
      )}
      aria-label={title}
    >
      <div className="u-panel__head">
        <h3 className="u-panel__title">{title}</h3>
        {/* Printed on every panel: this is how the series is found in Grafana. */}
        {metric && <span className="u-panel__metric">{metric}</span>}
      </div>

      {state === 'never' ? (
        <div className="u-panel__never">
          <span className="u-panel__never-head">
            <Icon name="circle-dashed" size={16} />
            No sample has ever arrived for this metric.
          </span>
          {metric && <code>{metric}</code>}
          <svg
            className="u-spark__svg u-spark__svg--never"
            viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            <line className="u-spark__never" x1="0" y1={SPARK_H - 1} x2={SPARK_W} y2={SPARK_H - 1} />
          </svg>
        </div>
      ) : loading ? (
        <SkeletonRegion busy label={`Loading ${title} …`} className="u-panel__loading">
          <Skeleton shape="block" height={30} width="42%" />
          <Skeleton shape="block" height={SPARK_H} />
        </SkeletonRegion>
      ) : (
        <>
          {value != null && (
            <span className="u-panel__value">
              <span className={state === 'zero' ? 'u-panel__value--zero' : undefined}>{value}</span>
              {unit && <span className="u-panel__unit">{unit}</span>}
              {/* Hue is never the only carrier (§12): the zero says so in words as well. */}
              {state === 'zero' && <span className="u-panel__zero">measured zero</span>}
            </span>
          )}
          {percentiles ? (
            <div className="u-spark">
              <Percentiles percentiles={percentiles} />
            </div>
          ) : (
            series &&
            series.length > 0 && (
              <div className="u-spark">
                <Bars series={series} fill={seriesFill} />
              </div>
            )
          )}
          {children}
          {sub && <span className="u-panel__sub">{sub}</span>}
        </>
      )}

      {(legend.length > 0 || lastSampleAgo || grafanaHref) && (
        <div className="u-panel__foot">
          {legend.map((entry) => (
            <span className="u-panel__legend" key={entry.label}>
              <span className="u-panel__key" style={{ background: vizToken(entry.viz) }} />
              {entry.label}
            </span>
          ))}
          {lastSampleAgo && (
            <span className={clsx('u-panel__sub', state === 'stale' && 'u-panel__sub--stale')}>
              {state === 'stale' && <Icon name="history" size={16} />}
              last sample {lastSampleAgo}
            </span>
          )}
          {grafanaHref && (
            <a
              className="u-link u-panel__grafana"
              href={grafanaHref}
              target="_blank"
              rel="noreferrer noopener"
              onClick={onOpenGrafana}
            >
              Open in Grafana<span className="u-visually-hidden"> (opens in a new tab)</span>
            </a>
          )}
        </div>
      )}
    </section>
  )
}
