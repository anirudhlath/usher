import type { ReactElement } from 'react'
import clsx from 'clsx'
import { Button } from '../actions'
import { Badge } from '../status'

/**
 * The play-target picker. `POST /titles/{id}/play` returns `{targets:[…]}`, never empty on a 200,
 * in copy order, across every source that holds the title. The server does not pick a winner.
 *
 * Hard rules:
 * · Every `url` is a **ticket valid for 300 s**. Never cache a play response; re-request on every
 *   press. Never render, copy, share or log the URL — they are secrets that redirect to a
 *   credential-bearing source URL. This component never exposes `url`.
 * · There is no quality string. Compose "2160p · HDR10 · HEVC · MKV" from the target's fields.
 * · An expired ticket (404 ticket_invalid) is a one-tap recovery, not an error page.
 * · /play is the slowest route in the app (one adapter per copy against a 1–5 s upstream) — the
 *   trigger needs a real pending state.
 */
export interface PlayTarget {
  kind: 'direct' | 'deep_link'
  /** Short-lived ticket. Never displayed. */
  url: string
  source: { id: string; name: string }
  scheme?: string
  container?: string
  video_codec?: string
  audio?: string
  hdr_format?: string
  resolution?: string
  runtime_seconds?: number
  resume_position_seconds?: number
}

export interface TargetPickerProps {
  targets: PlayTarget[]
  onPlay?: (t: PlayTarget) => void
  /** Browser decode probe. Defaults to "direct targets only". */
  canDecode?: (t: PlayTarget) => boolean
  /** 404 ticket_invalid → one-tap re-request. */
  expired?: boolean
  onRetryTicket?: () => void
  /** Collapses a single obvious target into one Play button. */
  compact?: boolean
}

/**
 * **The enforcement.** Everything this component renders is built from a `DisplayTarget`, which is
 * `PlayTarget` with `url` removed. There is therefore no code path that can print the ticket: the
 * property does not exist on the type the render functions receive, so `target.url` inside them is
 * a compile error rather than a review finding. The full `PlayTarget` is held only to hand back to
 * `onPlay`, which is the caller's own object.
 */
export type DisplayTarget = Omit<PlayTarget, 'url'>

function withoutTicket(target: PlayTarget): DisplayTarget {
  // The rest element is the omission. `url` is bound here and read nowhere.
  const { url, ...display } = target
  void url
  return display
}

/** "2160p · HDR10 · HEVC · MKV". There is no quality string on the wire; it is composed. */
function specs(target: DisplayTarget): string[] {
  return [target.resolution, target.hdr_format, target.video_codec, target.container].filter(
    (value): value is string => Boolean(value),
  )
}

function actionWord(target: DisplayTarget): string {
  if (target.kind === 'deep_link') return 'Hand off'
  return (target.resume_position_seconds ?? 0) > 0 ? 'Resume' : 'Play'
}

function accessibleName(target: DisplayTarget, decodable: boolean): string {
  const parts = [...specs(target), target.audio].filter((value): value is string => Boolean(value))
  const base = `${actionWord(target)} ${parts.join(' · ')} from ${target.source.name}`
  return decodable ? base : `${base} — your browser can't decode this`
}

export function TargetPicker({
  targets,
  onPlay,
  canDecode,
  expired = false,
  onRetryTicket,
  compact = false,
}: TargetPickerProps): ReactElement {
  const decodable = (target: PlayTarget): boolean =>
    canDecode ? canDecode(target) : target.kind === 'direct'

  if (expired) {
    return (
      <div className="u-ticket" role="status">
        <span>That link expired.</span>
        <Button type="button" variant="secondary" size="sm" onClick={onRetryTicket}>
          Play again
        </Button>
      </div>
    )
  }

  const only = targets[0]
  if (compact && targets.length === 1 && only) {
    const display = withoutTicket(only)
    // The specs ride in `iconRight` rather than in the children: `Button` wraps its children in
    // one element, and `.u-btn`'s flex `gap` is the only thing separating the action word from
    // the specs — inside that wrapper they would butt up against each other.
    return (
      <Button
        type="button"
        variant="primary"
        onClick={() => onPlay?.(only)}
        iconRight={<span className="u-target__compact-specs">{specs(display).slice(0, 2).join(' · ')}</span>}
      >
        {actionWord(display)}
      </Button>
    )
  }

  return (
    <div className="u-targets" role="group" aria-label="Playback options">
      {targets.map((target, index) => {
        const display = withoutTicket(target)
        const ok = decodable(target)
        const resume = display.resume_position_seconds ?? 0
        return (
          <button
            key={`${display.source.id}-${index}`}
            type="button"
            className={clsx(
              'u-target',
              index === 0 && ok && 'u-target--best',
              !ok && 'u-target--undecodable',
            )}
            aria-label={accessibleName(display, ok)}
            onClick={() => onPlay?.(target)}
          >
            <span className="u-target__body">
              <span className="u-target__specs">
                {specs(display).map((value) => (
                  <span key={value}>{value}</span>
                ))}
                {display.audio && <span className="u-target__audio">{display.audio}</span>}
              </span>
              <span className="u-target__src">
                <span>{display.source.name}</span>
                {display.kind === 'deep_link' && (
                  <Badge tone="info" mono>
                    {display.scheme ?? 'external'}
                  </Badge>
                )}
                {!ok && <span className="u-target__undecodable">your browser can&apos;t decode this</span>}
              </span>
            </span>
            <span className="u-target__trail">
              {resume > 0 && <span className="u-target__resume">resume {Math.floor(resume / 60)}m</span>}
              {/* Deliberately a span wearing the button's clothes, and deliberately not `Button`.
                  The whole row is already the control; a real button here would be a second focus
                  stop inside it and invalid nesting besides. `aria-hidden` keeps the word out of
                  the accessible name, which `aria-label` already supplies in full. */}
              <span className="u-btn u-btn--secondary u-btn--sm" aria-hidden="true">
                {actionWord(display)}
              </span>
            </span>
          </button>
        )
      })}
    </div>
  )
}
