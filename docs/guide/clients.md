# Building a client

Usher's HTTP API is enough to build a full media browser. The web console in
`web/` is one client, and it uses nothing a client of your own couldn't.

- **Reference:** the running server describes itself at `/openapi.json`, with an
  interactive view at `/docs`. The console generates its TypeScript types from
  that document.
- **Design:** [PRD 07](../prd/07-client-api.md) states what each operation
  promises.
- **No authentication:** no route checks a credential. See the README's
  [Security section](../../README.md#security).

## The API

| Group | Operations |
|---|---|
| **Screens** | `GET /home`, `GET /search`, `GET /search/suggest`, `GET /browse` |
| **Resources** | `GET /titles/{id}`, `GET /titles/{id}/similar`, `GET /series/{id}/seasons`, `GET /seasons/{id}/episodes`, `GET /episodes/{id}`, `GET /people/{id}`, `GET /collections/{id}`, `GET /images/{id}` |
| **Actions** | `POST /titles/{id}/play`, `POST /episodes/{id}/play`, `GET /stream/{ticket}`, `PUT /watch/titles/{id}`, `PUT /watch/episodes/{id}`, `POST` and `DELETE /watch/titles/{id}/played` |
| **Admin** | `POST` and `GET /admin/sources`, `GET /admin/sources/{id}/status`, `DELETE /admin/sources/{id}`, `POST /admin/sources/{id}/sync`, `GET /admin/unmatched`, `POST /admin/unmatched/{id}/resolve`, `GET /admin/bootstrap/status`, `POST /admin/bootstrap/{phase}`, `GET /admin/rows/providers`, `PUT /admin/rows/providers/{slug}`, `POST /admin/rows/regenerate` |
| **Meta** | `GET /health`, `GET /health/ready`, `GET /meta/attribution`, `GET /events`, `GET /openapi.json` |

- **`GET /home` paints a whole screen in one request.** It returns every row,
  with its cards, and no cursor.
- **Lists page with keyset cursors, never offsets.** A paged response carries
  `next_cursor`, which is `null` on the last page. Pass it back as `cursor` for
  the next page.
- **`GET /events` streams changes** as server-sent events, so a client can
  refresh a title when it changes instead of polling.
- **Titles are identified by Usher's own ids.** TMDb and IMDb ids are attributes
  of a title, not identifiers you can put in a URL.
- **Artwork comes from `GET /images/{id}`,** which fetches the image once,
  caches it, and serves it from then on.

## Errors

Every failure is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem
document (`application/problem+json`), carrying a `code` from a closed
vocabulary:

`not_found` · `validation_failed` · `method_not_allowed` · `invalid_cursor` ·
`source_unavailable` · `not_playable` · `ticket_invalid`

Branch on `code`, not on the human-readable `title` or `detail`. Two routes
answer outside this envelope: `GET /health/ready`, and `GET /events` once its
stream has started. A malformed `?titles=` on `/events` is still refused with a
problem document.

## Playback

`POST /titles/{id}/play` (or `/episodes/{id}/play`) returns `targets`, a ranked
list of ways to play the title. Each target carries a `kind` (`direct` or
`deep_link`), a `url`, the stream's container, codecs, resolution and HDR format,
a resume position, and the source it comes from.

**The `url` is a ticket, and you must treat it as a secret.** A ticket expires
after 300 seconds. Following it is a `302` to the real stream, and that
redirect's `Location` carries the media server's session token. The ticket keeps
the token out of the play response, but whoever follows it gets the token. The
token stays valid for as long as the media server keeps that session, long
after the ticket expires, and rotating `USHER_SECRET_KEY` invalidates tickets,
not tokens already handed out. Never log a ticket or its redirect, never display
either, and never put one where a screenshot could capture it.

A `deep_link` target hands the ticket to a third-party player, which follows
the redirect and then holds the real URL itself.

## Attribution

`GET /meta/attribution` returns four `{source, text}` entries: IMDb, TMDb,
Wikidata and MovieLens. **Display all four.** The list is the same on every
deployment, whether or not a dataset has been imported, because withholding one
would breach its licence.

**TMDb also requires its logo.** A string can't carry it, and Usher does not
ship it, so your client must display it itself. That is a condition of TMDb's
licence. [PRD 04](../prd/04-catalog-bootstrap.md) has every dataset's terms.

## Versioning

Usher is `0.x`, and the wire contract can still change between minor versions.
Pin the minor version your client was built against. The
[changelog](../../CHANGELOG.md) records every change.
