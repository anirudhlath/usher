import { TargetPicker, type PlayTarget } from '@/design-system'
import { GroupSection, Specimen } from '../Specimen'

/**
 * `url` is required by the contract and is a 300-second secret. It is a fixture
 * placeholder here and it does not matter what it says: `TargetPicker` renders
 * everything from a `DisplayTarget`, which is `PlayTarget` with `url` removed, so
 * there is no code path in the component that could print it.
 *
 * `hdr_format` is absent on the 1080p copy rather than null: the contract types it
 * `string | undefined`, and `exactOptionalPropertyTypes` makes the difference real.
 */
const BEST: PlayTarget = {
  kind: 'direct',
  url: 'ticket://kit',
  source: { id: 'kit-living-room', name: 'Living Room' },
  container: 'MKV',
  video_codec: 'HEVC',
  audio: 'TrueHD 7.1',
  hdr_format: 'HDR10',
  resolution: '2160p',
  runtime_seconds: 9660,
  resume_position_seconds: 0,
}

const RESUMABLE: PlayTarget = {
  kind: 'direct',
  url: 'ticket://kit',
  source: { id: 'kit-living-room', name: 'Living Room' },
  container: 'MP4',
  video_codec: 'H264',
  audio: 'AAC 5.1',
  resolution: '1080p',
  runtime_seconds: 9660,
  resume_position_seconds: 4100,
}

const HANDOFF: PlayTarget = {
  kind: 'deep_link',
  url: 'ticket://kit',
  scheme: 'infuse',
  source: { id: 'kit-attic', name: 'Attic' },
  container: 'MKV',
  video_codec: 'HEVC',
  hdr_format: 'Dolby Vision',
  resolution: '2160p',
  runtime_seconds: 9660,
}

const UNDECODABLE: PlayTarget = {
  kind: 'direct',
  url: 'ticket://kit',
  source: { id: 'kit-attic', name: 'Attic' },
  container: 'MKV',
  video_codec: 'AV1',
  audio: 'Opus 5.1',
  hdr_format: 'HDR10+',
  resolution: '2160p',
  runtime_seconds: 9660,
}

/** The browser decode probe the screens pass. Fixed, so the rendering is fixed. */
const canDecode = (target: PlayTarget): boolean => target.kind === 'direct' && target.video_codec !== 'AV1'

export function PlaybackSpecimens() {
  return (
    <GroupSection
      id="playback"
      title="Playback"
      blurb="POST /play returns every copy across every source, in copy order, and the server does not pick a winner. There is no quality string on the wire — “2160p · HDR10 · HEVC · MKV” is composed — and no code path here can print a ticket URL."
    >
      <Specimen
        name="TargetPicker/targets"
        wide
        note="Four copies across two servers. The first decodable one is marked best, a deep link is badged with its scheme and says “Hand off”, and an undecodable copy stays visible and dimmed with the reason in words."
      >
        <div className="k-fill">
          <TargetPicker targets={[BEST, RESUMABLE, HANDOFF, UNDECODABLE]} canDecode={canDecode} />
        </div>
      </Specimen>

      <Specimen
        name="TargetPicker/resume"
        wide
        note="A copy with a resume position says “Resume”, not “Play”."
      >
        <div className="k-fill">
          <TargetPicker targets={[RESUMABLE]} canDecode={canDecode} />
        </div>
      </Specimen>

      <Specimen
        name="TargetPicker/deep-link"
        wide
        note="Nothing direct on offer: the only copy hands off to an external player."
      >
        <div className="k-fill">
          <TargetPicker targets={[HANDOFF]} canDecode={canDecode} />
        </div>
      </Specimen>

      <Specimen name="TargetPicker/compact" note="Collapses a single obvious copy into one Play button.">
        <TargetPicker targets={[BEST]} compact />
      </Specimen>

      <Specimen
        name="TargetPicker/expired"
        width={420}
        note="404 ticket_invalid. One tap re-requests /play and plays — never an error page, and never a cached ticket."
      >
        <div className="k-fill">
          <TargetPicker targets={[BEST]} expired />
        </div>
      </Specimen>
    </GroupSection>
  )
}
