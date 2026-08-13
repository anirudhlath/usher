"""Response shape for `POST /admin/rows/regenerate` (PRD 07).

`api/dto/` types are distinct from `domain/` models (PRD 07): the wire
contract is versioned independently. Here the split earns its keep by
*subtraction* -- the route writes a whole `Job` row and this type carries two
of its columns. Which two is the argument below; a *count* of the rest is
deliberately not stated, because it is a number derived from `Job`'s field
list that nothing would re-derive when that list grows.

**Two fields, because `(kind, key)` is the only thing about the row that is
still true when the response is read.** The queue deduplicates on that pair
and nothing else, so it is what locates the row (`SELECT * FROM jobs WHERE
kind = 'curate' AND key = '...'`) and it is what an operator needs. Every
other column is a fact with a shorter lifetime than the response:

- **`status` is not here**, because the row can be `pending` when the handler
  returns and `running` before the bytes arrive -- and `complete()` deletes
  it, so "the status of the job you enqueued" is very often "there is no such
  row", which is the *success* case. A field whose commonest value is a
  contradiction is worse than an absent one.
- **`priority` is not here**, and it is the one that would actively lie. The
  route sends `DEMAND` and never reads the row back -- it discards `enqueue`'s
  return value and issues no `SELECT` -- so any priority in the body would be
  the number it *asked for*, printed as though it were the number stored.
  `_ENQUEUE` carries `WHERE jobs.status <> 'parked'`, so a parked row is the
  case where the two genuinely diverge: it keeps the priority it parked at
  while this request writes nothing. The sharpest form of that -- `100` over a
  row still sitting at `('parked', 20)` -- needs a **future** background
  enqueue at a lower rung to produce it. That pairing is measured against real
  Postgres and the table is in `usher.domain.jobs.JobKind.CURATE`, but nothing
  in `src/` produces it today: `POST /admin/rows/regenerate` is the only site
  that enqueues this kind and it enqueues at the top of the scale, so a
  `curate` row parks at `('parked', 100)` and the lie is the quieter one --
  the right number, read from nowhere.
- **`written` is not here**, and `usher.domain.jobs.JobKind.CURATE` says why
  at length: `enqueue` returns 1 both for a job it created and for one it
  merely promoted, and 0 for a repeat already at this priority. The number
  cannot answer "did my request do anything", so publishing it invites
  exactly the reading it cannot support.

**Both fields are computed entirely from the request, so the body reports
nothing *about the queue*** -- `kind` is a constant of this route and `key` is
the dependency's answer, and neither is read back from the write. An empty 202
was therefore a real option. What the pair earns is the half a caller did not
send: this route takes no body, so `key` is how a client learns which
household `DefaultUserIdDep` resolved, and PRD 07 specifies the enqueued key
as the response. It is a receipt for what was asked on the caller's behalf,
not a report on what the write did -- which is the same reason every column
above is absent, arriving from the other direction.

**`key` is a `str` and not a `uuid.UUID`, matching the column rather than the
value.** `jobs.key` is *one column, three kinds of identifier* (`Job.key`) --
a title id, a source's own `external_id`, or a household -- and this response
hands back the queue's handle, not a household identifier a client should
route on. Typing it `uuid.UUID` would put `"format": "uuid"` in
`/openapi.json` and invite a generated client to treat it as an entity id;
PRD 07's surface has no user-scoped endpoint to point it at.
"""

from pydantic import BaseModel

from usher.domain.jobs import JobKind


class RegenerateResponse(BaseModel):
    """The enqueued job's identity. See the module docstring for what is
    deliberately absent, and why each of those would misstate the queue."""

    kind: JobKind
    key: str


class RowProviderResponse(BaseModel):
    """One registered row provider, and whether it composes (PRD 07, E2).

    **Two fields, and the list this appears in is the *registry* left-joined
    onto `row_provider_settings`** -- so there is an entry for every provider
    whether or not anybody has ever touched it, and `enabled` is `true` for the
    ones nobody has. That table ships empty and is never seeded (PRD 09 item 9),
    so on a virgin database this endpoint answers ten entries all reading
    `true`, which is the same thing *"providers are enabled by registration in
    code"* has always meant -- now visible, and now changeable.

    **`slug` is `RowProvider.slug_prefix` and never the class name.** It is the
    identifier that already lives outside the codebase: `usher home`'s leftmost
    column and `usher.row.build.duration`'s `provider` label both carry it, and
    `ports/rows.py` calls it *"declared rather than derived"* for exactly this
    reason. A class rename must not silently re-enable a provider somebody
    turned off.

    **`updated_at` is not here**, though the column exists. It records when an
    operator last touched the row, and two thirds of this list has no row at
    all -- so the field would be `null` for every provider nobody has
    configured, which is indistinguishable from a provider whose row exists and
    whose timestamp failed to write. A column that is absent for the common
    case is not a field, it is a second endpoint's worth of question.

    **No `title`, no `description` and no `family`.** A provider's human name
    is `Row.title`, which is a property of the *rows it builds* rather than of
    the provider -- `because-you-watched-<seed>` mints one per seed -- and
    `family` is the key the composer's diversity constraints are stated in,
    which `api/dto/home.py` already declines to publish for the same reason.
    """

    slug: str
    enabled: bool


class RowProviderUpdate(BaseModel):
    """The whole body of `PUT /admin/rows/providers/{slug}`.

    **One field, because the slug is the path and everything else about a
    provider is code.** There is no `reason`, no `until` and no `user_id`: a
    toggle is deployment-wide (there is one household, PRD 01's authentication
    seam) and a scheduled re-enable would be a scheduler this milestone
    deliberately does not build.

    **`PUT {"enabled": bool}` rather than `POST .../enable` + `.../disable`.**
    PRD 07's Admin table settles neither -- it had no row for either until this
    commit added one -- and the pair was declined because two routes cannot
    express *"set it to what I am looking at"*: an admin screen holds a
    checkbox, and a client that has to choose a verb from the value it is
    sending has re-implemented this DTO badly. It is also idempotent in the
    HTTP sense, which the pair is only by accident.

    Strict `bool`, so `"maybe"` is a 422 rather than a coerced `True`.
    pydantic v2 refuses a non-boolean string here by default; the case that
    says so is in `tests/unit/test_api_rows.py`, because "the framework does
    this" is a claim about a version.
    """

    enabled: bool
