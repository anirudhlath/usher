/**
 * `POST /titles/{id}/play` and `POST /episodes/{id}/play`.
 *
 * **This response body is a secret for as long as the ticket is valid**, which
 * is 300 seconds. `PlayTargetResponse.url` is already a ticket URL on Usher's
 * own origin — `http://<usher>/stream/<fernet ticket>` — and the `deep_link`
 * arm is that same URL percent-encoded into an `infuse://x-callback-url/play`.
 * Neither carries a session token: the token appears only in the `302 Location`
 * that `/stream/{ticket}` answers with, which the browser follows and the page
 * never sees.
 *
 * The two arms below exist so `client.test.ts` can prove both are redacted out
 * of the request journal. A `.includes('/stream/')` check catches the first and
 * silently misses the second, where the separators arrive as `%2F` — which is
 * the bug the deep-link fixture is here to fail on.
 */

import type { PlayResponse } from '@/api'
import { PLAYBACK_TICKET, SOURCE_LIVING_ROOM } from './ids'

/** Where Usher believes it is reachable — read off the request's `Host` header. */
const USHER_ORIGIN = 'http://192.168.50.158:8100'

export const directTicketUrl = `${USHER_ORIGIN}/stream/${PLAYBACK_TICKET}`

export const deepLinkUrl = `infuse://x-callback-url/play?url=${encodeURIComponent(directTicketUrl)}`

export const playTargets: PlayResponse = {
  targets: [
    {
      kind: 'direct',
      url: directTicketUrl,
      scheme: 'http',
      container: 'mkv',
      video_codec: 'hevc',
      audio: 'ac3',
      hdr_format: 'HDR10',
      resolution: '3840x2160',
      runtime_seconds: 9_720,
      resume_position_seconds: 3_142,
      source: { id: SOURCE_LIVING_ROOM, name: 'Living Room Emby' },
    },
    {
      kind: 'deep_link',
      url: deepLinkUrl,
      scheme: 'infuse',
      container: 'mkv',
      video_codec: 'hevc',
      audio: 'ac3',
      hdr_format: 'HDR10',
      resolution: '3840x2160',
      runtime_seconds: 9_720,
      resume_position_seconds: 3_142,
      source: { id: SOURCE_LIVING_ROOM, name: 'Living Room Emby' },
    },
  ],
}

/**
 * An episode play. `resume_position_seconds: null` means the episode has never
 * been started — not that it should start at zero, which happens to be the same
 * behaviour and is a different claim.
 */
export const playEpisodeTargets: PlayResponse = {
  targets: [
    {
      kind: 'direct',
      url: `${USHER_ORIGIN}/stream/${PLAYBACK_TICKET}`,
      scheme: 'http',
      container: 'mkv',
      video_codec: 'h264',
      audio: 'aac',
      hdr_format: null,
      resolution: '1920x1080',
      runtime_seconds: 5_640,
      resume_position_seconds: null,
      source: { id: SOURCE_LIVING_ROOM, name: 'Living Room Emby' },
    },
  ],
}

/**
 * A 200 with no targets. Distinct from `not_playable`, which is a 409: this
 * says the request succeeded and the household has no reachable copy right now.
 */
export const playNoTargets: PlayResponse = { targets: [] }
