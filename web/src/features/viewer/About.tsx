import type { ReactElement, ReactNode } from 'react'
import { Button, Icon, Skeleton, SkeletonRegion, StateBlock } from '@/design-system'
import { readinessFromError, useAttribution, useReadiness, type ReadinessResponse, type Schemas } from '@/api'
import { useRuntimeConfig } from '@/app/runtime-config-context'
import { useDevDrawer } from '@/app/dev-drawer-context'
import { Wordmark } from '@/app/Wordmark'
import { ScreenProblem } from '@/features/shared/NotFound'

type AttributionEntry = Schemas['AttributionEntry']

/**
 * About — the attribution surface, the server's version, and its readiness.
 *
 * **The four strings from `GET /meta/attribution` are a licence term, not a
 * design choice.** IMDb and TMDb both require their notice to be shown, and
 * Usher ships importers rather than data precisely so that stays true — so the
 * strings are rendered exactly as the API sends them, and the screen they are
 * on is reachable from the header at every width and from the phone tab bar.
 * Nothing here edits, shortens, reflows or translates them, and this screen is
 * not a footer.
 */
export default function About(): ReactElement {
  return (
    <div
      className="mx-auto flex w-full flex-col gap-8 px-4 py-8 tablet:px-6"
      style={{ maxWidth: 'var(--width-prose)' }}
    >
      <header className="flex flex-col gap-2">
        <h1>
          <span className="u-visually-hidden">About </span>
          <Wordmark size="lg" />
        </h1>
        <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-muted)' }}>
          A catalog in front of your media server. It does not store or stream anything.
        </span>
      </header>

      <ThisServer />
      <Attribution />

      <section className="flex flex-col gap-2">
        <span className="u-eyebrow">Licensing</span>
        <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
          MIT licensed, self-hosted, non-commercial. IMDb and TMDb both require separate licensing for
          commercial use, which is out of scope for this project.
        </span>
        <DeveloperDrawerLink />
      </section>
    </div>
  )
}

/* ------------------------------------------------------------------ server */

function ThisServer(): ReactElement {
  const { version } = useRuntimeConfig()
  const query = useReadiness()

  /**
   * **A 503 from `/health/ready` is information, not an error.**
   *
   * That route is one of exactly two exempt from Usher's RFC 9457 envelope, and
   * the reason shows up right here: the 503 carries the *same*
   * `ReadinessResponse` document as the 200 and names which check failed. So a
   * degraded deployment is a degraded render — the panel says which lane is
   * down — rather than a page that disappears into an error box. A 503 whose
   * body did not parse as a readiness document is a genuine failure and is the
   * `null` below.
   */
  const readiness: ReadinessResponse | null = query.data ?? readinessFromError(query.error)

  return (
    <section className="flex flex-col gap-3">
      <span className="u-eyebrow">This server</span>
      <dl
        className="grid items-baseline gap-x-4 gap-y-2"
        style={{ gridTemplateColumns: 'auto 1fr', font: 'var(--text-mono-sm)' }}
      >
        <Row label="version">
          {version === '' ? (
            <StateBlock kind="na">
              Not reported. This console was not served by Usher, so{' '}
              <span className="u-mono">/console/config.json</span> carried no version.
            </StateBlock>
          ) : (
            <span style={{ color: 'var(--text-primary)' }}>{version}</span>
          )}
        </Row>

        <Row label="readiness">
          {query.isPending && readiness === null ? (
            <SkeletonRegion busy label="Loading readiness …">
              <Skeleton shape="block" width={90} height={14} />
            </SkeletonRegion>
          ) : readiness === null ? (
            <span style={{ color: 'var(--text-muted)' }}>could not be read</span>
          ) : (
            <ReadinessWord readiness={readiness} />
          )}
        </Row>

        {readiness !== null && (
          <>
            <Row label="checks">
              <span style={{ color: 'var(--text-primary)' }}>
                database {word(readiness.checks.database)} · migrations {word(readiness.checks.migrations)}
              </span>
            </Row>
            <Row label="lanes">
              <span style={{ color: 'var(--text-primary)' }}>
                push {readiness.lanes.push.length === 0 ? 'none' : readiness.lanes.push.join(', ')} · worker{' '}
                {readiness.lanes.worker ? 'running' : 'not running'}
              </span>
            </Row>
          </>
        )}
      </dl>

      {readiness !== null && readiness.status !== 'ready' && <Degraded readiness={readiness} />}

      {readiness === null && !query.isPending && (
        <ScreenProblem
          error={query.error}
          instance="/health/ready"
          onRetry={() => query.refetch().then(() => undefined)}
        />
      )}

      {readiness !== null && (
        <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
          Lanes are reported and never gated on: readiness is computed from the checks alone, so a media
          server this process cannot reach does not make the catalog unready.
        </span>
      )}
    </section>
  )
}

/** Hue plus glyph plus word, never hue alone (patterns.md §12). */
function ReadinessWord({ readiness }: { readiness: ReadinessResponse }): ReactElement {
  const ready = readiness.status === 'ready'
  return (
    <span
      className="flex items-center gap-1.5"
      style={{ color: ready ? 'var(--good-text)' : 'var(--warn-text)' }}
    >
      <Icon name={ready ? 'check-circle' : 'alert-triangle'} size={16} />
      {/* The server's own word, printed rather than re-derived from the status code. */}
      {readiness.status}
    </span>
  )
}

function Degraded({ readiness }: { readiness: ReadinessResponse }): ReactElement {
  const failed: string[] = []
  if (!readiness.checks.database) failed.push('The database check is failing.')
  if (!readiness.checks.migrations) failed.push('The migrations check is failing.')

  return (
    <div
      className="flex flex-col gap-1 p-3"
      style={{
        border: '1px solid var(--warn-border)',
        background: 'var(--warn-quiet)',
        borderRadius: 'var(--radius-card)',
        font: 'var(--text-body-sm)',
        color: 'var(--text-secondary)',
      }}
    >
      <span>
        <strong style={{ color: 'var(--warn-text)', font: 'var(--text-label)' }}>Running degraded. </strong>
        {failed.length === 0
          ? 'Every check passed and the server still reported itself degraded.'
          : failed.join(' ')}{' '}
        Browsing and search work; anything that writes may not.
      </span>
      {!readiness.lanes.worker && (
        <span>No worker lane is running in this process, so queued jobs will not be picked up.</span>
      )}
      {readiness.lanes.push.length === 0 && (
        <span>No source push lane is running, so changes on a media server arrive only on a sync.</span>
      )}
    </div>
  )
}

function word(passing: boolean): string {
  return passing ? 'ok' : 'failing'
}

function Row({ label, children }: { label: string; children: ReactNode }): ReactElement {
  return (
    <>
      <dt style={{ color: 'var(--text-muted)' }}>{label}</dt>
      <dd className="m-0">{children}</dd>
    </>
  )
}

/* ------------------------------------------------------------- attribution */

function Attribution(): ReactElement {
  const query = useAttribution()

  return (
    <section className="flex flex-col gap-4">
      <div>
        <span className="u-eyebrow mb-1 block">Attribution</span>
        <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
          Usher ships importers, never data. Every title here was built from these four sources, and their
          notices are reproduced exactly as required.
        </span>
      </div>

      {query.isPending ? (
        <SkeletonRegion busy label="Loading the attribution notices …" className="flex flex-col gap-4">
          <Skeleton shape="text" lines={2} />
          <Skeleton shape="text" lines={2} />
        </SkeletonRegion>
      ) : query.isError ? (
        <ScreenProblem
          error={query.error}
          instance="/meta/attribution"
          onRetry={() => query.refetch().then(() => undefined)}
        />
      ) : query.data.length === 0 ? (
        <StateBlock kind="empty" title="No notices were returned" meta="/meta/attribution: []">
          The route answered and the list was empty. Usher is required to carry these strings, so an empty
          list is a deployment to look at rather than a screen with nothing on it.
        </StateBlock>
      ) : (
        query.data.map((entry) => <Notice key={entry.source} entry={entry} />)
      )}

      <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
        Strings come from <span className="u-mono">/meta/attribution</span> and are never edited, shortened or
        translated in the client.
      </span>
    </section>
  )
}

function Notice({ entry }: { entry: AttributionEntry }): ReactElement {
  return (
    <div
      className="flex items-start gap-3 p-4"
      style={{
        border: '1px solid var(--border-default)',
        borderRadius: 'var(--radius-card)',
        background: 'var(--bg-surface)',
      }}
    >
      {isTmdb(entry.source) && <TmdbLogoSlot />}
      <span className="flex flex-col gap-1">
        <span style={{ font: 'var(--text-label)', color: 'var(--text-primary)' }}>{entry.source}</span>
        {/* Verbatim. One text node, no interpolation, no truncation. */}
        <span style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)', textWrap: 'pretty' }}>
          {entry.text}
        </span>
      </span>
    </div>
  )
}

function isTmdb(source: string): boolean {
  return source.trim().toLowerCase() === 'tmdb'
}

/**
 * A marked empty slot, and deliberately not a substitute mark.
 *
 * TMDb's terms require **their** logo beside their disclaimer, and the asset
 * has to be the official one from TMDb's brand page — it is the one thing in
 * this product a design system cannot originate. Drawing something else here,
 * or drawing nothing, would both be worse than saying what is missing: one
 * breaches the terms quietly and the other makes the omission invisible to
 * whoever ships this. So the slot states its own requirement, in the layout the
 * real mark will occupy, and swapping the official SVG in is the only change.
 */
function TmdbLogoSlot(): ReactElement {
  return (
    <span
      className="inline-flex flex-none items-center justify-center text-center"
      style={{
        width: 108,
        minHeight: 34,
        border: '1px dashed var(--border-control)',
        borderRadius: 'var(--radius-sm)',
        font: 'var(--text-label-sm)',
        color: 'var(--text-muted)',
        lineHeight: 1.2,
      }}
    >
      official TMDb logo required
    </span>
  )
}

/* ---------------------------------------------------------------- licensing */

/**
 * The kit's second control here was a "Documentation" link with no destination.
 * A dead link is worse than an absent one, and there is no documentation URL
 * this deployment knows, so the drawer is the only control that ships.
 */
function DeveloperDrawerLink(): ReactElement {
  const drawer = useDevDrawer()
  return (
    <span className="mt-2 inline-flex gap-2">
      <Button variant="ghost" size="sm" iconLeft={<Icon name="terminal" size={16} />} onClick={drawer.toggle}>
        Developer drawer
      </Button>
    </span>
  )
}
