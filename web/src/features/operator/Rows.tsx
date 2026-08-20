import { useState, type ReactNode } from 'react'
import {
  Badge,
  Button,
  ConfirmDialog,
  Icon,
  NOT_MEASURED,
  Problem,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  Switch,
  type ProblemDocument,
} from '@/design-system'
import { OpsHeader, OpsSection } from '@/app/shells/OperatorShell'
import { ROUTES } from '@/app/routes'
import {
  UsherProblem,
  fieldErrors,
  useHome,
  useRegenerateRows,
  useRowProviders,
  useSetRowProvider,
  type RowProviderResponse,
} from '@/api'
import { useToasts } from '@/patterns'
import { useProblemTrace } from '@/features/shared/trace'

/**
 * Recommendations — the ten registered row providers and whether each composes.
 *
 * Three things this screen exists to say, none of which the raw
 * `{slug, enabled}` pair says on its own:
 *
 * · **A slug is not a sentence.** `franchise` is a database value; `Switch`
 *   takes a `description` precisely so the plain-language explanation is bound
 *   to the control with `aria-describedby` (patterns.md §12).
 * · **Most of these slugs are prefixes.** `RowProvider.slug_prefix` is what this
 *   route returns, and a *row* built by one of them carries a suffix —
 *   `franchise-<collection>` is a family, not a literal slug — so one switch
 *   governs every row that provider can produce.
 * · **Off and inactive are different facts.** A provider that is switched off
 *   proposes nothing; a provider that is on and produced no row is inactive,
 *   which for a seasonal window in August is the correct outcome rather than a
 *   fault. Whether it has *ever* built one is not on the wire at all, and the
 *   screen says so rather than guessing.
 */
export default function Rows() {
  const traceOf = useProblemTrace()
  const providers = useRowProviders()
  const composed = useHome()
  const { mutate: setProvider } = useSetRowProvider()
  const { mutate: regenerate, isPending: regenerating } = useRegenerateRows()
  const { receipt, notice } = useToasts()
  const [confirming, setConfirming] = useState(false)

  const registry = providers.data ?? []
  const enabledCount = registry.filter((one) => one.enabled).length
  const composedRows = composed.data?.rows
  const inactive =
    composedRows === undefined
      ? null
      : registry.filter((one) => one.enabled && !producedRow(one.slug, composedRows)).length

  const toggle = (provider: RowProviderResponse, next: boolean) => {
    setProvider(
      { slug: provider.slug, enabled: next },
      {
        onSuccess: () => {
          // `PUT` answers 200 with the row it wrote, so this is a notice and not
          // a receipt: there is no queued job and no key to keep.
          notice({
            tone: 'good',
            title: `${next ? 'Enabled' : 'Disabled'} ${provider.slug}`,
            detail:
              'The composed home screen was cleared for every client on this deployment, not just for you.',
          })
        },
      },
    )
  }

  const queueRegeneration = () => {
    regenerate(undefined, {
      onSuccess: (queued) => {
        setConfirming(false)
        receipt({
          title: 'Queued a row regeneration',
          detail: `Queued as kind ${queued.kind}. The composed home cache was cleared for every client on this deployment.`,
          jobKey: queued.key,
          destination: { label: 'Watch it on Pipeline', to: ROUTES.pipeline },
        })
      },
      onError: () => setConfirming(false),
    })
  }

  return (
    <>
      <OpsHeader
        title="Recommendations"
        subtitle="The registered row providers and whether each composes. There is one household, so every toggle here is deployment-wide."
        actions={
          <Button
            size="sm"
            variant="secondary"
            iconLeft={<Icon name="refresh-cw" size={16} />}
            onClick={() => setConfirming(true)}
            disabled={providers.isError}
          >
            Regenerate rows
          </Button>
        }
      />
      <div className="u-ops__body">
        {providers.isError ? (
          <Problem
            problem={asProblem(providers.error)}
            {...traceOf(providers.error)}
            icon={<Icon name="x-circle" size={20} />}
            onRetry={() => {
              void providers.refetch()
            }}
          />
        ) : providers.isPending ? (
          <SkeletonRegion busy label="Loading the row providers …">
            <Skeleton shape="table" count={8} />
          </SkeletonRegion>
        ) : registry.length === 0 ? (
          <StateBlock kind="empty" meta="GET /admin/rows/providers → []">
            No provider is registered on this deployment. This list is the registry left-joined onto{' '}
            <span className="u-mono">row_provider_settings</span>, so an empty answer means the registry
            itself is empty — not that somebody turned everything off.
          </StateBlock>
        ) : (
          <>
            <div style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'center', flexWrap: 'wrap' }}>
              <Badge tone="neutral">
                {enabledCount} of {registry.length} enabled
              </Badge>
              {inactive !== null && (
                <Badge tone="neutral" icon={<Icon name="circle-dashed" />}>
                  {inactive} produced no row in the current composition
                </Badge>
              )}
            </div>

            <Notice>
              Toggling any provider clears the composed home screen for the whole deployment, not just for
              you. Most of these slugs are <strong>prefixes</strong>: the route returns{' '}
              <span className="u-mono">RowProvider.slug_prefix</span>, and a row built by one of them carries
              a suffix — <span className="u-mono">franchise-&lt;collection&gt;</span> is a family rather than
              a literal slug — so one switch governs every row that provider can produce. A provider that has
              built no row is <strong>inactive</strong>, not broken.
            </Notice>

            {/* patterns.md §2: the field that proves the claim is named, and the
                claim here is that there is no field. */}
            <StateBlock
              kind="never"
              title="Build history has never been on the wire"
              meta="GET /admin/rows/providers → {slug, enabled} · no built_at, no last_built"
            >
              Whether a provider has <em>ever</em> produced a row is not something any route answers. What is
              shown beside each switch is read from the current <span className="u-mono">GET /home</span>{' '}
              composition instead: a provider with no row in it is inactive right now — the correct outcome
              for a seasonal window in August — and that is a different fact from a provider that is switched
              off.
            </StateBlock>

            <OpsSection
              title="Providers"
              note="Every provider is enabled by registration in code; the settings table ships empty, so an untouched provider reads true."
            >
              <div
                style={{
                  border: '1px solid var(--border-default)',
                  borderRadius: 'var(--radius-card)',
                  background: 'var(--bg-surface)',
                }}
              >
                {registry.map((provider, at) => {
                  const copy = PROVIDER_COPY[provider.slug]
                  const built = composedRows === undefined ? null : producedRow(provider.slug, composedRows)
                  return (
                    <div
                      key={provider.slug}
                      style={{
                        display: 'flex',
                        gap: 'var(--space-4)',
                        alignItems: 'flex-start',
                        padding: 'var(--space-3)',
                        borderTop: at ? '1px solid var(--border-subtle)' : 'none',
                      }}
                    >
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <Switch
                          id={`row-provider-${provider.slug}`}
                          checked={provider.enabled}
                          onChange={(next) => toggle(provider, next)}
                          label={provider.slug}
                          description={describe(provider.slug, copy)}
                        />
                      </span>
                      <span
                        style={{
                          flex: 'none',
                          display: 'flex',
                          flexDirection: 'column',
                          alignItems: 'flex-end',
                          gap: 'var(--space-1)',
                          maxWidth: 220,
                          textAlign: 'right',
                        }}
                      >
                        {!provider.enabled ? (
                          <>
                            <Badge tone="neutral" icon={<Icon name="x" />}>
                              off
                            </Badge>
                            <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                              off — no row proposed
                            </span>
                          </>
                        ) : built === true ? (
                          <Badge tone="good">in the current home composition</Badge>
                        ) : built === false ? (
                          <>
                            <Badge tone="neutral" icon={<Icon name="circle-dashed" />}>
                              inactive
                            </Badge>
                            <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                              on, and it proposed no row in the current composition
                            </span>
                          </>
                        ) : (
                          <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
                            the composition has not been read
                          </span>
                        )}
                      </span>
                    </div>
                  )
                })}
              </div>
            </OpsSection>
          </>
        )}
      </div>

      <ConfirmDialog
        open={confirming}
        title="Regenerate every home row?"
        facts={[
          { label: 'affects', value: 'the whole deployment, not one person' },
          { label: 'clears', value: 'the composed home cache (30 s ETag)' },
          { label: 'rebuilds', value: `up to ${registry.length} providers, sequentially` },
          // patterns.md §5: a duration is measured or it says it was not. An
          // invented range is a lie with a unit on it.
          { label: 'measured', value: NOT_MEASURED },
          { label: 'returns', value: '202 with a key you cannot yet query' },
        ]}
        confirmLabel="Queue regeneration"
        loading={regenerating}
        onCancel={() => setConfirming(false)}
        onConfirm={queueRegeneration}
      >
        Rows are composed per request anyway; this only discards what is cached and forces the next request to
        rebuild from scratch. Every client on this deployment gets the rebuilt screen, because there is one
        household.
      </ConfirmDialog>
    </>
  )
}

interface ProviderCopy {
  /** Plain language, bound to the switch by `aria-describedby`. */
  readonly plain: string
  /** `slug_prefix` rather than a literal slug: one switch, many rows. */
  readonly family: boolean
}

/**
 * The ten providers the registry constructs, each in a sentence. `family` is
 * read off which of `_SLUG` / `_SLUG_PREFIX` the provider declares upstream —
 * the four literals are the ones that can only ever build one row.
 */
const PROVIDER_COPY: Record<string, ProviderCopy> = {
  'continue-watching': {
    plain: 'Titles you are part-way through. Always pinned first.',
    family: false,
  },
  'next-up': {
    plain: 'The next unplayed episode of a series you are mid-way through. Owned copies only.',
    family: false,
  },
  'recently-added': {
    plain: 'Added within the last 30 days. The only row that fires for a household that has watched nothing.',
    family: false,
  },
  rediscover: {
    plain: 'Played, and last played more than two years ago.',
    family: false,
  },
  'because-you-watched': {
    plain: 'Up to three seeds, from the precomputed neighbour table. One switch governs every seed row.',
    family: true,
  },
  franchise: {
    plain: 'Collections with at least two owned members and something unplayed left. Films only.',
    family: true,
  },
  'genre-affinity': {
    plain: 'Ranked by lift — your share of a genre divided by the library share, not raw count.',
    family: true,
  },
  seasonal: {
    plain: 'A calendar window. Silent for about 320 days a year.',
    family: true,
  },
  people: {
    plain: 'A person appearing in at least three titles you have engaged with.',
    family: true,
  },
  curated: {
    plain:
      "Last night's LLM-generated shelves. Measured on this deployment: 52 of 59 generated headings were the plain genre labels the prompt forbade.",
    family: true,
  },
}

/**
 * A slug this build has never heard of gets said so, rather than an invented
 * sentence. The registry is code and can grow; a console that guessed would be
 * describing a provider it has never seen.
 */
function describe(slug: string, copy: ProviderCopy | undefined): string {
  if (copy === undefined) {
    return `This console carries no plain-language description for ${slug}. It is a registered provider; the sentence that explains it has not been written here.`
  }
  return copy.family
    ? `${copy.plain} The slug is a prefix — one switch governs every row whose own slug starts with ${slug}-.`
    : copy.plain
}

/** A row's slug is the provider's prefix, optionally with a suffix after it. */
function producedRow(slug: string, rows: readonly { slug: string }[]): boolean {
  return rows.some((row) => row.slug === slug || row.slug.startsWith(`${slug}-`))
}

function Notice({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        display: 'flex',
        gap: 'var(--space-2x)',
        padding: 'var(--space-3)',
        border: '1px solid var(--info-border)',
        background: 'var(--info-quiet)',
        borderRadius: 'var(--radius-card)',
      }}
    >
      <span style={{ color: 'var(--info-text)', flex: 'none' }}>
        <Icon name="info" size={16} />
      </span>
      <span
        style={{
          font: 'var(--text-body-xs)',
          color: 'var(--text-secondary)',
          maxWidth: '82ch',
          textWrap: 'pretty',
        }}
      >
        {children}
      </span>
    </div>
  )
}

/** See `Review.tsx` — `detail` verbatim, `code` and `status` always rendered. */
function asProblem(error: unknown): ProblemDocument {
  if (!(error instanceof UsherProblem)) {
    return {
      status: 0,
      title: 'The request never reached the server.',
      detail: String(error),
    }
  }
  const errors = fieldErrors(error.errors).map((one) => ({ loc: [one.field], msg: one.message }))
  return {
    status: error.status,
    detail: error.detail,
    ...(error.knownCode === null ? {} : { code: error.knownCode }),
    ...(error.instance === undefined ? {} : { instance: error.instance }),
    ...(error.retryAfter === null ? {} : { retry_after: error.retryAfter }),
    ...(errors.length === 0 ? {} : { errors }),
  }
}
