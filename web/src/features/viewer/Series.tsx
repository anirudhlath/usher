import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Artwork,
  Badge,
  Button,
  Icon,
  LoadMore,
  Problem,
  ProgressBar,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  Tabs,
  type ProblemDocument as ProblemView,
} from '@/design-system'
import {
  UsherProblem,
  useEventStream,
  useSeasonEpisodes,
  useSeasons,
  useSetEpisodeWatchState,
  useTitle,
  type Schemas,
} from '@/api'
import { ROUTES, episodePath, playerPath } from '@/app/routes'
import { useViewport } from '@/app/useViewport'
import { useProblemTrace } from '@/features/shared/trace'
import './Series.css'

type Season = Schemas['SeasonResponse']
type Episode = Schemas['EpisodeResponse']

/**
 * The series hierarchy: `GET /series/{id}/seasons` for the switcher and
 * `GET /seasons/{id}/episodes` for the keyset-paged list under it.
 *
 * Two things this screen refuses to smooth over.
 *
 * · **Season 0 is real.** It is Specials, it is a season the provider counts,
 *   and hiding it because the number is falsy would drop episodes a household
 *   owns. It is a tab like any other.
 *
 * · **`episode_count` is the provider's count and the list is what we hold, and
 *   the two legitimately disagree** — a season block TMDb declines to serve
 *   comes back as the same 200 with the key silently absent, leaving a season
 *   row carrying a count and no episodes. Where both numbers are visible this
 *   screen says which is which rather than quietly showing fewer. It only
 *   claims a *disagreement* once the walk has ended (`next_cursor: null`);
 *   until then the honest label is "loaded so far", because there is no
 *   denominator in a keyset walk (patterns.md §4, §14).
 */
export default function Series() {
  const { titleId } = useParams()
  const navigate = useNavigate()
  const { phone } = useViewport()
  const traceOf = useProblemTrace()

  const title = useTitle(titleId)
  const seasons = useSeasons(titleId)
  const [chosen, setChosen] = useState<string | null>(null)

  const ordered = [...(seasons.data?.seasons ?? [])].sort(
    (left, right) => left.season_number - right.season_number,
  )
  /** Specials are a tab, never the default one. */
  const fallback = ordered.find((season) => season.season_number > 0) ?? ordered[0]
  const selected = ordered.find((season) => String(season.season_number) === chosen) ?? fallback

  const episodes = useSeasonEpisodes(selected?.id)
  const setWatchState = useSetEpisodeWatchState()

  /**
   * An episode carries no watch state of its own, so this map is the only place
   * progress can come from: what this client wrote, plus what another device
   * reported over the live channel. With zero frames and no writes it stays
   * empty and the list says why — which is the correct rendering, not a
   * degraded one (patterns.md §7).
   */
  const [observed, setObserved] = useState<Record<string, ObservedWatch>>({})

  useEventStream({
    enabled: titleId !== undefined,
    ...(titleId === undefined ? {} : { titles: [titleId] }),
    onEvent: (event) => {
      if (event.name !== 'watchstate.updated') return
      const { episode_id: episodeId, position_seconds: position, played } = event.payload
      if (episodeId === null) return
      setObserved((current) => ({
        ...current,
        [episodeId]: {
          position_seconds: position ?? 0,
          played: played ?? false,
          origin: 'another device',
        },
      }))
    },
  })

  if (title.isPending) return <SeriesLoading />

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
  const poster = 'images' in record ? (record.images.find((i) => i.kind === 'poster')?.id ?? null) : null

  return (
    <div className="u-series">
      <header className="u-series__head">
        <div className="u-series__poster">
          <Artwork id={poster} kind="poster" width={154} name={record.name} alt="" />
        </div>
        <div>
          <span className="u-eyebrow">Series</span>
          <h1 className="u-series__h1">{record.name}</h1>
          <span className="u-mono u-series__facts">
            {[
              record.year === null ? null : String(record.year),
              ordered.length === 0
                ? null
                : `${ordered.length} ${ordered.length === 1 ? 'season' : 'seasons'}`,
              record.availability[0]?.source ?? null,
            ]
              .filter((part): part is string => part !== null)
              .join(' · ')}
          </span>
        </div>
      </header>

      {seasons.isPending ? (
        <SkeletonRegion busy label="Loading seasons …">
          <Skeleton shape="text" lines={1} width="40%" />
        </SkeletonRegion>
      ) : seasons.isError ? (
        <Problem scale="panel" problem={problemView(seasons.error)} {...traceOf(seasons.error)} />
      ) : ordered.length === 0 || selected === undefined ? (
        <StateBlock kind="empty" title="No seasons on record" meta="seasons: []">
          This series has no season rows. Nothing has walked its hierarchy yet.
        </StateBlock>
      ) : (
        <Tabs
          id="seasons"
          value={String(selected.season_number)}
          onChange={setChosen}
          tabs={ordered.map((season) => ({
            value: String(season.season_number),
            label: seasonLabel(season),
            /* A real, non-paginated number the provider states. It is not a
               total of the list below, and the panel says so. */
            ...(season.episode_count === null ? {} : { count: season.episode_count }),
          }))}
        >
          <SeasonPanel
            season={selected}
            episodes={episodes}
            observed={observed}
            phone={phone}
            pendingEpisodeId={setWatchState.isPending ? (setWatchState.variables?.episodeId ?? null) : null}
            onOpenEpisode={(episodeId) => navigate(episodePath(episodeId))}
            onPlayEpisode={(episodeId) => navigate(playerPath('episode', episodeId))}
            onMarkWatched={(episodeId) => {
              setWatchState.mutate(
                { episodeId, body: { position_seconds: 0, played: true } },
                {
                  onSuccess: (written) => {
                    setObserved((current) => ({
                      ...current,
                      [episodeId]: {
                        position_seconds: written.position_seconds,
                        played: written.played,
                        origin: 'this client',
                      },
                    }))
                  },
                },
              )
            }}
          />
        </Tabs>
      )}

      <span className="u-series__note">
        Episode progress is written, not read: an episode carries no watch state of its own, so what you see
        here comes from what this client last sent plus anything another device reported over the live
        channel.
      </span>
    </div>
  )
}

/* ------------------------------------------------------------ season panel */

interface ObservedWatch {
  position_seconds: number
  played: boolean
  origin: 'this client' | 'another device'
}

/**
 * The hook's own result type rather than a hand-written structural echo of it:
 * a change in `hooks.ts` is then a compile error here instead of a shape that
 * still fits.
 */
type EpisodesQuery = ReturnType<typeof useSeasonEpisodes>

interface SeasonPanelProps {
  season: Season
  episodes: EpisodesQuery
  observed: Record<string, ObservedWatch>
  phone: boolean
  pendingEpisodeId: string | null
  onOpenEpisode: (episodeId: string) => void
  onPlayEpisode: (episodeId: string) => void
  onMarkWatched: (episodeId: string) => void
}

function SeasonPanel({
  season,
  episodes,
  observed,
  phone,
  pendingEpisodeId,
  onOpenEpisode,
  onPlayEpisode,
  onMarkWatched,
}: SeasonPanelProps) {
  const traceOf = useProblemTrace()

  if (episodes.isPending) return <EpisodeListLoading />
  if (episodes.isError)
    return <Problem scale="panel" problem={problemView(episodes.error)} {...traceOf(episodes.error)} />

  const pages = episodes.data?.pages ?? []
  const items = pages.flatMap((page) => page.items)
  const last = pages.at(-1)
  const nextCursor = last?.next_cursor ?? null
  const complete = nextCursor === null
  const count = season.episode_count
  const name = seasonLabel(season)

  if (items.length === 0 && complete) {
    return (
      <StateBlock
        kind="empty"
        title="No episodes returned for this season"
        meta="items: [] · next_cursor: null"
      >
        {count === null
          ? `The episode list for ${name} came back empty, and the provider gave no count to compare it with.`
          : `The provider reports ${count} episodes for ${name}, and the episode list came back empty. Both numbers are true: the count is the provider's, the list is what we hold.`}
      </StateBlock>
    )
  }

  return (
    <>
      <div className="u-series__countline">
        <span className="u-series__seasonname">{name}</span>
        {count === null ? (
          <StateBlock kind="never" meta="episode_count: null">
            The provider never supplied an episode count for this season, so there is nothing to compare the
            list against.
          </StateBlock>
        ) : complete && count !== items.length ? (
          <>
            <Badge tone="warn" icon={<Icon name="alert-triangle" />}>
              provider says {count} · we hold {items.length}
            </Badge>
            <span className="u-series__note">
              Both numbers are true: the count is the provider&apos;s, the list is what we hold.
            </span>
          </>
        ) : complete ? (
          <Badge tone="neutral">
            provider says {count} · we hold {items.length}
          </Badge>
        ) : (
          <Badge tone="neutral">
            provider says {count} · {items.length} loaded so far
          </Badge>
        )}
        <span className="u-series__note">Episodes load 50 at a time, keyset-paged.</span>
      </div>

      <ul className="u-series__list">
        {items.map((episode) => (
          <li key={episode.id}>
            <EpisodeRow
              episode={episode}
              watch={observed[episode.id]}
              phone={phone}
              marking={pendingEpisodeId === episode.id}
              onOpen={() => onOpenEpisode(episode.id)}
              onPlay={() => onPlayEpisode(episode.id)}
              onMarkWatched={() => onMarkWatched(episode.id)}
            />
          </li>
        ))}
      </ul>

      <LoadMore
        nextCursor={nextCursor}
        autoLoad
        loading={episodes.isFetchingNextPage}
        onLoad={() => {
          episodes.fetchNextPage()
        }}
        loadedLabel={`${items.length} loaded so far`}
        endMessage={`That is every episode we hold for ${name}.`}
      />
    </>
  )
}

/* -------------------------------------------------------------- one episode */

function EpisodeRow({
  episode,
  watch,
  phone,
  marking,
  onOpen,
  onPlay,
  onMarkWatched,
}: {
  episode: Episode
  watch: ObservedWatch | undefined
  phone: boolean
  marking: boolean
  onOpen: () => void
  onPlay: () => void
  onMarkWatched: () => void
}) {
  const started = watch !== undefined && watch.position_seconds > 0 && !watch.played
  const runtime = episode.runtime_minutes === null ? null : episode.runtime_minutes * 60

  return (
    <div className="u-ep">
      <div className="u-ep__still">
        {/* `GET /seasons/{id}/episodes` carries no artwork id, so this is the
            proxy's own "no artwork on record" state rather than a broken image. */}
        <Artwork id={null} kind="still" width={342} name={episode.name ?? ''} alt="" />
        {watch !== undefined && (
          <div className="u-ep__progress">
            <ProgressBar
              positionSeconds={watch.position_seconds}
              runtimeSeconds={runtime}
              played={watch.played}
            />
          </div>
        )}
      </div>

      <div className="u-ep__body">
        <div className="u-ep__heading">
          <span className="u-mono u-ep__number">{String(episode.episode_number).padStart(2, '0')}</span>
          <button type="button" className="u-ep__name" onClick={onOpen}>
            {episode.name ?? 'No name on record'}
          </button>
          {watch?.played === true && (
            <Badge tone="good" icon={<Icon name="check-circle" />}>
              watched
            </Badge>
          )}
        </div>
        <span className="u-mono u-ep__meta">
          {[
            episode.air_date,
            episode.runtime_minutes === null ? null : `${episode.runtime_minutes} min`,
            watch === undefined ? null : `progress from ${watch.origin}`,
          ]
            .filter((part): part is string => part !== null && part !== '')
            .join(' · ')}
        </span>
        {!phone && episode.overview !== null && <span className="u-ep__overview">{episode.overview}</span>}
      </div>

      <div className="u-ep__actions">
        <Button
          size="sm"
          variant={started ? 'primary' : 'secondary'}
          iconLeft={<Icon name="play" size={16} />}
          onClick={onPlay}
        >
          {started ? 'Resume' : watch?.played === true ? 'Watch again' : 'Play'}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          iconLeft={<Icon name="check" size={16} />}
          loading={marking}
          loadingLabel="Marking watched…"
          onClick={onMarkWatched}
        >
          Mark watched
        </Button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ loading */

function SeriesLoading() {
  return (
    <SkeletonRegion busy label="Loading series …" className="u-series">
      <div className="u-series__head">
        <div className="u-series__poster">
          <Skeleton shape="block" width="100%" height={144} />
        </div>
        <Skeleton shape="text" lines={2} width="18rem" />
      </div>
      <EpisodeListLoading />
    </SkeletonRegion>
  )
}

/**
 * patterns.md §1: the episode list's skeleton is the table shape with a 16:9
 * block in the leading cell, four rows. The design system's `table` shape has
 * no leading still — it is built for operator tables — so the row is composed
 * here from the same `u-skel` primitives rather than by widening a
 * design-system contract this one surface needs.
 */
function EpisodeListLoading() {
  return (
    <SkeletonRegion busy label="Loading episodes …" className="u-series__skeleton">
      {[0, 1, 2, 3].map((row) => (
        <div className="u-series__skeletonrow" key={row}>
          <span className="u-series__skeletonstill">
            <Skeleton shape="block" width="100%" style={{ aspectRatio: '16 / 9', height: 'auto' }} />
          </span>
          <Skeleton shape="text" lines={2} />
        </div>
      ))}
    </SkeletonRegion>
  )
}

/* ----------------------------------------------------------------- helpers */

/** Season 0 is Specials. The provider's own name wins when it sent one. */
function seasonLabel(season: Season): string {
  if (season.name !== null && season.name !== '') return season.name
  return season.season_number === 0 ? 'Specials' : `Season ${season.season_number}`
}

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
