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
- **`priority` is not here**, and it is the one that would actively lie.
  `_ENQUEUE` carries `WHERE jobs.status <> 'parked'`, so a parked row keeps
  the priority it parked at while this request asks for `DEMAND` and writes
  nothing -- a body reading `100` over a row still sitting at `('parked',
  20)` describes the *request* and calls it the queue. Measured against real
  Postgres; the table is in `usher.domain.jobs.JobKind.CURATE`.
- **`written` is not here**, and `usher.domain.jobs.JobKind.CURATE` says why
  at length: `enqueue` returns 1 both for a job it created and for one it
  merely promoted, and 0 for a repeat already at this priority. The number
  cannot answer "did my request do anything", so publishing it invites
  exactly the reading it cannot support.

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
