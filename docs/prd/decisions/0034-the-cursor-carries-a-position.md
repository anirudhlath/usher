# ADR-0034 — The cursor carries a sort position and nothing else, and no port takes one

**Status:** Accepted. Built in M9 (A3), with no consumer in that milestone's
group A — see *Consequences*.

## Context

[PRD 07](../07-client-api.md)'s `### Pagination` is two sentences:
*"Cursor-based (opaque, encodes sort position). Offset paging is not offered —
it degrades badly over a 1.3M-row catalog and produces duplicates under
concurrent writes."*

The first half is measured in this repository rather than asserted.
`MediaItemRepository.list_unmatched`'s `OFFSET` is **43.7 ms at offset 0 and
388.9 ms at offset 1,126,574** — linear per page, quadratic to drain
(`.claude/rules/emby-push-and-ingest.md`, quoted in PRD 10). That is why
`RawPayloadStore.iterate` and `TitleEmbeddingRepository.list_stale` already
take a typed `after: uuid.UUID` instead. This ADR gives that habit a **wire**
form, because five later routes need one and would otherwise each invent it:
`/browse`, episodes-by-number, the unmatched review queue, and more.

Three things had to be decided, and each has a reason that is not "it is
conventional".

## Decision

### 1. The cursor never reaches a port

The base64 lives in `usher.api.cursor`. A repository keeps taking typed keyset
values, exactly as those two walks already do.

Opacity is a **client-contract** concern: it exists so a client cannot build a
cursor and so the encoding can change without a `/v2`. A port that accepted
one would have to *decode* it, which means knowing the sort vocabulary of the
layer above it — and a cursor a port accepts is a cursor that has leaked into
the domain. The decode stays at the edge.

This is enforced, not agreed. `tests/unit/test_ports_pagination.py` walks every
abstract method under `usher.ports` and fails on a parameter named `cursor` or
on any annotation naming the codec's types. Half of it is already structural —
import-linter's layering contract puts `usher.api` above `usher.ports`, so a
port cannot import the codec — but the half that matters needs no import, and
that is the half a test can see. Measured: a port annotated
`position: "CursorSpec | None"` as a **string** passes `lint-imports` with
**9 kept, 0 broken** and is caught only by that file.

The one exemption is `MetadataProvider.changed_since`'s `cursor`, and it is
recorded with its reason: [ADR-0017](0017-the-metadata-port-is-an-aggregate-and-a-cursor.md)'s
cursor travels the *other* way. It is TMDb's own page token, minted by the
provider and handed back to the provider, never rendered to an Usher client.
The two share a noun and no direction.

### 2. The cursor is not signed, and it carries no user

It holds three things: a version, the sort-key values, and an 8-byte digest of
the query it was minted for. `Settings.secret_key` is deliberately not read.

Nothing in it is secret, and **every position it names is one the same request
reaches by paging** — so a forged cursor is not a capability. It is a request
for a page the client could have asked for anyway, and the route's own
authorisation still answers it. A hand-edited cursor cannot become an
injection either: every component is type-checked against the sort's declared
shape before it leaves the codec, so a year arrives as an `int` or the request
is refused. Without that check, a `text`-versus-`integer` comparison would
raise inside the handler — a 500 for a value a client typed.

> **The sentence to find when this changes.** This is right only while the
> cursor carries nothing the route does not re-derive from the request itself.
> **The day a cursor grows a `user_id`, a household, a filter the route trusts
> rather than re-reads, or any grant at all, it becomes forgeable and needs a
> MAC** — `Settings.secret_key` and `hmac.compare_digest`, verified before the
> payload is parsed. Carrying the household would also put authentication's
> seam somewhere other than `current_user`, which is the second reason not to.
> `CursorSpec`'s field list is pinned by a test so a fourth field has to argue
> for itself.

### 3. The digest is coherence, not security

Without it, a cursor minted under `sort=year` and replayed against `sort=name`
decodes cleanly and produces a plausible, wrong, **silent** page. With it, that
is a `400 invalid_cursor`. Same for a cursor minted over `genre=horror` and
replayed against `genre=comedy`.

Eight bytes of BLAKE2b over the sort name and the filter state, rendered in
sorted order so a client that reorders its own query string on a retry does not
lose its place. It is not a MAC and is not trying to be: the bar is accidental
collision between this API's own sorts, not forgery. It discloses nothing
either — it is computed over values the client itself sent, and the client is
the only party that ever holds the cursor.

### The keyset must be a total order, or nothing is minted

`CursorSpec` refuses to exist unless its last component is the UUIDv7 primary
key ([ADR-0003](0003-own-uuid-identity.md)). A keyset over a non-unique column
is not a total order and the damage is silent: `RawPayloadStore.iterate`'s
docstring already records that one bootstrap transaction stamps every row with
the same `transaction_timestamp()`, so a page boundary inside that group drops
the rest of it with nothing to say so.

**Three groups write keyset SQL independently this milestone, and the predicate
is not shared even though the codec is.** So the spelling is written down here,
where all three will read it. For a nullable sort column the keyset is
*conceptually* `(key IS NOT NULL, key, id)` — NULLs last, then the key, then
the primary key — and the comparison is **strict**. Note that neither clause
below spells that triple literally, and both of those are measurements rather
than style; the two warnings after the snippet are what each cost:

```sql
ORDER BY key <ASC|DESC> NULLS LAST, id ASC

-- resuming from a keyed position
WHERE key IS NULL                                   -- NULLs sort last
   OR key <  :after_key                             -- `>` for an ASC sort
   OR (key  = :after_key AND id > :after_id)

-- resuming from an unkeyed position: only the rest of that group can follow
WHERE key IS NULL AND id > :after_id
```

Relaxed from `>` to `>=` on the tail, it re-serves one row at every page
boundary — and a test whose pages do not abut cannot see it.

> ⚠️ **The `ORDER BY`, corrected 2026-08-12 by B7's measurement and B6's
> repair: it is `NULLS LAST` and must not be "clarified" into
> `(key IS NOT NULL) DESC, key <dir>`, which is the same order and costs
> 317×.** This section carried the written-out form for one day, on the
> good-faith argument that it lines the clause up with the predicate's first
> disjunct term for term. It supersedes only the `ORDER BY`; the three-arm
> `WHERE` below is untouched and is why the block under this one still
> stands. B7 measured it against a real 1,272,367-title
> catalog: `sort=name` is **299.21 ms p50 written out and 0.92 ms as `NULLS
> LAST`**, on a page proved byte-identical (0 mismatched positions of 25).
> `titles.sort_name` is `NOT NULL` and `ix_titles_sort_name` exists, but
> **Postgres does not simplify `sort_name IS NOT NULL` to `true` and matches an
> index by the sort-key *expression*** — so the written-out form has a leading
> key no index carries and the page becomes a 95,000-buffer Parallel Seq Scan.
> Pinned as a plan, not as a number:
> `tests/integration/test_title_repository.py::
> test_the_written_out_order_cannot_use_the_index_that_nulls_last_can` forces
> `enable_seqscan = off` and observes the refused plan come back at the
> disabled-node penalty of 1e10 — *unchoosable*, not merely not chosen.
>
> **So the `ORDER BY` and the `WHERE` no longer read as one rule, and that is
> paid for with a test rather than with prose.** `NULLS LAST` is where
> `key IS NULL` sorts; the two spellings are compared position for position,
> for every sort, over a population carrying NULLs and ties in every key, both
> unpaged and as a keyset walk. **The general form, which is the part that
> generalises past this ADR: two spellings of one order are two different sort
> keys, and a legibility decision about SQL text can be a plan decision.**

> ⚠️ **Corrected 2026-08-11 by measurement, and the spelling this replaces is
> the one a reader reaches for first.** This section shipped the predicate as
> a single row comparison:
>
> ```sql
> ORDER BY (key IS NOT NULL), key, id
> WHERE  ((key IS NOT NULL), key, id) > ((:after_key IS NOT NULL), :after_key, :after_id)
> ```
>
> It is wrong for a nullable key, and quietly. Postgres evaluates a row
> comparison element-wise and answers **NULL** — not false — when the first
> differing pair involves one, so resuming from an *unkeyed* boundary drops
> the whole rest of the unkeyed group while every page served looks full.
> Measured on `pgvector/pgvector:pg17`, five rows of which three have a NULL
> key, resuming from the first of those (`id = 1`):
>
> | spelling | rows returned |
> |---|---|
> | the row comparison above | `(5, id=4)`, `(7, id=5)` — **both keyed rows, neither remaining unkeyed one** |
> | the three-arm predicate | `(NULL, id=2)`, `(NULL, id=3)` |
>
> That is not an edge case on this schema: `titles.year`, `titles.popularity`
> and `titles.vote_count` are all nullable and `popularity` was measured NULL
> on **all 1,271,138 rows** of a bootstrap-only catalog, so the unkeyed group
> is most of the catalog on a fresh install. The ascending leading term is
> corrected with it — `(key IS NOT NULL)` ascending puts NULLs *first*, which
> contradicted this section's own opening sentence.
>
> The three arms are `IS NOT DISTINCT FROM` spelled out, and a two-branch
> Python helper is a legitimate rendering of them: the branch is on whether
> `:after_key` is NULL, which the caller knows before it builds the statement.
> `db/repositories/title.py`'s `_browse_after` is the worked example, and
> `TitleRepositoryBrowseContract::test_a_page_boundary_inside_the_unkeyed_
> group_does_not_drop_the_rest_of_it` is what holds it, on both arms and for
> all three nullable sorts.

### The page envelope carries no `total`

A count over a filtered 1.3M-row catalog is a sequential scan, paid on every
page, for a number a keyset page cannot use for anything: there is no page N to
jump to. `/browse`'s facet counts are a different question over a different
aggregate and are group B's.

`next_cursor` is `str | None` and is **always present**, which is deliberately
not the convention the rest of `api/dto/` keeps (elsewhere an empty value is an
absent key). A client takes both arms on every listing it renders, so "the key
is missing" and "there is no next page" would otherwise be the same bytes.

## Consequences

- **The codec ships with no caller in `src/`, and this is the one place the
  project's "no member without an emitter" rule is waived.** Four paged routes
  across three groups need the same codec and the milestone is built on
  parallel worktrees; the alternative is the first route writing it and the
  other three copying it, which is the shape the `adapters/http.py`
  consolidation had to undo for four adapters. It is proven at a **request
  boundary** instead — a probe route mounted on a real `create_app()`, the way
  `tests/integration/test_pipeline_spans.py` proves its wiring.
- **Pages are over-fetched by one row.** `over_fetch(limit)` asks for
  `limit + 1`; the extra row answers "is there more" and is never served, and
  the item mapper never runs on it. Without it, a population whose size is an
  exact multiple of the limit mints a cursor to nothing and a client spends a
  request to learn it is finished.
- **Every refusal is a `400 invalid_cursor` problem document with its own fixed
  sentence** — not base64, not a payload, wrong version, wrong query, wrong
  arity, wrong type. Never a 500, and never a pydantic 422, which would echo
  the rejected cursor back under `input`; no `detail` interpolates anything the
  client submitted. `ProblemCode.INVALID_CURSOR` is A2's, and the vocabulary it
  belongs to is group V's ADR-0030 to freeze — this task mints no code of its
  own.
- **A cursor is not portable across deployments of different versions.**
  `CURSOR_VERSION` is what buys the keyset the freedom to change shape; the
  cost is that a client holding one across a deploy is told to start over.

## Evidence

- **The off-by-one is real and only visible at `count % limit == 0`.** Planting
  the naive spelling (`over_fetch` returning `limit`, "a full page means there
  is more") fails
  `test_a_page_that_exactly_exhausts_the_population_carries_no_next_cursor`
  with *"and it is the last one"* — while the partition case over the same
  population at `limit=3` stays green, because `10 % 3 != 0`.
- **The port ban is not covered by import-linter.** Two plants, measured: a
  parameter named `cursor` on `TitleRepository.list_unwatched_candidates`, and
  the same parameter annotated `"CursorSpec | None"` as a string. Both pass
  `lint-imports` at 9 kept / 0 broken; each fails exactly one case in
  `tests/unit/test_ports_pagination.py`. Removing the `MetadataProvider`
  exemption fails the same file naming `changed_since`, which is what says the
  exemption is load-bearing rather than decorative.
- **`base64.urlsafe_b64decode` discards characters outside the alphabet.** So
  `!!not-base64!!` decoded to plausible garbage and was refused two steps later
  as *"not a payload"* — the right verdict for the wrong reason. The codec uses
  `validate=True`, and the six refusal sentences are asserted distinct.

## Uncertainty

**~~The Postgres arm of PRD 07's own claim is not tested by this task.~~
Closed 2026-08-11 by B6, the first repository to expose a wire-paged read.**
The debt was: *"offset produces duplicates under concurrent writes and keyset
does not"* is the stated reason for the whole design, it needs a real database
with a row inserted between page 1 and page 2, and no repository could serve
one. `TitleRepository.browse` can, and
`tests/integration/test_title_repository.py::
test_offset_duplicates_a_row_a_concurrent_insert_pushed_down_and_the_keyset_does_not`
runs both spellings over the same table with the same `ORDER BY`, against a
real `pgvector/pgvector:pg17`.

Measured: five rows, page size three, one row committed between the two
requests that sorts **into the page already served** (the case asserts that
premise — a row after the cursor is a page-2 row under both spellings and
would make the comparison vacuous). The keyset serves the pre-insert
population once, in order. `OFFSET 3` serves `Charlie` twice, because every
row after the insert moved one place down and the second request counted from
a total that had changed under it.

**One correction to the claim while verifying it: an insert duplicates and
does not drop.** The population grew by one, so the window that slid by one
still reaches the last row — nothing is lost, one row is repeated. The mirror
damage, a row *never* served, needs a concurrent **delete**. PRD 07 says
"produces duplicates", which is precisely what this measures; neither document
now claims the other half.
