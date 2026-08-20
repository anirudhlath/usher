/**
 * Sources — the connected media servers, their probe results, and the two
 * things about them that no route can do yet.
 *
 * The rules this screen exists to keep:
 *
 * · **Tri-state, never a bare boolean.** `reachable`, `authenticated`,
 *   `push_available` and `is_administrator` are yes / no / **unknown**. "We have
 *   not asked" is a different fact from "we asked and it said no", and the
 *   shell's `Tri` is what keeps them apart.
 * · **A probe is an action, not a page load.** `GET /admin/sources/{id}/status`
 *   builds an adapter and calls `verify()` against a real Emby at 1–5 s a call,
 *   so it fires on a click and on nothing else. Until it has, every probe field
 *   reads "unknown" and says why.
 * · **`is_administrator: true` is a risk surface, not a success** (§13). Warn
 *   tone, and the sentence.
 * · **`device_id` is deliberately visible**, with the reason attached: it is
 *   how you find and revoke Usher's session in Emby's own dashboard.
 * · **Credentials are write-only.** The password field states where the value
 *   goes; the API never returns it and nothing here reads it back into markup.
 * · **Durations are measured, not estimated.** No route reports how long a
 *   walk of this library took, so every duration fact is `NOT_MEASURED` rather
 *   than an invented range.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import {
  Badge,
  Button,
  ConfirmDialog,
  DataTable,
  Icon,
  Input,
  NOT_MEASURED,
  Problem,
  Select,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  type Column,
  type ConfirmFact,
  type ProblemDocument,
} from '@/design-system'
import { BackendWork, OpsHeader, OpsSection, Tri } from '@/app/shells/OperatorShell'
import { ROUTES } from '@/app/routes'
import { useViewport } from '@/app/useViewport'
import { useToasts } from '@/patterns'
import { useProblemTrace } from '@/features/shared/trace'
import {
  UsherProblem,
  fieldErrors,
  useCreateSource,
  useDeleteSource,
  useSourceStatus,
  useSources,
  useSyncSource,
  type SourceResponse,
  type SyncKind,
} from '@/api'

/* ------------------------------------------------------------------ shared */

/** An `UsherProblem` as the design system's document. Spread, never an explicit `undefined`. */
function problemOf(error: unknown): ProblemDocument {
  if (error instanceof UsherProblem) {
    return {
      status: error.status,
      detail: error.detail,
      ...(error.knownCode ? { code: error.knownCode } : {}),
      ...(error.instance ? { instance: error.instance } : {}),
      ...(error.retryAfter === null ? {} : { retry_after: error.retryAfter }),
    }
  }
  return { status: 0, detail: String(error) }
}

/**
 * `errors[].msg` for one field, verbatim, as props to spread.
 *
 * A 422 carries `errors[].loc` — `['body', 'base_url']` — and the message is
 * the server's own words, never reworded and never parsed. Returned as an
 * object to spread because `exactOptionalPropertyTypes` makes
 * `error={undefined}` a different thing from an absent `error`.
 */
function errorProps(failure: unknown, field: string): { error: string } | Record<string, never> {
  if (!(failure instanceof UsherProblem)) return {}
  const message = fieldErrors(failure.errors).find((one) => one.field.endsWith(field))?.message
  return message === undefined ? {} : { error: message }
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/* ------------------------------------------------------------------ fields */

function Field({ label, mono, children }: { label: string; mono?: boolean; children: ReactNode }) {
  return (
    <div
      className="flex gap-4"
      style={{ padding: 'var(--space-2x) 0', borderTop: '1px solid var(--border-subtle)' }}
    >
      <span
        className="flex-none"
        style={{ font: 'var(--text-label-sm)', color: 'var(--text-muted)', width: 148 }}
      >
        {label}
      </span>
      <span
        className="min-w-0"
        style={{
          font: mono ? 'var(--text-mono-xs)' : 'var(--text-body-sm)',
          color: 'var(--text-primary)',
          wordBreak: 'break-all',
        }}
      >
        {children}
      </span>
    </div>
  )
}

/* ------------------------------------------------------------------ wizard */

interface WizardProps {
  onClose: () => void
  onConnected: (source: SourceResponse, jobKey: string) => void
}

/**
 * Three steps, and the third one is a test step in position rather than in
 * capability: **there is no route that can probe an unsaved source.**
 * `GET /admin/sources/{id}/status` needs an id, and an id only exists after
 * `POST /admin/sources`. So step 3 states that plainly with the never-computed
 * treatment rather than showing four green checks it did not earn.
 */
function ConnectWizard({ onClose, onConnected }: WizardProps) {
  const traceOf = useProblemTrace()
  const [step, setStep] = useState(1)
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const create = useCreateSource()
  const sync = useSyncSource()
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const scrimRef = useRef<HTMLDivElement | null>(null)
  const headingRef = useRef<HTMLHeadingElement | null>(null)

  useEffect(() => {
    headingRef.current?.focus()
  }, [])

  /** Esc and the scrim click cancel; Tab cycles inside. Bound to the nodes, as `ConfirmDialog` does. */
  useEffect(() => {
    const dialog = dialogRef.current
    const scrim = scrimRef.current
    if (!dialog || !scrim) return undefined

    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const nodes = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE))
      const first = nodes[0]
      const last = nodes[nodes.length - 1]
      if (!first || !last) return
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    const onMouseDown = (event: MouseEvent): void => {
      if (event.target === scrim) onClose()
    }

    dialog.addEventListener('keydown', onKeyDown)
    scrim.addEventListener('mousedown', onMouseDown)
    return () => {
      dialog.removeEventListener('keydown', onKeyDown)
      scrim.removeEventListener('mousedown', onMouseDown)
    }
  }, [onClose])

  const connect = (): void => {
    void create
      .mutateAsync({ kind: 'emby', name, base_url: baseUrl, username, password })
      .then((source) =>
        sync.mutateAsync({ id: source.id, kind: 'full' }).then((queued) => onConnected(source, queued.key)),
      )
      .catch(() => {
        // The rejection is rendered from `create.error` / `sync.error` below.
        // Nothing is swallowed and nothing is retried on the operator's behalf.
      })
  }

  const failure = create.error ?? sync.error
  const pending = create.isPending || sync.isPending

  return (
    <div className="u-scrim" ref={scrimRef}>
      <div
        className="u-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="connect-source-title"
        ref={dialogRef}
      >
        <div className="flex items-center gap-2">
          <h2 className="u-dialog__title" id="connect-source-title" tabIndex={-1} ref={headingRef}>
            Connect a media server
          </h2>
          <span className="u-mono ml-auto" style={{ color: 'var(--text-muted)' }}>
            step {step} of 3
          </span>
        </div>

        {step === 1 && (
          <div className="u-dialog__body">
            <Select
              id="source-kind"
              label="Kind"
              defaultValue="emby"
              options={[{ value: 'emby', label: 'Emby' }]}
              hint="Emby is the only source type today. The adapter interface is the extension point."
            />
            <Input
              id="source-name"
              label="Name"
              placeholder="Living Room"
              value={name}
              onChange={(event) => setName(event.target.value)}
              hint="Shown next to every copy this server holds."
              {...errorProps(failure, 'name')}
            />
            <Input
              id="source-url"
              label="Base URL"
              mono
              placeholder="http://emby.lan:8096"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              hint="Reachable from the Usher container, not from your browser."
              {...errorProps(failure, 'base_url')}
            />
          </div>
        )}

        {step === 2 && (
          <div className="u-dialog__body">
            <Input
              id="source-username"
              label="Username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              {...errorProps(failure, 'username')}
            />
            {/* Write-only. The value is never echoed into a hint, a label, a
                title or an aria attribute, and the API never returns it. */}
            <Input
              id="source-password"
              label="Password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              hint="Sent once, stored encrypted on the server, and never returned by the API."
              {...errorProps(failure, 'password')}
            />
            <div
              className="flex gap-2"
              style={{
                padding: 'var(--space-3)',
                border: '1px solid var(--warn-border)',
                background: 'var(--warn-quiet)',
                borderRadius: 'var(--radius-md)',
              }}
            >
              <span className="flex-none" style={{ color: 'var(--warn-text)' }}>
                <Icon name="alert-triangle" size={16} />
              </span>
              <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-secondary)' }}>
                If this account is an Emby administrator, Usher will hold an admin session. It only ever reads
                and writes watch state, but the session is as privileged as the account.
              </span>
            </div>
          </div>
        )}

        {step === 3 && (
          <div className="u-dialog__body">
            <StateBlock
              kind="never"
              title="Not tested yet"
              meta="GET /admin/sources/{id}/status needs an id — this source does not have one yet"
            >
              A probe is a real round trip made through the saved source's own adapter, so it cannot run
              before the source exists. Connect, then press <strong>Probe now</strong> — the result lands on
              this screen as reachable / authenticated / push_available, each of them yes, no or unknown.
            </StateBlock>
            <dl className="u-dialog__facts">
              <dt className="u-dialog__k">next</dt>
              <dd className="u-dialog__v">the source is created and a full sync is queued</dd>
              <dt className="u-dialog__k">walks</dt>
              <dd className="u-dialog__v">every item the source reports</dd>
              <dt className="u-dialog__k">measured</dt>
              <dd className="u-dialog__v u-dialog__v--unmeasured">{NOT_MEASURED}</dd>
              <dt className="u-dialog__k">during</dt>
              <dd className="u-dialog__v">the catalog stays browsable</dd>
              <dt className="u-dialog__k">returns</dt>
              <dd className="u-dialog__v">202 with a key you cannot yet query</dd>
            </dl>
            {failure !== null && (
              <Problem
                scale="panel"
                problem={problemOf(failure)}
                {...traceOf(failure)}
                icon={<Icon name="x-circle" size={20} />}
              />
            )}
          </div>
        )}

        <div className="u-dialog__foot">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          {step > 1 && (
            <Button variant="secondary" onClick={() => setStep(step - 1)}>
              Back
            </Button>
          )}
          <Button
            variant="primary"
            loading={pending}
            loadingLabel="Connecting…"
            onClick={() => (step < 3 ? setStep(step + 1) : connect())}
          >
            {step === 1 ? 'Next' : step === 2 ? 'Review' : 'Connect and start the first sync'}
          </Button>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ screen */

type Pending = { kind: 'sync'; sync: SyncKind } | { kind: 'delete' }

export default function Sources() {
  const { phone } = useViewport()
  const toasts = useToasts()
  const traceOf = useProblemTrace()

  const sources = useSources()
  const rows = sources.data ?? []

  const [selectedId, setSelectedId] = useState<string | undefined>(undefined)
  const selected = rows.find((row) => row.id === selectedId) ?? rows[0]

  /**
   * The probe is keyed on the source it was asked for, so switching rows shows
   * "unknown" again instead of the previous row's answer. `useSourceStatus` is
   * disabled while this is `undefined`, which is what keeps the round trip off
   * the page load.
   */
  const [probeFor, setProbeFor] = useState<string | undefined>(undefined)
  const statusQuery = useSourceStatus(probeFor)
  const probed = selected !== undefined && probeFor === selected.id
  const probe = probed ? statusQuery.data : undefined
  const probeError = probed ? statusQuery.error : null

  const sync = useSyncSource()
  const remove = useDeleteSource()
  const [pending, setPending] = useState<Pending | null>(null)
  const [wizard, setWizard] = useState(false)

  const runProbe = (): void => {
    if (!selected) return
    if (probeFor === selected.id) void statusQuery.refetch()
    else setProbeFor(selected.id)
  }

  const queueSync = (kind: SyncKind, source: SourceResponse): void => {
    // `mutate` rather than `mutateAsync`: a rejection here is rendered from
    // `sync.error` below, and an un-awaited rejected promise is not an error
    // report.
    sync.mutate(
      { id: source.id, kind },
      {
        onSuccess: (queued) => {
          // 202. The word is "Queued", the key is the only record, and the
          // destination is honest about where evidence will appear even though
          // that surface is itself REQUIRES BACKEND WORK.
          toasts.receipt({
            title: `Queued a ${kind} sync of ${source.name}`,
            detail:
              kind === 'full'
                ? 'A full walk of the library. The server accepted it with a 202 and nothing else; no route can look this key up.'
                : 'Items changed since the last cursor. The server accepted it with a 202 and nothing else; no route can look this key up.',
            jobKey: queued.key,
            destination: { label: 'Watch it on Pipeline', to: ROUTES.pipeline },
          })
        },
      },
    )
  }

  const columns: Column<SourceResponse>[] = [
    { key: 'name', header: 'Name' },
    { key: 'kind', header: 'Kind', mono: true },
    { key: 'base_url', header: 'Base URL', mono: true },
    {
      key: 'supports_push',
      header: 'Push',
      render: (row) => <Tri value={row.supports_push} labels={['yes', 'no', 'unknown']} />,
    },
    {
      key: 'enabled',
      header: 'Enabled',
      render: (row) => <Tri value={row.enabled} labels={['yes', 'no', 'unknown']} />,
    },
    { key: 'created_at', header: 'Added', mono: true, render: (row) => row.created_at.slice(0, 10) },
  ]

  const syncFacts = (kind: SyncKind): ConfirmFact[] => [
    {
      label: 'walks',
      value: kind === 'full' ? 'every item the source reports' : 'items changed since the last cursor',
    },
    // No route reports how long the last walk took, so this is stated rather
    // than estimated. An invented range is a lie with a unit on it.
    { label: 'measured', value: NOT_MEASURED },
    { label: 'writes', value: 'media items, matches and watch state' },
    { label: 'during', value: 'the catalog stays browsable' },
    { label: 'returns', value: '202 with a key you cannot yet query' },
  ]

  const deleteFacts: ConfirmFact[] = [
    { label: 'removes', value: 'this source and every copy it made available' },
    { label: 'keeps', value: 'watch state — it survives a source deletion' },
    { label: 'measured', value: NOT_MEASURED },
    { label: 'reversible', value: 'no — availability returns only after re-adding and re-syncing' },
  ]

  return (
    <>
      <OpsHeader
        title="Sources"
        subtitle="The media servers Usher reads from. Probe results are three-valued, and a probe is a real round trip you ask for."
        actions={
          <Button
            variant="primary"
            size="sm"
            iconLeft={<Icon name="plus" size={16} />}
            onClick={() => setWizard(true)}
          >
            Connect a media server
          </Button>
        }
      />

      <div className="u-ops__body">
        {sources.isPending && (
          <SkeletonRegion busy label="Loading sources …">
            <Skeleton shape="table" count={4} />
          </SkeletonRegion>
        )}

        {sources.isError && (
          <Problem
            scale="panel"
            problem={problemOf(sources.error)}
            {...traceOf(sources.error)}
            onRetry={() => void sources.refetch()}
            icon={<Icon name="server-off" size={20} />}
          />
        )}

        {sources.data && rows.length === 0 && (
          <div style={{ maxWidth: 'var(--width-prose)' }}>
            <h2 style={{ font: 'var(--text-title)', color: 'var(--text-primary)' }}>
              No media server is connected
            </h2>
            <p
              style={{
                font: 'var(--text-body)',
                color: 'var(--text-secondary)',
                marginTop: 'var(--space-3)',
              }}
            >
              The catalog works without one — every title already imported is browsable and searchable right
              now. A source is what makes a title playable and what gives you an &ldquo;owned&rdquo; filter
              that means anything.
            </p>
            <div style={{ marginTop: 'var(--space-5)' }}>
              <Button
                variant="primary"
                iconLeft={<Icon name="plus" size={16} />}
                onClick={() => setWizard(true)}
              >
                Connect a media server
              </Button>
            </div>
          </div>
        )}

        {rows.length > 0 && (
          <DataTable
            caption="Configured sources"
            keyField="id"
            rows={rows}
            columns={columns}
            asCards={phone}
            {...(selected ? { selectedId: selected.id } : {})}
            onRowClick={(row) => {
              setSelectedId(row.id)
              // A probe belongs to the row it was run for; selecting another
              // row must not inherit its answer.
              setProbeFor(undefined)
            }}
          />
        )}

        {selected && (
          <OpsSection
            title={selected.name}
            note="A probe is a real round trip to the media server — it costs 1–5 seconds, so it is a button, not a poll."
            action={
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="secondary"
                  loading={probed && statusQuery.isFetching}
                  loadingLabel="Probing…"
                  iconLeft={<Icon name="activity" size={16} />}
                  onClick={runProbe}
                >
                  Probe now
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  iconLeft={<Icon name="refresh-cw" size={16} />}
                  onClick={() => setPending({ kind: 'sync', sync: 'full' })}
                >
                  Full sync
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setPending({ kind: 'sync', sync: 'delta' })}>
                  Delta sync
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  iconLeft={<Icon name="trash-2" size={16} />}
                  onClick={() => setPending({ kind: 'delete' })}
                >
                  Delete
                </Button>
              </div>
            }
          >
            {probeError !== null && (
              <Problem
                scale="panel"
                problem={problemOf(probeError)}
                {...traceOf(probeError)}
                onRetry={() => void statusQuery.refetch()}
                icon={<Icon name="server-off" size={20} />}
              />
            )}
            {sync.error !== null && (
              <Problem
                scale="panel"
                problem={problemOf(sync.error)}
                {...traceOf(sync.error)}
                icon={<Icon name="x-circle" size={20} />}
              />
            )}
            {remove.error !== null && (
              <Problem
                scale="panel"
                problem={problemOf(remove.error)}
                {...traceOf(remove.error)}
                icon={<Icon name="x-circle" size={20} />}
              />
            )}

            <div className="flex flex-wrap items-start gap-6">
              <div
                className="min-w-0 flex-1 basis-[380px]"
                style={{
                  border: '1px solid var(--border-default)',
                  borderRadius: 'var(--radius-card)',
                  background: 'var(--bg-surface)',
                  padding: 'var(--space-4)',
                }}
              >
                <span className="u-eyebrow">Last probe</span>

                {probe === undefined && probeError === null && (
                  <div style={{ marginTop: 'var(--space-2x)' }}>
                    <StateBlock kind="never" title="Not probed" meta="status: not requested">
                      Nothing has asked this server anything yet. Every field below reads
                      &ldquo;unknown&rdquo; because we have not asked, which is a different fact from the
                      server saying no.
                    </StateBlock>
                  </div>
                )}

                <Field label="reachable">
                  <Tri value={probe?.reachable} labels={['yes', 'no', 'unknown']} />
                </Field>
                <Field label="authenticated">
                  <Tri value={probe?.authenticated} labels={['yes', 'no', 'unknown']} />
                </Field>
                <Field label="push_available">
                  <Tri value={probe?.push_available} labels={['yes', 'no', 'unknown']} />
                </Field>
                <Field label="is_administrator">
                  {/* The one probe field `Tri` is deliberately *not* used for.
                      `Tri` reads `true` as good, and a green tick here would
                      celebrate the risk: an administrator token rides in every
                      playback URL and opens the long-lived push socket. Warn
                      tone, the fixed warn glyph, and the sentence (§13). */}
                  <span className="flex flex-wrap items-center gap-2">
                    {probe?.is_administrator === true ? (
                      <>
                        <Badge tone="warn" icon={<Icon name="alert-triangle" />}>
                          yes
                        </Badge>
                        <span style={{ font: 'var(--text-body-xs)', color: 'var(--warn-text)' }}>
                          Usher holds an administrator session on this server.
                        </span>
                      </>
                    ) : probe?.is_administrator === false ? (
                      <Badge tone="good">no</Badge>
                    ) : (
                      <Tri value={undefined} labels={['yes', 'no', 'unknown']} />
                    )}
                  </span>
                </Field>
                <Field label="server_version" mono>
                  {probe ? (probe.server_version ?? '— never answered') : '— not asked'}
                </Field>
                <Field label="detail">
                  {/* The adapter's own status line, shown verbatim and never parsed. */}
                  {probe ? (probe.detail ?? '— the adapter reported no status line.') : '— not asked'}
                </Field>
                <Field label="device_id" mono>
                  {selected.device_id}
                  <span
                    className="block"
                    style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)', marginTop: 4 }}
                  >
                    Find this in Emby&rsquo;s own dashboard under Devices to revoke Usher&rsquo;s session.
                  </span>
                </Field>
              </div>

              <div className="flex min-w-0 flex-1 basis-[320px] flex-col gap-3">
                <OpsSection
                  title="Activity"
                  note="Per-attempt counters, cursors and errors are written for every run."
                >
                  <BackendWork routes="GET /admin/sources/{id}/runs?limit=&cursor=">
                    This is where the last ten syncs belong: kind, started, items seen / matched / unmatched,
                    duration, and the error if it stopped. The data is in{' '}
                    <span className="u-mono">sync_runs</span>; only the CLI can read it.
                  </BackendWork>
                </OpsSection>

                <OpsSection title="Enable and disable">
                  <BackendWork routes="PATCH /admin/sources/{id}">
                    A source can be created and deleted but not paused. Sync refuses with a 409 when a source
                    is disabled, and nothing over HTTP can disable one — the{' '}
                    <span className="u-mono">enabled</span> column above is readable and not writable.
                  </BackendWork>
                </OpsSection>
              </div>
            </div>
          </OpsSection>
        )}
      </div>

      {selected && pending?.kind === 'sync' && (
        <ConfirmDialog
          open
          title={`Run a ${pending.sync} sync of ${selected.name}?`}
          facts={syncFacts(pending.sync)}
          confirmLabel={`Queue ${pending.sync} sync`}
          loading={sync.isPending}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            setPending(null)
            queueSync(pending.sync, selected)
          }}
        >
          {pending.sync === 'full'
            ? 'A full sync re-walks the whole library rather than asking for changes since the last cursor. Use it after a library move or when watch state has drifted.'
            : 'A delta sync asks the source what changed since the stored cursor. It is the cheap one, and it is what the scheduled walk already runs.'}
        </ConfirmDialog>
      )}

      {selected && pending?.kind === 'delete' && (
        <ConfirmDialog
          open
          destructive
          requireTyped={selected.name}
          title={`Delete ${selected.name}?`}
          facts={deleteFacts}
          confirmLabel="Delete this source"
          loading={remove.isPending}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            const doomed = selected
            setPending(null)
            remove.mutate(doomed.id, {
              onSuccess: () => {
                setSelectedId(undefined)
                setProbeFor(undefined)
                // 204, not 202: this one really is done, so it is a notice
                // rather than a receipt. There is no key to keep.
                toasts.notice({
                  tone: 'good',
                  title: `Removed ${doomed.name}`,
                  detail: 'Watch state survives; availability does not.',
                })
              },
            })
          }}
        >
          Deleting a source is the only irreversible action in this console. Watch state survives it; every
          copy this server made available disappears from the catalog until it is re-added and re-synced.
        </ConfirmDialog>
      )}

      {wizard && (
        <ConnectWizard
          onClose={() => setWizard(false)}
          onConnected={(source, jobKey) => {
            setWizard(false)
            setSelectedId(source.id)
            setProbeFor(undefined)
            toasts.receipt({
              title: `Queued a full sync of ${source.name}`,
              detail:
                'The source is saved. The server accepted the first sync with a 202 and nothing else; no route can look this key up.',
              jobKey,
              destination: { label: 'Watch it on Pipeline', to: ROUTES.pipeline },
            })
          }}
        />
      )}
    </>
  )
}
