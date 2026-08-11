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
