import { useMemo, useState, type ReactElement } from 'react'
import { Badge, Button, ChartPanel, Icon, StateBlock, Tabs } from '@/design-system'
import { readinessFromError, useReadiness, useSources } from '@/api'
import { useRuntimeConfig } from '@/app/runtime-config-context'
import { BackendWork, OpsHeader, OpsSection } from '@/app/shells/OperatorShell'
import './Insights.css'

/**
 * Insights — native panels over Usher's telemetry, and a marked escape hatch to
 * Grafana for everything else.
 *
 * Three facts govern this screen and every one of them is measured rather than
 * assumed.
 *
 * · **Usher emits 35 metrics. 29 have real series in Prometheus and six have
 *   never fired** — four `usher.bootstrap.*` and two `usher.curation.*`. A
 *   blank panel is otherwise indistinguishable from a healthy zero, which is
 *   the single most misleading thing a metrics screen can do, so `ChartPanel`
 *   separates never-fired from measured-zero **in words as well as in colour**
 *   and this screen uses that separation rather than drawing ten identical
 *   empty boxes.
 * · **The five dashboards and seven alert rules PRD 10 specifies do not
 *   exist.** Zero of either was ever built. That is REQUIRES BACKEND WORK #6,
 *   labelled on screen with all twelve named, so the gap is legible instead of
 *   being a screen nobody knows is missing.
 * · **Usher's HTTP API serves no Prometheus query route**, so a panel carries a
 *   number only where an admin route measures the same thing. The rest name
 *   their series and hand off. That is stated on the screen rather than papered
 *   over with a fabricated value.
 *
 * **No iframes.** Grafana's own `frame-ancestors` policy would refuse one, its
 * theme cannot be made to match this one, and a framed panel cannot participate
 * in this console's error and trace idioms — so the escape hatch is a link, and
 * the link is **absent with a sentence** when `grafanaUrl` is unconfigured
 * rather than dead.
 */

/** PRD 10's five dashboards, none of which has ever been built. */
const DASHBOARDS: readonly (readonly [string, string])[] = [
  ['Library & Catalog', 'catalog size, enrichment tiers, unmatched backlog, image coverage'],
  ['Taste & Watching', 'row provider performance — what gets built and what gets opened'],
  ['Pipeline', 'queue depth, parking, throughput, sync runs, push connectivity'],
  ['Performance', 'HTTP p50/p95/p99 by route, search by mode, home composition, cache hit rates'],
  ['Cost & Compliance', 'LLM spend, provider request volume, attribution status'],
]

/** PRD 10's seven alerts. None is armed; nothing provisions them. */
const ALERTS: readonly (readonly [string, string, 'warn' | 'bad'])[] = [
  ['ingest stalled', 'bootstrap heartbeat older than 120 s', 'warn'],
  ['push down', 'usher.source.push.connected drops to 0 for 5 m', 'bad'],
  ['jobs parking', 'usher.jobs.parked increases for 15 m', 'warn'],
  ['enrichment SLA missed', 'usher.enrichment.latency p95 over 5 s', 'warn'],
  ['provider degraded', 'usher.provider.requests error ratio over 5%', 'bad'],
  ['disk projection', 'image cache growth projects full within 14 d', 'warn'],
  ['cost anomaly', 'LLM spend over 3× the 7-day median', 'bad'],
]

/**
 * What a panel can be, and the distinction the whole screen turns on.
 *
 * `never` is a claim about the *metric*: no sample has ever arrived for it.
 * `grafana` is a claim about *this console*: the series is real and nothing
 * here can query it. Collapsing the two would put the six never-fired metrics
 * and the twenty-nine measured ones under one grey box, which is the bug this
 * screen exists in order not to have.
 */
interface PanelSpec {
  readonly title: string
  readonly metric: string
  readonly kind: 'never' | 'grafana'
  readonly sub: string
}

const PANELS: readonly PanelSpec[] = [
  {
    title: 'Bootstrap rows written',
    metric: 'usher.bootstrap.rows',
    kind: 'never',
    sub: 'GET /admin/bootstrap/status reports rows_seen and rows_written per run; this series does not.',
  },
  {
    title: 'Bootstrap batch duration',
    metric: 'usher.bootstrap.batch.duration',
    kind: 'never',
    sub: 'Emitted by the importer process, which exports to no collector on this deployment.',
  },
  {
    title: 'Bootstrap phase duration',
    metric: 'usher.bootstrap.phase.duration',
    kind: 'never',
    sub: 'Emitted by the importer process, which exports to no collector on this deployment.',
  },
  {
    title: 'Bootstrap failures',
    metric: 'usher.bootstrap.failures',
    kind: 'never',
    sub: 'A failed import run is still visible on Bootstrap, from the route rather than from this series.',
  },
  {
    title: 'Curated rows generated',
    metric: 'usher.curation.rows',
    kind: 'never',
    sub: 'The curated provider has never run here, so the counter has never been incremented.',
  },
  {
    title: 'Curated cards dropped',
    metric: 'usher.curation.dropped',
    kind: 'never',
    sub: 'The curated provider has never run here, so the counter has never been incremented.',
  },
  {
    title: 'Parked jobs',
    metric: 'usher.jobs.parked',
    kind: 'grafana',
    sub: 'Measured, and no route here serves the number — job introspection is REQUIRES BACKEND WORK.',
  },
  {
    title: 'HTTP latency by route',
    metric: 'http.server.duration',
    kind: 'grafana',
    sub: 'Measured. No usher. prefix: it is the instrumentor’s own name, over the route template.',
  },
  {
    title: 'SSE connections',
    metric: 'usher.sse.connections',
    kind: 'grafana',
    sub: 'Measured. Aggregate across instances — every restart mints a new one.',
  },
]

/** The push panel is the tenth, and the one number a route here really serves. */
const PUSH_METRIC = 'usher.source.push.connected'

const PANEL_COUNT = PANELS.length + 1

export default function Insights(): ReactElement {
  const { grafanaUrl } = useRuntimeConfig()
  const [tab, setTab] = useState('panels')

  const readiness = useReadiness()
  const sources = useSources()

  /**
   * `lanes.push` names the sources whose push lane is up, and it arrives on the
   * 503 as well as the 200 — `/health/ready` is one of two routes exempt from
   * the problem envelope precisely so a degraded deployment still reports which
   * lane is down. So a degraded read is a degraded *panel*, not an empty one.
   */
  const readinessDocument = readiness.data ?? readinessFromError(readiness.error)
  const pushCapable = useMemo(
    () => (sources.data ?? []).filter((source) => source.supports_push).length,
    [sources.data],
  )
  const pushConnected = readinessDocument?.lanes.push.length ?? null
  const pushPending = readiness.isPending || sources.isPending
  const pushUnreadable = !pushPending && readinessDocument === null

  return (
    <>
      <OpsHeader
        title="Insights"
        subtitle="Native panels for the numbers an operator checks daily, and a marked link out to Grafana for everything else."
        actions={
          <>
            {/*
              Absent, not dead. `grafanaUrl` is `null` on a deployment that has
              not configured one and there is no anchor at all in that case —
              the sentence below the badges says so instead.
            */}
            {grafanaUrl !== null && (
              <Button
                as="a"
                size="sm"
                variant="secondary"
                href={grafanaUrl}
                target="_blank"
                rel="noreferrer noopener"
                iconLeft={<Icon name="external-link" size={16} />}
              >
                Open Grafana
              </Button>
            )}
          </>
        }
      />

      <div className="u-ops__body">
        <div className="flex flex-wrap items-center gap-3">
          <Badge tone="neutral" mono>
            35 metrics emitted
          </Badge>
          <Badge tone="neutral" icon={<Icon name="circle-dashed" size={16} />}>
            6 have never fired
          </Badge>
          <Badge tone="warn">0 dashboards, 0 alert rules exist</Badge>
        </div>

        {/* The embed-versus-link decision, argued where it applies. */}
        <p className="u-ins__note">
          Grafana opens in a new tab and never in an iframe: its own{' '}
          <span className="u-mono">frame-ancestors</span> policy refuses to be framed, its theme cannot be
          made to match this one, and a framed panel cannot link back into a trace here.
          {grafanaUrl === null
            ? ' Grafana is not configured on this deployment, so there is no link to it — set grafanaUrl in the console configuration and the link appears in the header.'
            : ' The link is in the header.'}
        </p>

        <Tabs
          id="insights"
          value={tab}
          onChange={setTab}
          tabs={[
            { value: 'panels', label: 'Daily numbers', count: PANEL_COUNT },
            { value: 'dashboards', label: 'Dashboards', count: DASHBOARDS.length },
            { value: 'alerts', label: 'Alerts', count: ALERTS.length },
          ]}
        >
          {tab === 'panels' && (
            /* An `OpsSection` rather than a bare div: every panel title is an
               `h3`, so without an `h2` above them the document jumps h1 → h3
               and the heading outline is wrong for anyone navigating by it. */
            <OpsSection
              title="Daily numbers"
              note="Every panel names its metric so the same series is findable in Grafana. Usher’s HTTP API serves no Prometheus query route, so a panel carries a number only where an admin route measures the same thing; the rest name the series and hand off."
            >
              <div className="u-ins__grid">
                {/*
                  The one panel with a real value on it, and `zero` rather than
                  `ok` when nothing is connected: a measured zero is data and
                  must not look like the six panels that never fired.
                */}
                <ChartPanel
                  title="Push connectivity"
                  metric={PUSH_METRIC}
                  loading={pushPending}
                  {...(pushConnected !== null
                    ? {
                        value: `${pushConnected} of ${pushCapable}`,
                        state: pushConnected === 0 ? ('zero' as const) : ('ok' as const),
                      }
                    : {})}
                  sub={
                    pushUnreadable
                      ? 'GET /health/ready did not answer with a readiness document, so this number is unknown rather than zero.'
                      : 'lanes.push from GET /health/ready, over the sources that support push from GET /admin/sources.'
                  }
                />

                {PANELS.map((panel) =>
                  panel.kind === 'never' ? (
                    <ChartPanel
                      key={panel.metric}
                      title={panel.title}
                      metric={panel.metric}
                      state="never"
                      sub={panel.sub}
                    />
                  ) : (
                    <ChartPanel
                      key={panel.metric}
                      title={panel.title}
                      metric={panel.metric}
                      sub={panel.sub}
                    />
                  ),
                )}
              </div>

              <p className="u-ins__fine">
                A dashed panel means no sample has ever arrived for that metric — not that the value is zero.
                A grey zero means the value was measured and it is zero. Those two states looked identical in
                the old client and that is the single most misleading thing a metrics screen can do.
              </p>

              <p className="u-ins__fine">
                No panel carries its own link into Grafana, because none of the five dashboards below exists
                to link to. A per-panel link would be a dead one.
              </p>
            </OpsSection>
          )}

          {tab === 'dashboards' && (
            <div className="flex flex-col gap-3">
              <p className="u-ins__note">
                Five dashboards are specified in prose and none have been built. They are named here so the
                gap is legible rather than invisible.
              </p>
              <ul className="u-ins__ledger">
                {DASHBOARDS.map(([name, what]) => (
                  <li className="u-ins__row" key={name}>
                    <Icon name="layout-dashboard" size={16} />
                    <span className="u-ins__body">
                      <span className="u-ins__name">{name}</span>
                      <span className="u-ins__what">{what}</span>
                    </span>
                    <Badge tone="warn" icon={<Icon name="circle-dashed" size={16} />}>
                      not built
                    </Badge>
                  </li>
                ))}
              </ul>
              <BackendWork routes="ship the five dashboards and seven alert rules as JSON in the compose stack">
                Provisioned dashboard JSON belongs in the repository next to the collector config, so a fresh
                deployment gets all five without a person clicking anything.
              </BackendWork>
            </div>
          )}

          {tab === 'alerts' && (
            <div className="flex flex-col gap-3">
              <ul className="u-ins__ledger">
                {ALERTS.map(([name, condition, tone]) => (
                  <li className="u-ins__row" key={name}>
                    <span className={tone === 'bad' ? 'u-ins__bell u-ins__bell--bad' : 'u-ins__bell'}>
                      <Icon name="bell" size={16} />
                    </span>
                    <span className="u-ins__body">
                      <span className="u-ins__name">{name}</span>
                      <span className="u-ins__cond">{condition}</span>
                    </span>
                    <Badge tone="neutral" icon={<Icon name="circle-dashed" size={16} />}>
                      never armed
                    </Badge>
                  </li>
                ))}
              </ul>
              <StateBlock kind="never" meta="0 alert rules provisioned">
                Seven alerts are specified and none exist. Until they do, this console is the only thing that
                will tell you an import stalled — which is why stall detection lives in the UI at all.
              </StateBlock>
              <BackendWork routes="ship the five dashboards and seven alert rules as JSON in the compose stack">
                Alert rules belong beside the dashboards, in the compose stack, for the same reason: an alert
                a person has to arm by hand is an alert that is off on every fresh deployment.
              </BackendWork>
            </div>
          )}
        </Tabs>

        {/*
          Outside the tabs on purpose. Somebody hunting a series reads this
          whichever tab they arrived on, and all three have cost time on this
          deployment already.
        */}
        <OpsSection
          title="Finding a series"
          note="Three measured label hazards. Each makes a query return the wrong thing rather than nothing, which is the harder failure to notice."
        >
          <ul className="u-ins__hazards">
            <li>
              <span className="u-mono">instance</span> — every process restart mints a new one, and ten or
              more dead series have already accumulated here. Aggregate it away, or a panel counts restarts
              rather than work.
            </li>
            <li>
              <span className="u-mono">source</span> — a UUID on{' '}
              <span className="u-mono">usher.ingest.items</span> and a human name on{' '}
              <span className="u-mono">usher.sync.run.duration</span>. Two vocabularies under one label name:
              never join the two series on it.
            </li>
            <li>
              Usher&apos;s container logs land in Loki as{' '}
              <span className="u-mono">{'{service_name="docker"}'}</span> rather than{' '}
              <span className="u-mono">{'{service_name="usher"}'}</span>, so a log query written from the
              service name returns nothing and reads as silence.
            </li>
          </ul>
        </OpsSection>
      </div>
    </>
  )
}
