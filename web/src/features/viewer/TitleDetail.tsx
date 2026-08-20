import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  Artwork,
  Badge,
  Button,
  Icon,
  PosterCard,
  Problem,
  ProgressBar,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  TargetPicker,
  TextLink,
  type PlayTarget as PickerTarget,
  type ProblemDocument as ProblemView,
} from '@/design-system'
import {
  UsherProblem,
  useEventStream,
  useMarkPlayed,
  usePlayTitle,
  useSetTitleWatchState,
  useSimilar,
  useTitle,
  type PlayTarget,
  type Schemas,
  type SimilarResponse,
  type TitleResponse,
} from '@/api'
import { ROUTES, personPath, playerPath, titlePath } from '@/app/routes'
import { useViewport } from '@/app/useViewport'
import { useProblemTrace } from '@/features/shared/trace'
import './TitleDetail.css'

/**
 * `GET /titles/{id}` as a screen: hero, availability per copy, play targets,
 * cast and crew, similar titles, images.
 *
 * Three facts about this route shape everything below.
 *
 * · **`enrichment_state: "skeleton"` is a first-class state, not an error.** It
 *   is the majority of a 1.27M-row catalog: a title known to a public dataset
 *   that no provider payload has ever been derived from. On such a row `cast`,
 *   `crew` and `images` are **absent from the payload entirely** — the route
 *   serialises with `response_model_exclude_unset=True` — and an absent key is
 *   "not applicable to this record", which is a *different fact* from `[]`,
 *   "we looked and there is nothing". patterns.md §2 gives them different
 *   treatments and collapsing them is a correctness bug, so the two are read
 *   apart here with `in` and rendered apart below.
 *
 * · **The server refuses to pick a playback winner.** `POST /play` answers
 *   every copy across every source in copy order, which is why `TargetPicker`
 *   is required rather than optional, and why the press needs a real pending
 *   state: the route resolves one adapter per copy against a 1–5 s upstream.
 *
 * · **A ticket URL is a secret** (patterns.md §13). Nothing here renders,
 *   copies, shares or logs `target.url`: `TargetPicker` is built on a type with
 *   `url` removed, and this screen's `onPlay` navigates to the player route by
 *   **id**, discarding the target it is handed. The ticket is minted fresh on
 *   arrival, so no ticket ever reaches the address bar either.
 */
export default function TitleDetail() {
  const { titleId } = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { phone } = useViewport()
  const traceOf = useProblemTrace()

  /**
   * How a title view is attributed back to the search that produced it
   * (`search_queries`). The route declares the parameter and the reference
   * client never sent it.
   */
  const searchId = searchParams.get('search_id')

  const title = useTitle(titleId, searchId ?? undefined)
  const similar = useSimilar(titleId)
  const play = usePlayTitle()
  const markPlayed = useMarkPlayed()
  const setWatchState = useSetTitleWatchState()

  const availabilityHeading = useRef<HTMLHeadingElement>(null)

  /**
   * patterns.md §7. A `title.updated` frame is how enrichment lands on an open
   * skeleton title, and the whole treatment is a 1000 ms highlight: opacity
   * only, no movement, no reorder, no focus theft. **The screen is correct if
   * zero frames ever arrive** — the frame only invalidates a query the screen
   * already knows how to render.
   */
  const [patchedAt, setPatchedAt] = useState<number | null>(null)
  useEffect(() => {
    if (patchedAt === null) return undefined
    const timer = setTimeout(() => setPatchedAt(null), 1000)
    return () => clearTimeout(timer)
  }, [patchedAt])

  useEventStream({
    enabled: titleId !== undefined,
    ...(titleId === undefined ? {} : { titles: [titleId] }),
    onEvent: (event) => {
      if (event.name !== 'title.updated') return
      if (event.payload.title_id !== titleId) return
      void queryClient.invalidateQueries({ queryKey: ['title', titleId] })
      void queryClient.invalidateQueries({ queryKey: ['similar', titleId] })
      setPatchedAt(Date.now())
    },
  })

  const onPlay = useCallback(() => {
    if (titleId === undefined) return
    /**
     * The chosen target is deliberately **not** carried across. The player
     * route names a title and mints its own ticket on arrival, so nothing that
     * could outlive the 300 s ticket — a history entry, a shared link, a
     * restored tab — ever holds one.
     */
    navigate(playerPath('title', titleId))
  }, [navigate, titleId])

  if (title.isPending) return <TitleLoading phone={phone} />

  if (title.isError) {
    return (
      <Problem
        scale="page"
        problem={problemView(title.error)}
        {...traceOf(title.error)}
        icon={<Icon name="file-question" size={24} />}
        actions={
          <>
            <Button variant="secondary" onClick={() => navigate(ROUTES.home)}>
              Back to home
            </Button>
            <Button variant="ghost" onClick={() => navigate(ROUTES.search)}>
              Search
            </Button>
          </>
        }
      />
    )
  }

  const record = title.data
  const skeleton = record.enrichment_state === 'skeleton'
  const backdrop = imageOf(record, 'backdrop')
  const poster = imageOf(record, 'poster')
  const watch = record.watch_state
  const targets = play.data?.targets ?? []

  return (
    <div className={patchedAt === null ? 'u-title' : 'u-title u-title--patched'}>
      <div className="u-title__hero">
        <Artwork id={backdrop} kind="backdrop" width={1280} name={record.name} alt="" />
        <span className="u-title__scrim" aria-hidden="true" />
        <div className="u-title__heroband">
          {/* At 390 there is no poster and the title sits over the backdrop. */}
          {!phone && (
            <div className="u-title__poster">
              <Artwork id={poster} kind="poster" width={342} name={record.name} alt="" />
            </div>
          )}
          <div className="u-title__headline">
            <h1 className="u-title__h1">{record.name}</h1>
            {record.tagline !== null && <p className="u-title__tagline">{record.tagline}</p>}
            <div className="u-title__facts">
              <span className="u-mono">{record.year ?? '—'}</span>
              {record.runtime_minutes !== null && (
                <span className="u-mono">{record.runtime_minutes} min</span>
              )}
              {record.community_rating !== null && (
                <span className="u-mono">★ {record.community_rating}</span>
              )}
              {record.genres.length > 0 && (
                <span className="u-title__genres">{record.genres.join(' · ')}</span>
              )}
              {skeleton && <Badge tier="skeleton">skeleton</Badge>}
            </div>
            {watch !== null && (
              <div className="u-title__progress">
                <ProgressBar
                  positionSeconds={watch.position_seconds}
                  runtimeSeconds={runtimeSeconds(record)}
                  played={watch.played}
                />
                <span className="u-mono u-title__progresstext">{watchLine(record, watch)}</span>
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="u-title__body">
        {skeleton && (
          <div className="u-title__live" role="status">
            <Icon name="radio" size={20} />
            <span>
              Opening this title asked the server for full metadata. It usually arrives within 5 seconds and
              fills in below — nothing here will move when it does.
            </span>
          </div>
        )}

        {/* `enrichment_error` is a stored string, not a problem document: a failed
            attempt neither consumes nor resets an enrichment rung, so the row is
            intact and will be retried. It is shown verbatim and never parsed. */}
        {record.enrichment_error !== null && (
          <div className="u-title__enrichfail" role="status">
            <Icon name="alert-triangle" size={20} />
            <div className="u-title__enrichfailbody">
              <span>Enrichment failed for this title.</span>
              <span className="u-mono">{record.enrichment_error}</span>
              <span className="u-title__note">
                The catalog row is intact; the extra metadata is not here yet.
              </span>
            </div>
          </div>
        )}

        <div className="u-title__columns">
          <section className="u-title__actions" aria-labelledby="title-actions">
            <h2 className="u-eyebrow" id="title-actions">
              Play
            </h2>
            <div className="u-title__buttons">
              <Button
                variant="primary"
                size="lg"
                loading={play.isPending}
                loadingLabel="Finding copies…"
                iconLeft={play.isPending ? null : <Icon name="play" size={20} />}
                onClick={() => {
                  if (titleId === undefined) return
                  play.mutate(searchId === null ? { titleId } : { titleId, searchId })
                }}
              >
                {playLabel(watch)}
              </Button>
              <Button
                variant="secondary"
                iconLeft={<Icon name="check" size={16} />}
                loading={markPlayed.isPending}
                loadingLabel="Marking watched…"
                onClick={() => {
                  if (titleId !== undefined) markPlayed.mutate({ titleId, played: true })
                }}
              >
                Mark watched
              </Button>
              <Button
                variant="ghost"
                iconLeft={<Icon name="rotate-ccw" size={16} />}
                loading={setWatchState.isPending}
                loadingLabel="Starting over…"
                onClick={() => {
                  if (titleId === undefined) return
                  setWatchState.mutate({
                    titleId,
                    body: { position_seconds: 0, played: false },
                  })
                }}
              >
                Start over
              </Button>
            </div>
            <span className="u-title__note">
              Play resolves one adapter per copy against your media server, so it takes a moment. Links it
              returns are valid for five minutes and are never shown or shared.
            </span>

            {play.isError && (
              <Problem
                scale="panel"
                problem={problemView(play.error)}
                {...traceOf(play.error)}
                icon={<Icon name="alert-triangle" size={20} />}
                /* No `onRetry`. `not_playable` will fail identically every time,
                   so patterns.md §3 replaces the retry with this one action. */
                actions={
                  <Button variant="secondary" onClick={() => availabilityHeading.current?.focus()}>
                    See other copies
                  </Button>
                }
              />
            )}

            {play.isSuccess && targets.length > 0 && (
              <div className="u-title__targets">
                <span className="u-eyebrow">{copiesLine(targets)}</span>
                <TargetPicker targets={targets.map(pickerTarget)} onPlay={onPlay} />
              </div>
            )}

            {play.isSuccess && targets.length === 0 && (
              <StateBlock kind="empty" title="No copy answered" meta="targets: []">
                Every configured source was asked and none of them holds a playable copy right now.
              </StateBlock>
            )}
          </section>

          <section className="u-title__availability" aria-labelledby="title-availability">
            <h2 className="u-eyebrow" id="title-availability" tabIndex={-1} ref={availabilityHeading}>
              Availability
            </h2>
            {record.availability.length === 0 ? (
              <StateBlock kind="na">
                No copy of this title exists on any source. It is in the catalog because a public dataset
                knows about it.
              </StateBlock>
            ) : (
              <ul className="u-title__copies">
                {/* One source legitimately holds several copies of a title —
                    a 2160p remux beside a 1080p one — and the response carries
                    no per-copy id, so `source_id` alone is not unique and the
                    position in the response is the only remaining tie-break.
                    The list is server-ordered and never re-sorted here. */}
                {record.availability.map((copy, index) => (
                  // oxlint-disable-next-line react/no-array-index-key
                  <li key={`${copy.source_id}-${index}`}>
                    <AvailabilityRow copy={copy} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>

        <section className="u-title__overview" aria-labelledby="title-overview">
          <h2 className="u-eyebrow" id="title-overview">
            Overview
          </h2>
          {record.overview === null ? (
            <StateBlock kind="never" meta={`overview: null · enrichment_state: ${record.enrichment_state}`}>
              This title has never been enriched, so we have a name and a year and nothing else. That is true
              of most of the 1.27M rows in the catalog.
            </StateBlock>
          ) : (
            <p className="u-title__prose">{record.overview}</p>
          )}
        </section>

        {/* Cast and crew, and the three-way distinction this screen exists to keep:
            the key absent (skeleton — not applicable to this record), the key
            present and empty (enrichment ran and found nobody), or credits. */}
        <div className="u-title__credits">
          <CreditColumn
            heading="Cast · billing order, capped at 20"
            field="cast"
            credits={creditsOf(record, 'cast')}
            secondary={(credit) => credit.character}
            onOpenPerson={(personId) => navigate(personPath(personId))}
          />
          <CreditColumn
            heading="Crew · billing order, capped at 20"
            field="crew"
            credits={creditsOf(record, 'crew')}
            secondary={(credit) => credit.job}
            onOpenPerson={(personId) => navigate(personPath(personId))}
          />
        </div>

        <SimilarSection query={similar} onOpenTitle={(id) => navigate(titlePath(id))} />

        <ImagesSection images={imagesOf(record)} name={record.name} />

        <span className="u-title__note">
          Metadata from TMDb and IMDb.{' '}
          <TextLink
            href={ROUTES.about}
            onClick={(event) => {
              if (event.metaKey || event.ctrlKey || event.shiftKey) return
              event.preventDefault()
              navigate(ROUTES.about)
            }}
          >
            Attribution and licensing
          </TextLink>
          .
        </span>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ loading */

/**
 * patterns.md §1: a full-bleed block for the backdrop, then `shape="hero"`.
 * Never a route spinner — a spinner reads as "restarting", a skeleton shaped
 * like the layout reads as "arriving". The skeleton is `aria-hidden` and the
 * region that owns it carries `aria-busy` and the sentence.
 */
function TitleLoading({ phone }: { phone: boolean }) {
  return (
    <SkeletonRegion busy label="Loading title …" className="u-title">
      <Skeleton shape="block" height={phone ? 260 : 420} style={{ borderRadius: 0 }} />
      <div className="u-title__body">
        <Skeleton shape="hero" />
      </div>
    </SkeletonRegion>
  )
}

/* ------------------------------------------------------------- availability */

/**
 * One copy. **There is no `quality` field on the wire** — the string is
 * composed here from `resolution` and `hdr_format` (plus codec and container),
 * each printed exactly as the API sent it. Reformatting `3840x2160` into
 * `2160p` would be a client inventing a fact the server did not state.
 */
function AvailabilityRow({ copy }: { copy: Schemas['AvailabilityResponse'] }) {
  const specs = [copy.resolution, copy.hdr_format, copy.video_codec, copy.container].filter(
    (value): value is string => typeof value === 'string' && value !== '',
  )
  return (
    <div className={copy.available ? 'u-copy' : 'u-copy u-copy--missing'}>
      {/* `hard-drive` is the handoff's glyph here and is not in the registry,
          which is read-only; `server` is the nearest sanctioned one and names
          the same thing — the machine the copy lives on. */}
      <Icon name={copy.available ? 'server' : 'alert-triangle'} size={16} />
      <span className="u-mono u-copy__specs">
        {specs.length > 0 ? specs.join(' · ') : 'No format on record'}
      </span>
      <span className="u-copy__source">{copy.source}</span>
      {!copy.available && <Badge tone="warn">missing</Badge>}
    </div>
  )
}

/* ------------------------------------------------------------- cast and crew */

interface CreditColumnProps {
  heading: string
  field: 'cast' | 'crew'
  /** `undefined` is the key being **absent**, which is not the same as `[]`. */
  credits: Schemas['CreditResponse'][] | undefined
  secondary: (credit: Schemas['CreditResponse']) => string | null
  onOpenPerson: (personId: string) => void
}

function CreditColumn({ heading, field, credits, secondary, onOpenPerson }: CreditColumnProps) {
  return (
    <section className="u-title__creditcol" aria-label={heading}>
      <h2 className="u-eyebrow">{heading}</h2>
      {credits === undefined ? (
        /* Absent from the payload: not applicable to this record. Em dash, one
           clause, no border — and deliberately *not* the empty treatment. */
        <StateBlock kind="na">
          {field === 'cast'
            ? 'Credits are not on this record; it has never been enriched.'
            : 'Crew credits are not on this record; it has never been enriched.'}
        </StateBlock>
      ) : credits.length === 0 ? (
        /* Present and empty: enrichment ran and returned nobody. */
        <StateBlock kind="empty" meta={`${field}: []`}>
          {field === 'cast'
            ? 'Enrichment ran for this title and returned no cast.'
            : 'Enrichment ran for this title and returned no crew.'}
        </StateBlock>
      ) : (
        <>
          <ul className="u-title__creditlist">
            {/* A person can hold two credits on one title — two characters, or
                a writer who also directed — so `person_id` is not unique in
                either list. The order *is* `billing_order`, already spent by
                the server's `ORDER BY … NULLS LAST`, so the position is stable
                and is the correct tie-break. */}
            {credits.map((credit, index) => {
              const role = secondary(credit)
              return (
                // oxlint-disable-next-line react/no-array-index-key
                <li key={`${credit.person_id}-${index}`}>
                  <button
                    type="button"
                    className="u-credit"
                    /* Composed, because the two spans abut with no whitespace
                       between them and the computed name would otherwise read
                       "Alexander KaidanovskyStalker". */
                    aria-label={role === null ? credit.name : `${credit.name}, ${role}`}
                    onClick={() => onOpenPerson(credit.person_id)}
                  >
                    <span className="u-credit__name">{credit.name}</span>
                    <span className="u-credit__role">{role ?? '—'}</span>
                  </button>
                </li>
              )
            })}
          </ul>
          <span className="u-title__note">No photographs exist for people anywhere in this API.</span>
        </>
      )}
    </section>
  )
}

/* ----------------------------------------------------------------- similar */

/**
 * Four states, and every one of them is a different fact (patterns.md §2):
 *
 * · neighbours, current — the row.
 * · `stale: true` — the row **is still shown**, with an amber marker. The list
 *   is real and its inputs moved; suppressing it would be a bigger lie than
 *   showing it.
 * · `neighbors: []` with a `computed_at` — computed, and nothing scored close
 *   enough.
 * · `computed_at: null` — never computed at all, with `meta` naming the field.
 *
 * There are **no similarity reasons in this API**. PRD 07 describes them and no
 * such field exists, so nothing here invents one: the row says outright that
 * there is no "why".
 */
function SimilarSection({
  query,
  onOpenTitle,
}: {
  query: { isPending: boolean; isError: boolean; data: SimilarResponse | undefined }
  onOpenTitle: (titleId: string) => void
}) {
  if (query.isPending) {
    return (
      <SkeletonRegion busy label="Loading similar titles …" className="u-title__similar">
        <h2 className="u-title__h2">Similar titles</h2>
        <Skeleton shape="rail" count={6} />
      </SkeletonRegion>
    )
  }

  if (query.isError || query.data === undefined) {
    return (
      <section className="u-title__similar" aria-labelledby="title-similar">
        <h2 className="u-title__h2" id="title-similar">
          Similar titles
        </h2>
        <StateBlock kind="never" meta="GET /titles/{id}/similar">
          We could not read similar titles for this one.
        </StateBlock>
      </section>
    )
  }

  const { neighbors, computed_at: computedAt, stale } = query.data

  return (
    <section className="u-title__similar" aria-labelledby="title-similar">
      <div className="u-title__similarhead">
        <h2 className="u-title__h2" id="title-similar">
          Similar titles
        </h2>
        {stale && (
          <Badge tone="warn" icon={<Icon name="history" />}>
            stale
          </Badge>
        )}
        {stale && computedAt !== null && (
          <span className="u-title__note">
            Computed {shortDate(computedAt)}, before the scoring blend changed. Shown as they were. Fixed at
            ten; there is no {'“why”'}.
          </span>
        )}
      </div>

      {computedAt === null ? (
        <StateBlock kind="never" meta="computed_at: null">
          We have never computed similar titles for this one.
        </StateBlock>
      ) : neighbors.length === 0 ? (
        <StateBlock kind="empty" meta={`neighbors: [] · computed_at: ${computedAt}`}>
          Computed {shortDate(computedAt)}. Nothing scored close enough to show.
        </StateBlock>
      ) : (
        <div className="u-title__rail">
          {neighbors.map((neighbor) => (
            <PosterCard
              key={neighbor.id}
              card={{
                title_id: neighbor.id,
                kind: neighbor.kind,
                name: neighbor.name,
                year: neighbor.year,
              }}
              onOpen={() => onOpenTitle(neighbor.id)}
            />
          ))}
        </div>
      )}
    </section>
  )
}

/* ------------------------------------------------------------------ images */

function ImagesSection({ images, name }: { images: Schemas['ImageResponse'][] | undefined; name: string }) {
  return (
    <section className="u-title__images" aria-labelledby="title-images">
      <h2 className="u-eyebrow" id="title-images">
        {images === undefined || images.length === 0 ? 'Images' : `Images · ${images.length} on record`}
      </h2>
      {images === undefined ? (
        <StateBlock kind="na">No images are on this record; artwork arrives with enrichment.</StateBlock>
      ) : images.length === 0 ? (
        <StateBlock kind="empty" meta="images: []">
          Enrichment ran for this title and returned no artwork.
        </StateBlock>
      ) : (
        <ul className="u-title__imagegrid">
          {images.map((image) => (
            <li key={image.id} className={`u-title__image u-title__image--${image.kind}`}>
              <Artwork
                id={image.id}
                kind={image.kind}
                width={image.kind === 'poster' || image.kind === 'profile' ? 154 : 342}
                name={name}
                alt=""
              />
              <span className="u-mono u-title__imagekind">{image.kind}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/* ----------------------------------------------------------------- helpers */

/**
 * The absent-vs-empty read, done once.
 *
 * `schema.d.ts` declares `cast` and `crew` required with a `[]` default, which
 * OpenAPI cannot qualify with `response_model_exclude_unset=True` — so the
 * generated type is optimistic and the wire is the authority. `in` is what
 * separates the two on the wire without a cast.
 */
function creditsOf(title: TitleResponse, field: 'cast' | 'crew'): Schemas['CreditResponse'][] | undefined {
  return field in title ? title[field] : undefined
}

function imagesOf(title: TitleResponse): Schemas['ImageResponse'][] | undefined {
  return 'images' in title ? title.images : undefined
}

/** First image of a kind, or `null` — which `Artwork` draws as its own state. */
function imageOf(title: TitleResponse, kind: Schemas['ImageKind']): string | null {
  const images = imagesOf(title)
  return images?.find((image) => image.kind === kind)?.id ?? null
}

function runtimeSeconds(title: TitleResponse): number | null {
  return title.runtime_minutes === null ? null : title.runtime_minutes * 60
}

function playLabel(watch: Schemas['WatchStateResponse'] | null): string {
  if (watch === null || watch.position_seconds <= 0) return 'Play'
  return `Resume at ${clock(watch.position_seconds)}`
}

/** `1:09` past the hour, `52 min` below it. */
function clock(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)
  return hours > 0 ? `${hours}:${String(minutes % 60).padStart(2, '0')}` : `${minutes} min`
}

function watchLine(title: TitleResponse, watch: Schemas['WatchStateResponse']): string {
  const played = watch.played ? 'watched' : null
  const position =
    title.runtime_minutes === null
      ? null
      : `${Math.round(watch.position_seconds / 60)} of ${title.runtime_minutes} min`
  const last = watch.last_played_at === null ? null : `last played ${shortDate(watch.last_played_at)}`
  return [position, played, last].filter((part): part is string => part !== null).join(' · ')
}

function shortDate(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime())
    ? iso
    : at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

/** "4 copies across 2 sources" — both numbers are real counts of this response. */
function copiesLine(targets: PlayTarget[]): string {
  const sources = new Set(targets.map((target) => target.source.id)).size
  const copies = `${targets.length} ${targets.length === 1 ? 'copy' : 'copies'}`
  return `${copies} across ${sources} ${sources === 1 ? 'source' : 'sources'}`
}

/**
 * `PlayTargetResponse` → the picker's `PlayTarget`.
 *
 * Field by field rather than a spread, because `exactOptionalPropertyTypes`
 * makes `{container: null}` a different thing from an absent `container`, and
 * the picker's contract says absent. `url` is carried across because the
 * picker's own type removes it again before anything renders.
 */
function pickerTarget(target: PlayTarget): PickerTarget {
  return {
    kind: target.kind,
    url: target.url,
    source: { id: target.source.id, name: target.source.name },
    ...(target.scheme == null ? {} : { scheme: target.scheme }),
    ...(target.container == null ? {} : { container: target.container }),
    ...(target.video_codec == null ? {} : { video_codec: target.video_codec }),
    ...(target.audio == null ? {} : { audio: target.audio }),
    ...(target.hdr_format == null ? {} : { hdr_format: target.hdr_format }),
    ...(target.resolution == null ? {} : { resolution: target.resolution }),
    ...(target.runtime_seconds == null ? {} : { runtime_seconds: target.runtime_seconds }),
    ...(target.resume_position_seconds == null
      ? {}
      : { resume_position_seconds: target.resume_position_seconds }),
  }
}

/**
 * An `UsherProblem` as the design system's document. `code` and `status` are
 * carried so they can be shown in mono, `detail` verbatim and never parsed, and
 * `instance` when the server named the route that failed.
 */
function problemView(error: unknown): ProblemView {
  if (!(error instanceof UsherProblem)) {
    return { title: 'Something went wrong.', detail: String(error) }
  }
  return {
    ...(error.knownCode === null ? {} : { code: error.knownCode }),
    status: error.status,
    detail: error.detail,
    ...(error.instance === undefined ? {} : { instance: error.instance }),
  }
}
