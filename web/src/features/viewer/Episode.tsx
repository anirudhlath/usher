import { useCallback, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Artwork,
  Badge,
  Button,
  Icon,
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
  useEpisode,
  useEventStream,
  usePlayEpisode,
  useSetEpisodeWatchState,
  useTitle,
  type PlayTarget,
} from '@/api'
import { ROUTES, playerPath, seriesPath } from '@/app/routes'
import { useProblemTrace } from '@/features/shared/trace'
import './Episode.css'

/**
 * One episode, from `GET /episodes/{id}`.
 *
 * **The episode response carries no watch state.** There is no `watch_state`
 * key on it at all — progress for an episode is *written* (`PUT
 * /watch/episodes/{id}`) and read back only through the series' own aggregates
 * and the live channel. So this screen shows the last state it wrote, and says
 * outright that that is what it is: a screen that drew a progress bar as though
 * it had read one would be inventing a reading. With zero writes and zero
 * frames the never-computed treatment is the correct rendering, not a
 * degraded one.
 *
 * The breadcrumb is resolved through the parent title (`title_id` is on the
 * episode, which is what the DTO calls "the two ids a client climbs back up
 * with"), so the name in it is the series' real name rather than an id.
 *
 * A playback ticket is a secret: `TargetPicker` cannot print `target.url`, and
 * the play handler here navigates by **id** and discards the target it is
 * handed.
 */
export default function Episode() {
  const { episodeId } = useParams()
  const navigate = useNavigate()
  const traceOf = useProblemTrace()

  const episode = useEpisode(episodeId)
  const parentId = episode.data?.title_id
  const parent = useTitle(parentId)
  const play = usePlayEpisode()
  const setWatchState = useSetEpisodeWatchState()

  /**
   * The only progress this screen can honestly show, and where it came from.
   * `null` until something is written or a frame arrives.
   */
  const [observed, setObserved] = useState<ObservedWatch | null>(null)

  useEventStream({
    enabled: parentId !== undefined,
    ...(parentId === undefined ? {} : { titles: [parentId] }),
    onEvent: (event) => {
      if (event.name !== 'watchstate.updated') return
      if (event.payload.episode_id !== episodeId) return
      setObserved({
        position_seconds: event.payload.position_seconds ?? 0,
        played: event.payload.played ?? false,
        origin: 'another device',
        at: event.payload.observed_at ?? new Date(event.at).toISOString(),
      })
    },
  })

  const onPlay = useCallback(() => {
    if (episodeId === undefined) return
    // By id. The ticket is minted on arrival and never enters the address bar.
    navigate(playerPath('episode', episodeId))
  }, [navigate, episodeId])

  if (episode.isPending) return <EpisodeLoading />

  if (episode.isError) {
    return (
      <Problem
        scale="page"
        problem={problemView(episode.error)}
        {...traceOf(episode.error)}
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

  const record = episode.data
  const code = episodeCode(record.season_number, record.episode_number)
  const targets = play.data?.targets ?? []
  const runtime = record.runtime_minutes === null ? null : record.runtime_minutes * 60

  return (
    <div className="u-episode">
      <nav className="u-episode__crumbs" aria-label="Breadcrumb">
        <ol>
          <li>
            {parent.isPending ? (
              <SkeletonRegion busy label="Loading series name …">
                <Skeleton shape="text" lines={1} width="8rem" />
              </SkeletonRegion>
            ) : parent.isError || parent.data === undefined ? (
              /* The climb failed. The id is still true and is printed rather
                 than swallowed, so the link that is missing is visible. */
              <span className="u-mono u-episode__crumbfail">title_id {record.title_id}</span>
            ) : (
              <TextLink
                href={seriesPath(record.title_id)}
                onClick={(event) => {
                  if (event.metaKey || event.ctrlKey || event.shiftKey) return
                  event.preventDefault()
                  navigate(seriesPath(record.title_id))
                }}
              >
                {parent.data.name}
              </TextLink>
            )}
          </li>
          <li aria-hidden="true">/</li>
          <li>
            <span className="u-mono">{code}</span>
          </li>
        </ol>
      </nav>

      <header className="u-episode__head">
        <div className="u-episode__still">
          {/* The episode DTO carries no artwork id; this is the proxy's own
              "no artwork on record" state, not a failed load. */}
          <Artwork id={null} kind="still" width={342} name={record.name ?? code} alt="" />
        </div>
        <div className="u-episode__headline">
          <span className="u-eyebrow">{code}</span>
          <h1 className="u-episode__h1">{record.name ?? code}</h1>
          {record.name === null && <StateBlock kind="na">No name is on record for this episode.</StateBlock>}
          <span className="u-mono u-episode__facts">
            {[
              record.air_date,
              record.runtime_minutes === null ? null : `${record.runtime_minutes} min`,
              record.absolute_number === null ? null : `absolute ${record.absolute_number}`,
            ]
              .filter((part): part is string => part !== null && part !== '')
              .join(' · ')}
          </span>
        </div>
      </header>

      <section className="u-episode__section" aria-labelledby="episode-overview">
        <h2 className="u-eyebrow" id="episode-overview">
          Overview
        </h2>
        {record.overview === null ? (
          <StateBlock kind="never" meta="overview: null">
            No overview has ever been written for this episode.
          </StateBlock>
        ) : (
          <p className="u-episode__prose">{record.overview}</p>
        )}
      </section>

      <section className="u-episode__section" aria-labelledby="episode-play">
        <h2 className="u-eyebrow" id="episode-play">
          Play
        </h2>
        <div className="u-episode__buttons">
          <Button
            variant="primary"
            size="lg"
            loading={play.isPending}
            loadingLabel="Finding copies…"
            iconLeft={play.isPending ? null : <Icon name="play" size={20} />}
            onClick={() => {
              if (episodeId !== undefined) play.mutate({ episodeId })
            }}
          >
            {observed !== null && observed.position_seconds > 0 && !observed.played ? 'Resume' : 'Play'}
          </Button>
          <Button
            variant="secondary"
            iconLeft={<Icon name="check" size={16} />}
            loading={setWatchState.isPending}
            loadingLabel="Marking watched…"
            onClick={() => {
              if (episodeId === undefined) return
              setWatchState.mutate(
                { episodeId, body: { position_seconds: 0, played: true } },
                {
                  onSuccess: (written) => {
                    setObserved({
                      position_seconds: written.position_seconds,
                      played: written.played,
                      origin: 'this client',
                      at: written.last_played_at ?? new Date().toISOString(),
                    })
                  },
                },
              )
            }}
          >
            Mark watched
          </Button>
        </div>
        <span className="u-episode__note">
          Play resolves one adapter per copy against your media server, so it takes a moment. Links it returns
          are valid for five minutes and are never shown or shared.
        </span>

        {play.isError && (
          <Problem
            scale="panel"
            problem={problemView(play.error)}
            {...traceOf(play.error)}
            icon={<Icon name="alert-triangle" size={20} />}
            /* No retry: `not_playable` fails identically every time. */
            actions={
              <Button variant="secondary" onClick={() => navigate(seriesPath(record.title_id))}>
                See other copies
              </Button>
            }
          />
        )}

        {play.isSuccess && targets.length > 0 && (
          <div className="u-episode__targets">
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

      <section className="u-episode__section" aria-labelledby="episode-watch">
        <h2 className="u-eyebrow" id="episode-watch">
          Watch state
        </h2>
        {observed === null ? (
          <StateBlock kind="never" meta="GET /episodes/{episode_id} · no watch_state field">
            Episode progress is written, not read: an episode carries no watch state of its own, so what you
            see here comes from what this client last sent plus anything another device reported over the live
            channel.
          </StateBlock>
        ) : (
          <div className="u-episode__watch">
            <ProgressBar
              positionSeconds={observed.position_seconds}
              runtimeSeconds={runtime}
              played={observed.played}
            />
            <div className="u-episode__watchline">
              {observed.played && (
                <Badge tone="good" icon={<Icon name="check-circle" />}>
                  watched
                </Badge>
              )}
              <span className="u-episode__note">
                {observed.origin === 'this client'
                  ? `This is what this client wrote at ${clockTime(observed.at)}, not a reading — the episode route reports no watch state.`
                  : `Another device reported this at ${clockTime(observed.at)} over the live channel, not a reading — the episode route reports no watch state.`}
              </span>
            </div>
          </div>
        )}
      </section>
    </div>
  )
}

/* ------------------------------------------------------------------ loading */

function EpisodeLoading() {
  return (
    <SkeletonRegion busy label="Loading episode …" className="u-episode">
      <Skeleton shape="text" lines={1} width="12rem" />
      <div className="u-episode__head">
        <span className="u-episode__still">
          <Skeleton shape="block" width="100%" style={{ aspectRatio: '16 / 9', height: 'auto' }} />
        </span>
        <Skeleton shape="text" lines={3} />
      </div>
    </SkeletonRegion>
  )
}

/* ----------------------------------------------------------------- helpers */

interface ObservedWatch {
  position_seconds: number
  played: boolean
  origin: 'this client' | 'another device'
  /** ISO 8601, from the write's `last_played_at` or the frame's `observed_at`. */
  at: string
}

/** "S01E04". Already formatted for display and never recomposed downstream. */
function episodeCode(season: number, episode: number): string {
  return `S${String(season).padStart(2, '0')}E${String(episode).padStart(2, '0')}`
}

function clockTime(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function copiesLine(targets: PlayTarget[]): string {
  const sources = new Set(targets.map((target) => target.source.id)).size
  const copies = `${targets.length} ${targets.length === 1 ? 'copy' : 'copies'}`
  return `${copies} across ${sources} ${sources === 1 ? 'source' : 'sources'}`
}

/**
 * `PlayTargetResponse` → the picker's `PlayTarget`. Field by field because
 * `exactOptionalPropertyTypes` distinguishes `{container: null}` from an absent
 * `container`, and the picker's contract says absent.
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
