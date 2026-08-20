import { useMemo, useState, type ChangeEvent, type ReactElement } from 'react'
import { Badge, DataTable, Icon, Input, Select, StateBlock, type Column } from '@/design-system'
import { readinessFromError, useReadiness } from '@/api'
import { useViewport } from '@/app/useViewport'
import { BackendWork, OpsHeader, OpsSection } from '@/app/shells/OperatorShell'
import { CONFIG, SETTING_COUNT, type SettingRow } from './Config.settings'
import './Config.css'

/**
 * Configuration — a read-only, searchable inspector over Usher's settings.
 *
 * **There is no settings editor and there must not be one.** The API exposes no
 * write path for any of these, and a disabled-looking form implies one exists
 * and is merely switched off. The screen says so once, prominently, instead.
 *
 * **REQUIRES BACKEND WORK, and the way this screen went is worth stating
 * plainly**: no route in `/openapi.json` returns the running configuration, so
 * the current-value column is honest about not having one. A few settings are
 * exceptions and each carries the read that proves it — `USHER_DATABASE_URL`
 * and `USHER_SECRET_KEY` are both *required* fields with no default, so a
 * process that answers `/health/ready` at all has both, and `checks.database`
 * proves the first is not merely present but working. `USHER_WORKER_ENABLED`
 * and `USHER_PUSH_ENABLED` are reported by `lanes` on the same document. Every
 * other row says **not served** rather than inventing a value, and a
 * `GET /admin/config` was not fabricated to fill the gap.
 *
 * **Secrets are `•••• set` or `not set` and never anything else** — no value,
 * no length, no prefix, not even to an operator on the LAN, because this
 * console is unauthenticated. The reference client printed the whole DSN in
 * this column.
 */
export default function Config(): ReactElement {
  const { phone } = useViewport()
  const [query, setQuery] = useState('')
  const [group, setGroup] = useState('all')

  /**
   * The one read this screen makes, and it is not a configuration route: it is
   * readiness, which reports two lane switches and proves two required settings
   * exist. A 503 carries the same document as a 200, so a degraded deployment
   * still answers these four.
   */
  const readiness = useReadiness()
  const document_ = readiness.data ?? readinessFromError(readiness.error)

  const rows = useMemo(() => CONFIG.map((row) => withObservation(row, document_)), [document_])

  const groups = useMemo(() => ['all', ...Array.from(new Set(CONFIG.map((row) => row.group)))], [])

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return rows.filter(
      (row) =>
        (group === 'all' || row.group === group) &&
        (needle === '' || `${row.key} ${row.about}`.toLowerCase().includes(needle)),
    )
  }, [rows, query, group])

  const columns: Column<SettingRow>[] = [
    { key: 'key', header: 'Key', mono: true },
    { key: 'group', header: 'Subsystem' },
    { key: 'current', header: 'Current', render: renderCurrent },
    { key: 'def', header: 'Default', render: renderDefault },
    {
      key: 'about',
      header: 'What it controls',
      render: (row) => <span className="u-cfg__about">{row.about}</span>,
    },
    { key: 'source', header: 'Source', render: renderSource },
  ]

  return (
    <>
      <OpsHeader
        title="Configuration"
        subtitle={`${SETTING_COUNT} settings, read once at startup. Read-only here, and read-only in the API.`}
      />

      <div className="u-ops__body">
        <div className="u-cfg__lock">
          <span className="u-cfg__lock-icon">
            <Icon name="lock" size={16} />
          </span>
          <span className="u-cfg__lock-body">
            Read-only. All {SETTING_COUNT} settings are environment variables read once at startup — nothing
            here can be changed over HTTP, and changing one means editing the environment and restarting the
            process. There is no editor on this screen and there is no write route behind one: a
            disabled-looking form would imply a path that does not exist.
          </span>
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <span className="u-cfg__search">
            <Input
              id="cfg-q"
              label="Find a setting"
              value={query}
              onChange={(event: ChangeEvent<HTMLInputElement>) => setQuery(event.target.value)}
              lead={<Icon name="search" size={16} />}
              placeholder="ef_search, tmdb, heartbeat…"
            />
          </span>
          <span className="u-cfg__group">
            <Select
              id="cfg-group"
              label="Subsystem"
              value={group}
              onChange={(event: ChangeEvent<HTMLSelectElement>) => setGroup(event.target.value)}
              options={groups.map((name) => ({
                value: name,
                label: name === 'all' ? 'All subsystems' : name,
              }))}
            />
          </span>
          <Badge tone="neutral" mono>
            {shown.length} of {SETTING_COUNT} shown
          </Badge>
        </div>

        {/*
          Where the values would be. `never`, not `empty`: nothing has ever been
          computed here because nothing serves it — that is a different fact
          from a route answering with nothing.
        */}
        <StateBlock
          kind="never"
          title="No route returns the running configuration"
          meta="/openapi.json declares 33 paths and none of them is a settings read"
        >
          So the Current column says what this console can prove and nothing more. A handful of rows carry a
          value, each naming the read behind it; every other row says not served rather than repeating its
          default and calling it the truth.
        </StateBlock>

        {shown.length === 0 ? (
          <StateBlock kind="empty" title="No setting matches" meta={`query: "${query}"`}>
            All {SETTING_COUNT} keys are searchable by name and by what they control. Try the subsystem filter
            instead.
          </StateBlock>
        ) : (
          <DataTable caption="Configuration" keyField="key" rows={shown} columns={columns} asCards={phone} />
        )}

        <BackendWork
          routes={`a read-only projection of Settings: ${SETTING_COUNT} keys with the value this process read, every SecretStr rendered as a boolean and never as a value or a length`}
        >
          Nothing in the API returns the configuration a running process holds, so an operator checking
          whether a change to <span className="u-mono">.env</span> actually reached the container has to read
          it out of Docker instead. A route would have to answer the key, the value, and whether the value
          came from the environment, the <span className="u-mono">.env</span> file or the field default — and
          it must answer a secret as set or not set, because a console reachable unauthenticated on the LAN is
          the wrong place to return one.
        </BackendWork>

        <OpsSection
          title="Measured defaults worth knowing"
          note="Several of these numbers are measurements rather than preferences, and the row carries the measurement where one exists."
        >
          <p className="u-cfg__note">
            <span className="u-mono">USHER_SEARCH_HNSW_EF_SEARCH</span> is 200 because recall@10 measured
            0.858 at 100, 0.917 at 200 and 0.967 at 400 — 400 costs more latency than the recall is worth on
            this hardware. <span className="u-mono">USHER_QUERY_EXPANSION_ENABLED</span> is off because
            turning it on moved MRR from 0.733 to 0.373. Both numbers came from the measurement scripts in the
            repository, not from a guess.
          </p>
          <p className="u-cfg__fine">
            Secrets show as set or not set and are never revealed, including to an operator on the LAN — this
            console is unauthenticated.
          </p>
          <p className="u-cfg__fine">
            All but two are read under the <span className="u-mono">USHER_</span> prefix. The two exceptions
            are read under OpenTelemetry&apos;s own names,{' '}
            <span className="u-mono">OTEL_EXPORTER_OTLP_ENDPOINT</span> and{' '}
            <span className="u-mono">OTEL_SERVICE_NAME</span>, so a search for{' '}
            <span className="u-mono">USHER_OTEL_</span> finds nothing and the variable that looks missing is
            spelled the other way.
          </p>
        </OpsSection>
      </div>
    </>
  )
}

/** What `/health/ready` proves about a setting, if anything. */
type Readiness = ReturnType<typeof readinessFromError>

function withObservation(row: SettingRow, readiness: Readiness): SettingRow {
  if (readiness === null) return row
  if (row.key === 'USHER_DATABASE_URL') {
    return { ...row, observed: { value: 'set', proof: 'checks.database on GET /health/ready' } }
  }
  if (row.key === 'USHER_SECRET_KEY') {
    // Required, `min_length=32`, no default: `Settings` refuses to construct
    // without it, so a process that answered at all has one. That is a proof
    // about presence and says nothing whatever about the value.
    return {
      ...row,
      observed: { value: 'set', proof: 'the process answered GET /health/ready' },
    }
  }
  if (row.key === 'USHER_WORKER_ENABLED') {
    return {
      ...row,
      observed: { value: String(readiness.lanes.worker), proof: 'lanes.worker' },
    }
  }
  // An empty `lanes.push` proves nothing — no source may support push — so the
  // observation is only made in the direction it is sound in.
  if (row.key === 'USHER_PUSH_ENABLED' && readiness.lanes.push.length > 0) {
    return { ...row, observed: { value: 'true', proof: 'lanes.push names a running lane' } }
  }
  return row
}

const NOT_SERVED = (
  <Badge tone="neutral" icon={<Icon name="circle-dashed" size={16} />}>
    not served
  </Badge>
)

/**
 * The Current cell.
 *
 * **A secret has exactly two renderings and neither is derived from a value.**
 * There is no code path here that can print one, a length of one, or a prefix
 * of one — the row model carries no value for a secret at all, which is the
 * only way to be sure.
 */
function renderCurrent(row: SettingRow): ReactElement {
  if (row.secret) {
    if (!row.observed) return NOT_SERVED
    return (
      <span className="u-cfg__observed">
        {row.observed.value === 'set' ? (
          <Badge tone="good" icon={<Icon name="lock" size={16} />}>
            •••• set
          </Badge>
        ) : (
          <Badge tone="neutral" icon={<Icon name="circle-dashed" size={16} />}>
            not set
          </Badge>
        )}
        {/* The read that proves it, exactly as `StateBlock`'s `meta` does
            elsewhere: a claim about a credential with no provenance is the one
            claim this screen cannot afford to make loosely. */}
        <span className="u-cfg__proof">{row.observed.proof}</span>
      </span>
    )
  }
  if (!row.observed) return NOT_SERVED
  return (
    <span className="u-cfg__observed">
      <span className="u-cfg__value">{row.observed.value}</span>
      <span className="u-cfg__proof">{row.observed.proof}</span>
    </span>
  )
}

function renderDefault(row: SettingRow): ReactElement {
  return (
    <span className="u-cfg__default">
      <span className="u-cfg__value">{row.def}</span>
      {row.measured && <Badge tone="neutral">measured</Badge>}
    </span>
  )
}

/**
 * Where the value comes from. One answer for every one of them: the environment the
 * process was started with, read once. There is no second layer — PRD 08's TOML
 * config file does not exist — so this column states a fact rather than
 * offering a choice.
 */
function renderSource(): ReactElement {
  return (
    <Badge tone="neutral" mono>
      container env
    </Badge>
  )
}
