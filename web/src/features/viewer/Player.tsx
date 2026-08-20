import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
  type RefObject,
} from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Badge,
  Button,
  Icon,
  IconButton,
  Skeleton,
  SkeletonRegion,
  StateBlock,
  TargetPicker,
  type PlayTarget as PickerTarget,
} from '@/design-system'
import {
  UsherProblem,
  streamPath,
  ticketOf,
  useEpisode,
  usePlayEpisode,
  usePlayTitle,
  useSetEpisodeWatchState,
  useSetTitleWatchState,
  useTitle,
  type Schemas,
} from '@/api'
import { episodePath, titlePath } from '@/app/routes'
import { NotFound, ScreenProblem } from '@/features/shared/NotFound'

type PlayTarget = Schemas['PlayTargetResponse']

/**
 * **The enforcement, and it is a type rather than a review comment.**
 *
 * Every function below that renders anything takes a `DisplayTarget`, which is
 * a `PlayTarget` with `url` removed. Writing `target.url` inside one of them is
 * a compile error, so there is no code path that can print the ticket — the
 * property is not on the type the view ever holds. The full target lives in one
 * variable at the top of this screen and is read by exactly two functions:
 * `streamOf`, which lifts the ticket out and re-issues it same-origin for the
 * media element, and `handOff`, which passes the deep link to the browser
 * without it ever becoming an `href`.
 *
 * This is `TargetPicker`'s own mechanism, repeated here so the rule holds on
 * both sides of the design-system boundary rather than only inside it.
 */
type DisplayTarget = Omit<PlayTarget, 'url'>

function withoutTicket(target: PlayTarget): DisplayTarget {
  // The rest element is the omission. `url` is bound here and read nowhere.
  const { url, ...display } = target
  void url
  return display
}

/** How long a minted ticket is good for, and why nothing caches a play response. */
const TICKET_TTL_SECONDS = 300

/** patterns.md §9: `Space` is play / pause on a player surface. */
const PLAY_PAUSE_KEY = ' '

/** The write cadence the sentence at the foot of this screen promises. */
const POSITION_WRITE_MS = 15_000

/** Selectors `Space` belongs to when one of them has focus. */
const INTERACTIVE = 'button, a[href], input, select, textarea, [contenteditable], [role="slider"]'

/**
 * Player and hand-off.
 *
 * Four outcomes are worth designing and three of them are not errors:
 *
 * · **Playing.** A `direct` target, played inline from `/stream/{ticket}`.
 * · **Handed off.** A `deep_link` target: the copy plays in another app, and
 *   the position comes back to Usher when that app reports it.
 * · **The ticket expired.** A ticket lasts 300 s, so a page left open over
 *   lunch is holding a dead one. That is an inline recovery — one tap
 *   re-requests and plays — and never an error page.
 * · **This browser cannot decode this copy.** Which is *not* the same fact as
 *   "playback broke", and telling those two apart is most of why this screen
 *   exists at all.
 */
export default function Player(): ReactElement {
  const { kind, id } = useParams()
  const navigate = useNavigate()
  const episode = kind === 'episode'

  /**
   * **Never cached, always re-requested.** The response body is a secret for as
   * long as it is valid and it stops being valid after `TICKET_TTL_SECONDS`,
   * which is why playback is a mutation and not a query with a `staleTime`:
   * there is no version of this answer worth keeping.
   */
  const playTitle = usePlayTitle()
  const playEpisode = usePlayEpisode()
  const active = episode ? playEpisode : playTitle
  const { mutate: mutateTitle } = playTitle
  const { mutate: mutateEpisode } = playEpisode

  const name = useMediaName(episode ? 'episode' : 'title', id)
  const writeTitle = useSetTitleWatchState()
  const writeEpisode = useSetEpisodeWatchState()
  const { mutate: mutateTitleWatch } = writeTitle
  const { mutate: mutateEpisodeWatch } = writeEpisode

  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [choiceKey, setChoiceKey] = useState<string | null>(null)
  const [undecodable, setUndecodable] = useState<readonly string[]>([])
  const [fault, setFault] = useState<Fault>(null)
  const [playing, setPlaying] = useState(false)
  const [position, setPosition] = useState(0)
  const [captionsOpen, setCaptionsOpen] = useState(false)
  const [targets, setTargets] = useState<PlayTarget[]>([])

  /**
   * The targets are kept in state and written from the mutation's own success
   * callback rather than read off `mutation.data`, and that is what makes the
   * expiry recovery **one tap** rather than a page that blinks.
   *
   * Calling `mutate` again clears `data` for the length of the round trip, so a
   * screen reading it directly would fall back to its skeleton mid-renewal,
   * throw away the position, and re-mount the player. Holding the last answer
   * until the next one arrives means the only thing that changes on screen is
   * the button's pending state.
   */
  const capture = useCallback((response: { targets: PlayTarget[] }) => {
    setTargets(response.targets)
    setFault(null)
  }, [])

  const request = useCallback(() => {
    if (id === undefined) return
    if (episode) mutateEpisode({ episodeId: id }, { onSuccess: capture })
    else mutateTitle({ titleId: id }, { onSuccess: capture })
  }, [capture, episode, id, mutateEpisode, mutateTitle])

  const started = useRef(false)
  useEffect(() => {
    if (started.current || id === undefined) return
    started.current = true
    request()
  }, [id, request])

  /**
   * The choice is held as a **key**, not as a target object.
   *
   * A renewal answers with the same copies carrying fresh tickets, and a screen
   * holding the old object would go on pointing at a dead one. Deriving the
   * target from the current response by key is what makes the recovery actually
   * recover.
   */
  const chosen = targets.find((target) => keyOf(target) === choiceKey) ?? preferred(targets, undecodable)
  const source = chosen === undefined ? null : streamOf(chosen)

  const seeded = useRef(false)
  useEffect(() => {
    if (seeded.current || chosen === undefined) return
    seeded.current = true
    setPosition(chosen.resume_position_seconds ?? 0)
  }, [chosen])

  const positionRef = useRef(position)
  useEffect(() => {
    positionRef.current = position
  }, [position])

  /** The sentence at the foot of this screen promises this, so it happens. */
  const writePosition = useCallback(
    (seconds: number) => {
      if (id === undefined || seconds <= 0) return
      const body = { position_seconds: Math.round(seconds), played: false }
      if (episode) mutateEpisodeWatch({ episodeId: id, body })
      else mutateTitleWatch({ titleId: id, body })
    },
    [episode, id, mutateEpisodeWatch, mutateTitleWatch],
  )

  useEffect(() => {
    if (!playing) return undefined
    const timer = setInterval(() => writePosition(positionRef.current), POSITION_WRITE_MS)
    return () => clearInterval(timer)
  }, [playing, writePosition])

  const toggle = useCallback(() => {
    setPlaying((current) => {
      if (current) writePosition(positionRef.current)
      return !current
    })
  }, [writePosition])

  /**
   * React state drives the element rather than the other way round, so the
   * control's label and the element cannot disagree — and the element's own
   * `play` / `pause` events feed back, so a browser that stops for its own
   * reasons (an autoplay policy, a phone call) still updates the label.
   */
  useEffect(() => {
    const video = videoRef.current
    if (video === null) return
    if (playing) {
      const attempt: unknown = video.play()
      if (attempt instanceof Promise) attempt.catch(() => undefined)
    } else {
      video.pause()
    }
  }, [playing, source])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== PLAY_PAUSE_KEY) return
      // Space belongs to whatever is focused when that thing uses it: a button
      // activates, a slider pages, a field types a space.
      if (event.target instanceof Element && event.target.closest(INTERACTIVE)) return
      event.preventDefault()
      toggle()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [toggle])

  const onMediaError = useCallback(() => {
    if (source === null || chosen === undefined) return
    const mediaError = videoRef.current?.error?.code ?? null
    setFault({ kind: 'probing' })
    const key = keyOf(chosen)
    void probeStream(source).then((outcome) => {
      if (outcome === 'ticket-invalid') {
        setFault({ kind: 'expired' })
        return
      }
      setUndecodable((current) => (current.includes(key) ? current : [...current, key]))
      setFault({ kind: 'decode', mediaError })
    })
  }, [chosen, source])

  const seek = useCallback((seconds: number) => {
    setPosition(seconds)
    const video = videoRef.current
    if (video === null) return
    try {
      video.currentTime = seconds
    } catch {
      // Some environments refuse a seek before metadata has arrived. The slider
      // is still the truth and the element catches up when it can.
    }
  }, [])

  /**
   * One tap: ask for a new ticket and carry on from the same second. The fault
   * is cleared by `capture` when the answer lands rather than here, so the
   * strip does not disappear and leave the reader looking at a dead element for
   * the length of the request.
   */
  const renew = useCallback(() => {
    setPlaying(true)
    request()
  }, [request])

  if (id === undefined || (kind !== 'title' && kind !== 'episode')) return <NotFound />

  const back = () => navigate(episode ? episodePath(id) : titlePath(id))

  if (active.isError) {
    const conflict = active.error instanceof UsherProblem && active.error.code === 'not_playable'
    return (
      <Frame>
        <PlayerHeading>{name ?? 'Playback'}</PlayerHeading>
        {/* `onRetry` is passed unconditionally and the closed vocabulary decides:
            `source_unavailable` gets the control, `not_playable` never does. */}
        <ScreenProblem
          error={active.error}
          instance={episode ? `/episodes/${id}/play` : `/titles/${id}/play`}
          onRetry={request}
        />
        {conflict && (
          <span className="flex flex-wrap items-center gap-3">
            <Button size="sm" variant="secondary" onClick={back}>
              See other copies
            </Button>
            <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
              409 gets no retry button. Retrying cannot conjure a file.
            </span>
          </span>
        )}
      </Frame>
    )
  }

  if (targets.length === 0 && !active.isSuccess) return <PlayerSkeleton />

  if (chosen === undefined) {
    return (
      <Frame>
        <PlayerHeading>{name ?? 'Playback'}</PlayerHeading>
        <StateBlock kind="empty" title="No copy came back" meta="targets: []">
          The request succeeded and the resolution returned no way to play this. Nothing went wrong with the
          request; there is nothing on the other end of it right now.
        </StateBlock>
      </Frame>
    )
  }

  const display = withoutTicket(chosen)
  const expired = fault?.kind === 'expired'

  return (
    <Frame>
      <PlayerHeading>{name ?? 'Playback'}</PlayerHeading>

      {display.kind === 'deep_link' ? (
        <HandOff target={display} onOpenAgain={() => handOff(chosen)} onBack={back} />
      ) : source === null ? (
        <NoTicket />
      ) : expired ? (
        <Expired position={position} onRenew={renew} pending={active.isPending} />
      ) : fault?.kind === 'decode' ? (
        <DecodeFailure target={display} mediaError={fault.mediaError} onBack={back} />
      ) : (
        <Stage>
          {/* The one place the ticket is used, and it is *used* rather than
              shown: a same-origin path with no host in it and no session token.
              Nothing reads it back out, nothing copies it, nothing links to it,
              and `redact.ts` removes it from the request journal. */}
          <video
            ref={videoRef}
            className="h-full w-full"
            src={source}
            preload="metadata"
            playsInline
            onError={onMediaError}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onTimeUpdate={(event) => setPosition(event.currentTarget.currentTime)}
          />
          {fault?.kind === 'probing' && (
            <span
              role="status"
              className="absolute inset-x-0 bottom-0 p-3 text-center"
              style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}
            >
              Playback stopped. Checking whether the bytes are arriving …
            </span>
          )}
        </Stage>
      )}

      {display.kind === 'direct' && source !== null && !expired && fault?.kind !== 'decode' && (
        <Controls
          target={display}
          playing={playing}
          position={position}
          captionsOpen={captionsOpen}
          videoRef={videoRef}
          onToggle={toggle}
          onSeek={seek}
          onCaptions={() => setCaptionsOpen((open) => !open)}
        />
      )}

      {/* Suppressed while the ticket is dead: every copy in that response
          carries the same expiry, so listing them would be offering four
          buttons that all fail the same way. The one tap is on the stage. */}
      {!expired && (
        <div className="flex flex-col gap-2">
          <span className="u-eyebrow">Playing from · switch copy without losing your place</span>
          <TargetPicker
            targets={targets.map(pickerTarget)}
            canDecode={(candidate) =>
              candidate.kind === 'deep_link' || !undecodable.includes(keyOf(candidate))
            }
            onPlay={(candidate) => {
              if (candidate.kind === 'deep_link') {
                handOff(candidate)
                return
              }
              setFault(null)
              setChoiceKey(keyOf(candidate))
              setPlaying(true)
            }}
          />
          <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>
            Position is written to Usher every 15 seconds and on pause, so another device picks it up. A
            playback link lasts {TICKET_TTL_SECONDS} seconds and is asked for again rather than kept.
          </span>
        </div>
      )}
    </Frame>
  )
}

/* ------------------------------------------------------------------ faults */

/**
 * What went wrong *after* a ticket was minted, which is a different question
 * from what the API answered.
 *
 * `probing` is a real state and not a transient to be hidden: separating the
 * two outcomes below costs a network round trip, and a screen that guessed and
 * then corrected itself would have told the user something false first.
 */
type Fault =
  | null
  | { readonly kind: 'probing' }
  | { readonly kind: 'expired' }
  | { readonly kind: 'decode'; readonly mediaError: number | null }

type ProbeOutcome = 'ticket-invalid' | 'bytes-flow' | 'unknown'

/**
 * Which half failed — measured, not guessed.
 *
 * `/stream/{ticket}` is **same-origin**, so its own status is readable: an
 * expired ticket is refused right there, before any redirect, and that 404 is
 * the one playback failure a probe can read for certain.
 *
 * A *valid* ticket answers `302` to the source, which is cross-origin and which
 * Usher ships no CORS for — so the probe's own request fails. **That failure is
 * the evidence.** The request left, the ticket redeemed, the redirect happened:
 * the byte path works and the decoder is what refused.
 *
 * No response header is read here and none could be. `content-range` is not
 * CORS-safelisted and the Emby upstream sends no `access-control-expose-headers`,
 * so a UI built on reading one would work under `curl` and nowhere else.
 */
async function probeStream(path: string): Promise<ProbeOutcome> {
  try {
    const response = await fetch(path, { headers: { range: 'bytes=0-1' } })
    if (response.status === 404) return 'ticket-invalid'
    return response.ok ? 'bytes-flow' : 'unknown'
  } catch {
    return 'bytes-flow'
  }
}

/* --------------------------------------------------------------- fragments */

function Frame({ children }: { children: ReactNode }): ReactElement {
  return (
    <div className="mx-auto flex w-full max-w-[980px] flex-col gap-4 px-4 py-6 tablet:px-6">{children}</div>
  )
}

function PlayerHeading({ children }: { children: ReactNode }): ReactElement {
  return <h1 style={{ font: 'var(--text-title-sm)', color: 'var(--text-primary)' }}>{children}</h1>
}

function Stage({ children }: { children: ReactNode }): ReactElement {
  return (
    <div
      className="relative grid place-items-center overflow-hidden"
      style={{
        aspectRatio: '16 / 9',
        background: 'var(--bg-letterbox)',
        borderRadius: 'var(--radius-card)',
      }}
    >
      {children}
    </div>
  )
}

function Controls({
  target,
  playing,
  position,
  captionsOpen,
  videoRef,
  onToggle,
  onSeek,
  onCaptions,
}: {
  target: DisplayTarget
  playing: boolean
  position: number
  captionsOpen: boolean
  videoRef: RefObject<HTMLVideoElement | null>
  onToggle: () => void
  onSeek: (seconds: number) => void
  onCaptions: () => void
}): ReactElement {
  const runtime = target.runtime_seconds ?? null

  return (
    <div className="flex flex-col gap-3">
      {runtime === null ? (
        <StateBlock kind="na">
          This copy reported no runtime, so there is no scrubber. Playback still works; the position has
          nothing to be a position within.
        </StateBlock>
      ) : (
        <div className="flex items-center gap-3">
          <span className="u-mono" style={{ color: 'var(--text-muted)' }}>
            {clock(position)}
          </span>
          <input
            type="range"
            className="flex-1"
            aria-label="Seek"
            min={0}
            max={runtime}
            step={1}
            value={Math.min(position, runtime)}
            onChange={(event) => onSeek(Number(event.currentTarget.value))}
            style={{ accentColor: 'var(--progress-fill)' }}
          />
          <span className="u-mono" style={{ color: 'var(--text-muted)' }}>
            -{clock(Math.max(runtime - position, 0))}
          </span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1">
        <IconButton
          label="Back 10 seconds"
          icon={<Icon name="rotate-ccw" size={20} />}
          outlined
          onClick={() => onSeek(Math.max(position - 10, 0))}
        />
        <IconButton
          label={playing ? 'Pause' : 'Play'}
          icon={<Icon name={playing ? 'pause' : 'play'} size={24} />}
          outlined
          touch
          onClick={onToggle}
        />
        <IconButton
          label="Forward 30 seconds"
          icon={<Icon name="rotate-cw" size={20} />}
          outlined
          onClick={() => onSeek(runtime === null ? position + 30 : Math.min(position + 30, runtime))}
        />
        <span className="ml-auto flex gap-1">
          {/* patterns.md §12: the captions control is present **even when no
              track exists**. "This copy has no subtitles" and "this player has
              no subtitle support" are different facts, and hiding the button
              would say the second one about a case that is the first. */}
          <IconButton
            label="Subtitles"
            icon={<Icon name="captions" size={20} />}
            outlined
            aria-expanded={captionsOpen}
            aria-controls="player-captions"
            onClick={onCaptions}
          />
          <IconButton
            label="Full screen"
            icon={<Icon name="maximize" size={20} />}
            outlined
            onClick={() => requestFullScreen(videoRef.current)}
          />
        </span>
      </div>

      <p
        id="player-captions"
        hidden={!captionsOpen}
        style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}
      >
        No subtitle track was supplied with this copy. A play target describes the video, the audio and the
        container and carries no subtitle streams at all, so there is nothing here to switch on — that is a
        gap in what the API reports, not an empty menu.
      </p>

      <Specs target={target} />
    </div>
  )
}

function Specs({ target }: { target: DisplayTarget }): ReactElement {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Badge mono outline>
        {specLine(target)}
      </Badge>
      {target.kind === 'deep_link' && (
        <Badge tone="info" mono>
          {target.scheme ?? 'external'}
        </Badge>
      )}
      <span style={{ font: 'var(--text-body-xs)', color: 'var(--text-muted)' }}>{target.source.name}</span>
    </div>
  )
}

/**
 * The 300-second ticket, expired, as one tap rather than as an error page.
 *
 * `code` and `status` are printed because patterns.md §3 requires them of every
 * rendered problem — an operator reading over a shoulder pastes them into a log
 * query — and the recovery above them is the only one there is: ask again.
 */
function Expired({
  position,
  onRenew,
  pending,
}: {
  position: number
  onRenew: () => void
  pending: boolean
}): ReactElement {
  return (
    <>
      <Stage>
        <div className="max-w-[420px] p-6 text-center">
          <h2 style={{ font: 'var(--text-title-sm)', color: 'var(--text-primary)' }}>That link expired</h2>
          <p className="mt-2" style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
            Playback links last five minutes. Asking again takes a second and picks up where you were.
          </p>
          <span className="mt-4 inline-flex">
            <Button
              variant="primary"
              iconLeft={<Icon name="play" size={20} />}
              loading={pending}
              loadingLabel="Asking for a new link…"
              onClick={onRenew}
            >
              Play again from {clock(position)}
            </Button>
          </span>
        </div>
      </Stage>
      <span className="u-mono" style={{ color: 'var(--text-muted)' }}>
        code ticket_invalid · HTTP 404 · one tap re-requests
      </span>
    </>
  )
}

/**
 * A decode refusal, told apart from a playback failure — and this is a measured
 * distinction rather than a hedge.
 *
 * Most of this catalog is mkv and no mainstream browser ships a Matroska
 * demuxer for HEVC, so the **correct** outcome of pressing play on one of those
 * copies is a `206` followed by a decode refusal: the network is fine, the
 * ticket redeemed, the bytes are arriving, and the decoder is what said no.
 * Without this screen that reads as a broken client, and the next thing
 * somebody does is go hunting for a bug in the transport.
 */
function DecodeFailure({
  target,
  mediaError,
  onBack,
}: {
  target: DisplayTarget
  mediaError: number | null
  onBack: () => void
}): ReactElement {
  const codec = target.video_codec ?? 'this video'
  const container = target.container ?? 'this container'

  return (
    <>
      <Stage>
        <div className="max-w-[460px] p-6 text-center">
          <span style={{ color: 'var(--warn-text)' }}>
            <Icon name="alert-triangle" size={24} />
          </span>
          <h2 className="mt-2" style={{ font: 'var(--text-title-sm)', color: 'var(--text-primary)' }}>
            Your browser can&apos;t decode this copy
          </h2>
          <p className="mt-2" style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
            The network is fine and the file is intact — the bytes arrived and this browser has no {codec}{' '}
            decoder for {container}. Another copy of the same title will play.
          </p>
          <span className="mt-4 inline-flex gap-2">
            <Button variant="secondary" onClick={onBack}>
              See other copies
            </Button>
          </span>
        </div>
      </Stage>
      <span className="u-mono" style={{ color: 'var(--text-muted)' }}>
        {specLine(target)} · {target.source.name} — the range probe reached the source and the decoder refused
        before the first frame
        {mediaError === null ? '' : ` · MediaError ${mediaError}`}
      </span>
    </>
  )
}

function HandOff({
  target,
  onOpenAgain,
  onBack,
}: {
  target: DisplayTarget
  onOpenAgain: () => void
  onBack: () => void
}): ReactElement {
  const app = target.scheme ?? 'the external player'
  return (
    <>
      <Stage>
        <div className="max-w-[460px] p-6 text-center">
          <Icon name="external-link" size={24} />
          <h2 className="mt-2" style={{ font: 'var(--text-title-sm)', color: 'var(--text-primary)' }}>
            Handed off to {app}
          </h2>
          <p className="mt-2" style={{ font: 'var(--text-body-sm)', color: 'var(--text-secondary)' }}>
            This copy plays there and not here. Your position will come back to Usher when {app} reports it —
            usually within ten seconds of you stopping.
          </p>
          <span className="mt-4 inline-flex gap-2">
            {/* A button, never an anchor. An `href` would put the ticket in the
                DOM, in the status bar on hover, and in "copy link address". */}
            <Button variant="secondary" onClick={onOpenAgain}>
              Open {app} again
            </Button>
            <Button variant="ghost" onClick={onBack}>
              Back to the title
            </Button>
          </span>
        </div>
      </Stage>
      <Specs target={target} />
    </>
  )
}

/** A target whose URL carries no ticket is reported rather than played. */
function NoTicket(): ReactElement {
  return (
    <Stage>
      <span className="max-w-[460px] p-6 text-center">
        <StateBlock kind="na">
          This copy came back without a playback ticket in it, so it is reported rather than played. The only
          other thing that address could be is a source URL carrying a session token.
        </StateBlock>
      </span>
    </Stage>
  )
}

/** Shaped like the stage that is coming. `/play` is measured at 1–5 s. */
function PlayerSkeleton(): ReactElement {
  return (
    <SkeletonRegion
      busy
      label="Finding copies of this …"
      className="mx-auto flex w-full max-w-[980px] flex-col gap-4 px-4 py-6 tablet:px-6"
    >
      <Skeleton shape="block" width={240} height={24} />
      <Skeleton shape="block" height={360} style={{ borderRadius: 'var(--radius-card)' }} />
      <Skeleton shape="text" lines={2} />
    </SkeletonRegion>
  )
}

/* ----------------------------------------------------------------- helpers */

/**
 * The title's or episode's own name, for the heading.
 *
 * Its failure is deliberately swallowed: a play route that cannot read the
 * catalog record still holds a working ticket, and replacing a working player
 * with a 404 because the *name* did not load would be the wrong screen.
 */
function useMediaName(kind: 'title' | 'episode', id: string | undefined): string | null {
  const title = useTitle(kind === 'title' ? id : undefined)
  const episode = useEpisode(kind === 'episode' ? id : undefined)
  if (kind === 'title') return title.data?.name ?? null
  return episode.data?.name ?? null
}

/**
 * The ticket, lifted out of the target's absolute URL and re-issued
 * same-origin.
 *
 * `target.url` names a host — the one Usher read off the request's `Host`
 * header — and the only host this document may assume is its own. It is also
 * *cross-origin* after the `302`, and Usher ships no CORS, so handing it to a
 * media element or a probe verbatim is less a leak than a thing that does not
 * work.
 *
 * A target whose URL carries no `/stream/{…}` segment is **reported rather than
 * played**: the only other thing that address could be is a source URL with
 * somebody's session token in it.
 */
function streamOf(target: PlayTarget): string | null {
  const ticket = ticketOf(target.url)
  return ticket === null ? null : streamPath(ticket)
}

/**
 * Hands the deep link to the browser without it ever becoming an `href`.
 *
 * The scheme's URL has to be the absolute one — the other app is a different
 * process and cannot resolve this document's origin — so it is passed to
 * `location.assign` and never rendered, never logged and never linked. The
 * `try` is because a scheme with no registered handler throws in some browsers,
 * and a hand-off that fails must not take the screen down with it.
 */
function handOff(target: { readonly url: string }): void {
  try {
    window.location.assign(target.url)
  } catch {
    // Nothing to say that the screen does not already say: this copy plays in
    // another app, and that app is not installed.
  }
}

function requestFullScreen(video: HTMLVideoElement | null): void {
  const element = video?.parentElement ?? video
  if (element && typeof element.requestFullscreen === 'function') {
    void element.requestFullscreen().catch(() => undefined)
  }
}

/**
 * The API's DTO onto the picker's props, which is what the `features/` layer is
 * for: the design system declares `scheme?: string` and the wire sends
 * `scheme: string | null`, and under `exactOptionalPropertyTypes` those are
 * different types on purpose. A `null` is dropped rather than passed through,
 * because the component's contract says "absent" and the wire says "we looked
 * and there is nothing" — collapsing the two is the §2 mistake in miniature.
 *
 * `url` crosses this boundary because `TargetPicker` needs it to hand back to
 * `onPlay`, and it is safe: that component's own render functions are built on
 * a type the property has been removed from.
 */
function pickerTarget(target: PlayTarget): PickerTarget {
  const picker: PickerTarget = { kind: target.kind, url: target.url, source: target.source }
  if (target.scheme != null) picker.scheme = target.scheme
  if (target.container != null) picker.container = target.container
  if (target.video_codec != null) picker.video_codec = target.video_codec
  if (target.audio != null) picker.audio = target.audio
  if (target.hdr_format != null) picker.hdr_format = target.hdr_format
  if (target.resolution != null) picker.resolution = target.resolution
  if (target.runtime_seconds != null) picker.runtime_seconds = target.runtime_seconds
  if (target.resume_position_seconds != null) {
    picker.resume_position_seconds = target.resume_position_seconds
  }
  return picker
}

/**
 * The fields a copy is identified by. Structural rather than one of the two
 * concrete target types, so the same key can be computed from the API's DTO and
 * from the object `TargetPicker` hands back — which is how a click in the
 * picker selects the *API's* target rather than a copy of it.
 */
interface TargetIdentity {
  kind: 'direct' | 'deep_link'
  source: { id: string }
  container?: string | null
  video_codec?: string | null
  resolution?: string | null
}

/**
 * Identity for a copy, stable across a renewal.
 *
 * Deliberately not the URL: the URL carries the ticket, and the ticket is
 * exactly the part that changes when the response is asked for again.
 */
function keyOf(target: TargetIdentity): string {
  return [
    target.source.id,
    target.kind,
    target.container ?? '',
    target.video_codec ?? '',
    target.resolution ?? '',
  ].join('|')
}

/**
 * Which copy to open with: a direct one, and never one this session has already
 * watched a decoder refuse. That refusal is **measured** rather than predicted,
 * which is why nothing here asks `canPlayType` and guesses — a browser that
 * answers `""` for everything it has not been asked about would have this
 * screen declaring half the library unplayable before trying any of it.
 */
function preferred(targets: PlayTarget[], undecodable: readonly string[]): PlayTarget | undefined {
  return (
    targets.find((target) => target.kind === 'direct' && !undecodable.includes(keyOf(target))) ??
    targets.find((target) => target.kind === 'direct') ??
    targets[0]
  )
}

/** "2160p · HDR10 · HEVC · MKV · DTS-HD MA 5.1". There is no quality string on the wire. */
function specLine(target: DisplayTarget): string {
  return [target.resolution, target.hdr_format, target.video_codec, target.container, target.audio]
    .filter((value): value is string => typeof value === 'string' && value.length > 0)
    .join(' · ')
}

/** `1:09:40`, and `9:40` when there is no hour to show. */
function clock(seconds: number): string {
  const whole = Math.max(Math.floor(seconds), 0)
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const rest = whole % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`
}
