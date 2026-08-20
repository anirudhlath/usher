/**
 * The one recursive redactor, and the two things it removes.
 *
 * It lives in its own module rather than inside `client.ts` for one mechanical
 * reason: `devlog.ts` has to apply it at the moment a record is created, and
 * `client.ts` already imports `devlog.ts`. A redactor owned by the client would
 * make that a cycle, and — more to the point — would only protect the calls
 * that happen to go through the client. Anything that writes to the journal
 * gets redacted, not just the requests we remembered to redact.
 */

/**
 * Field names whose values never reach the journal.
 *
 * `POST /admin/sources` takes a real Emby `password`, and without this the
 * create-source form would write that credential into the in-memory journal
 * and render it verbatim in the dev drawer -- a drawer whose entire purpose
 * is to be read, and screenshotted, and pasted into a bug report.
 *
 * This is the client-side half of the rule Usher enforces on its own side by
 * making every secret a `pydantic.SecretStr`: the credential is redacted at
 * the boundary where it would otherwise be recorded, not at the point of use.
 */
export const REDACTED_KEYS = /^(password|api_key|token|secret|credential|authorization)$/i

/** What a redacted credential reads as. */
export const REDACTED = '<redacted>'

/**
 * patterns.md §13's exact string, used verbatim because the copy is the point:
 * it says *what* was removed and *how long it would have been valid*, so a
 * reader of the drawer knows they are not looking at a truncation bug.
 *
 * "Playback ticket URLs are secrets. No copy button, no share affordance, no
 * visible URL, no logging." The journal is logging.
 */
export const PLAYBACK_TICKET_PLACEHOLDER = '«redacted — 300 s playback ticket»'

/**
 * A `/stream/{ticket}` segment, in either of the two spellings it reaches us in.
 *
 * `POST /play` answers targets whose `url` is an absolute `…/stream/{ticket}`,
 * and the `deep_link` arm wraps that same URL **percent-encoded** into an
 * `infuse://x-callback-url/play?url=…`. So a plain `.includes('/stream/')`
 * catches the first arm and silently misses the second, where the separators
 * arrive as `%2F`. Both arms carry the same ticket; both have to match.
 */
const TICKET_URL = /(?:\/|%2f)stream(?:\/|%2f)[^/?#&\s]/i

/**
 * The deep-link scheme, matched on its own as well.
 *
 * `infuse:` appears in exactly one place in this API and it is always wrapping
 * a ticket, so a deep link whose inner URL is encoded some way this file did
 * not anticipate still never reaches the journal.
 */
const INFUSE_DEEP_LINK = /^infuse:/i

/**
 * Keys that are a ticket URL by contract rather than by inspection.
 *
 * `url` is unambiguous here: `PlayTargetResponse.url` is the only member named
 * `url` anywhere in `schema.d.ts` (a source's is `base_url`), so keying on it
 * cannot over-redact something harmless. `deep_link` is not a member name today
 * — the schema spells that arm as `kind: "deep_link"` on the same `url` field —
 * and it is here so that if the DTO ever splits into two fields, the new one is
 * already redacted rather than leaking on the release that introduces it.
 */
const TICKET_KEYS = /^(url|deep_link)$/i

function redactString(value: string): string {
  return TICKET_URL.test(value) || INFUSE_DEEP_LINK.test(value) ? PLAYBACK_TICKET_PLACEHOLDER : value
}

function redactEntry(key: string, value: unknown): unknown {
  if (REDACTED_KEYS.test(key)) return REDACTED
  if (TICKET_KEYS.test(key) && typeof value === 'string') return PLAYBACK_TICKET_PLACEHOLDER
  return redact(value)
}

/**
 * Idempotent: neither placeholder matches either rule, so redacting an already
 * redacted structure is a no-op. That is what lets `client.ts` redact a request
 * body on the way past and `devlog.record` redact it again at the boundary
 * without the second pass mangling the first.
 */
export function redact(value: unknown): unknown {
  if (typeof value === 'string') return redactString(value)
  if (Array.isArray(value)) return value.map(redact)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, entry]: [string, unknown]) => [key, redactEntry(key, entry)]),
    )
  }
  return value
}

/**
 * A request path with any ticket segment removed but the route left legible.
 *
 * `GET /stream/{ticket}` puts the secret in the *path*, so redacting only
 * bodies would journal it in the column the drawer shows first. Replacing the
 * whole path would cost the journal the one thing it exists to show — which
 * operation was called — so only the segment after `/stream/` is replaced.
 */
export function redactPath(path: string): string {
  return path.replace(/(\/stream\/)[^/?#]+/i, `$1${PLAYBACK_TICKET_PLACEHOLDER}`)
}
