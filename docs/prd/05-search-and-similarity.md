# 05 — Search and similarity

## Two workloads, not one

Conflating these inflates the difficulty of the whole problem:

| | Scale | Job | What it needs |
|---|---|---|---|
| **Catalog lookup** | ~1.3M skeleton titles | "Find the half-remembered title" | Fast typo-tolerant prefix match on names and people, plus facets |
| **Library experience** | ~2k–10k owned titles | Taste, similarity, curation | Rich blending — and at this scale *every* technique is cheap |

The second is where the interesting UX lives, and 10k × 1024 float32 is 41 MB —
brute-force exact cosine in numpy, sub-millisecond. **No ANN index is required
for the tier that matters most.** *(This read "10k × 384 … 15 MB" until
2026-08-13. The width moved to 1024 with `m09e`
([ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)); the
conclusion is unchanged, the number is 2.7× larger, and the "sub-millisecond"
claim carries its own correction under `### Semantic` — it is a numpy figure
and Postgres measures 1.820 ms.)*

## Postgres-first

v1 uses PostgreSQL for all of it. Full reasoning and the evidence that reversed
an earlier Meilisearch recommendation: [ADR-0002](decisions/0002-postgres-first-search.md).

Summary of why:

- The well-known "Postgres full-text search collapses" benchmark is driven by
  *match-set cardinality*, not corpus size — it appears when a query matches
  ~1M rows, which long-text search does and title search does not. The vendor
  who published it started with a 34k movie dataset and discarded it for being
  too small to show any difference against Elasticsearch.
- **Ranking blend is application code regardless of engine.** Neither
  Meilisearch nor Typesense can express `0.6·semantic + 0.2·log(popularity) +
  0.2·recency`; Meilisearch's custom ranking is only `attribute:asc|desc` as a
  bucket-sort tiebreaker. So the search engine is a *candidate generator*, which
  makes it swappable behind a port.
- Staying in one system removes dual-write synchronisation, ghost documents,
  reindex-on-facet-change, and a second stateful service entirely.

## Design

### Full-text

A stored generated `tsvector` with weighted fields — A: name and original
name, **B: `credit_names`** (M7), C: overview and tagline, D: genres and
keywords — indexed with GIN and `fastupdate = off` (the default buffers into
a pending list that produces mysterious p99 spikes).

**The lane's ordering is not `ts_rank_cd` alone.** A title whose name *is* the
query leads, by `lower(name) = lower(btrim(query))`, ahead of the score and
ahead of the `LIMIT`; the weight classes decide everything below it. Why that
key rather than a heavier weight or a lower relevance decay, and what it was
worth over 800 sampled titles, is under [Ranking](#ranking).

✅ **Weight class D carried two spellings of one concept until 2026-08-19, and
`usher genres --backfill` is what fixes it —
[ADR-0039](decisions/0039-the-genre-vocabulary-is-usher-owned.md) and its
Amendment.** `titles.genres` unions two importers' vocabularies and the two
spellings share no lexemes: `to_tsvector('english','Sci-Fi')` is `'fi':3
'sci':2 'sci-fi':1` against `'fiction':2 'scienc':1`. A query reaching the
genres segment matched one half of the catalog — 20,051 titles or 6,223, never
both, because zero titles carry both labels. ADR-0039 fixed `/browse`'s filter
and facets at *read* time and deferred this one at a cost of *"~1.8 h of
re-embedding plus a 3.3 h `usher similar --rebuild`"*; **that estimate priced
the whole embedded population and the real bill is 304 embeddings**, so the
deferral was withdrawn the same day. `search_document` is
`GENERATED ALWAYS AS (...) STORED`, so the backfill's own `UPDATE` recomputes
the tsvector in the same statement — this lane costs nothing beyond the sweep.
Still unmeasured, and the sampleable version needs no user traffic: how many
`Sci-Fi` titles change position in a `/search` for a science-fiction query now
that they carry the other label.

**Weight class B is filled by M7, and the sentence M6 wrote about what that
would cost was optimistic.** M6 shipped B *reserved and empty* — correctly:
there was no `Person`, `Credit`, `Collection` or `Image` table, model or port
anywhere in `src/`, the only place credits physically existed was
`raw_payloads.payload`, and assembling a search document out of a
*provider's* JSON shape would have put a TMDb-shaped concept in `services/`.
It also wrote that *"filling B when M7 lands `Credit` is a migration rather
than a rewrite"*. **That is true only of the search path, and the migration is
a bigger one than the sentence implies.** Filling B cost three things:

- **A denormalised column, `titles.credit_names text[]`.** A stored generated
  expression may reference only the current row, so `setweight(to_tsvector(…,
  (SELECT … FROM credits …)), 'B')` is not expressible. Measured on
  PostgreSQL 17.10: the subquery form answers `ERROR: cannot use subquery in
  column generation expression` — **not** the immutability error this schema's
  wrapper trains a reader to expect, because Postgres refuses it syntactically
  before volatility is considered — and a bare cross-table reference answers
  `ERROR: missing FROM-clause entry for table "credits"`. An
  `IMMUTABLE`-declared SQL function that reads `credits` is **accepted in
  silence**, and is the worst of the three: the column it feeds then reflects
  credits as of whenever each row was last written, permanently, with no
  migration to blame. `credit_names` is maintained by the one call that also
  writes `credits`, inside the same transaction, holds the top ten billed plus
  every stored crew name, and is `NOT NULL` because `usher_array_text` is
  `STRICT` and one NULL nulls the entire document.
- **A forced full-column rewrite.** `CREATE OR REPLACE FUNCTION` does not
  recompute stored generated values, and neither does changing the expression:
  the migration drops the GIN index, drops the column, re-adds it and
  recreates the index. A table rewrite over the whole catalog — a maintenance
  window, not a hot deploy.
- **A full re-embed of the enriched tier.** The document assembly is
  positional, so an uncredited title gains a seventh *empty* segment and its
  fingerprint moves too: there is no subset of the catalog that keeps its old
  one. That is ADR-0020's scheme working, and it is 25 s to 2 min at the
  measured throughput — *for a 2k–10k household library*. Over the 130,647-row
  priority tier M9 enriched the same re-embed measured **2.31 hours**; the
  sizing paragraph below carries the arithmetic and why the invariant is not
  what predicts it.

`SearchDocument.credits` was carried through M6 as an always-empty parameter
so that M7 filled a caller rather than rewriting the type, and it is filled
from `titles.credit_names` — which is **not** a `Title` field: it is `credits`
projected to names and truncated to a ranking constant, so a domain model
carrying it would be a cast list that is not the cast.

**M9 stopped B being an enriched-tier column, and the paragraphs above are
written as though it still is.** Under M7 alone, `credit_names` is non-empty
only where `DeriveService` has run, which is the TMDb-enriched tier —
measured at **0 of 1,271,138** rows on a bootstrap-only catalog, i.e. weight
class B was reserved, filled, and in practice still empty for everybody who
had not run a crawl. M9 fills it from IMDb's `title.principals` × `name.basics`
for every title the crawl has not reached: **1,192,217 of 1,271,138 (93.8%)**,
mean **9.11** names, 158,479,368 B of text. Two writers, one predicate —
`enrichment_state = 'skeleton'` decides which owns a title — so `credit_names`
still never disagrees with `credits`, because a title with any `credits` row
is by construction not a skeleton.

**What that costs, measured on a real 1,271,138-row `titles` rather than
estimated.** The relation goes **872,759,296 B → 1,496,825,856 B after
`VACUUM FULL` (+624,066,560 B, +71.5%)** — 3.9 bytes stored per byte of name —
of which `ix_titles_search_document` is **4.54×** (40,304,640 → 182,951,936 B),
which is the class-B lexemes arriving. **The transient is the number to budget
against [08](08-operations.md)'s 8–12 GB, not the settled one**: before any
vacuum the same table is 2,240,970,752 B, because one `UPDATE` over 1.19M rows
leaves a dead tuple per live one.

**And it is an ordering constraint on the roadmap rather than a free win —
though not the one this paragraph claimed until 2026-08-12.** The embedded
population is `enrichment_state <> 'skeleton'` and the fill writes under
exactly the complement of that predicate, so filling B invalidates **no**
embedding, on a bootstrap-only catalog or an enriched one, by construction.
The constraint is *precedence*: run it after a priority tier is enriched and
the fill correctly declines every title in that tier, so the **203,969 of the
204,335 titles with ≥100 votes (99.82%)** that would have gained a
`credit_names` never do — and no re-run repairs it, because the predicate
goes on declining them. Backfill it before the crawl, not after. (The
superseded sentence said the late ordering "invalidates nearly all" of the
tier; it is refused by the fill's own `AND m.ours`, and it had reached the
CLI's `--help` and the bootstrap report before an audit caught it.)

**Measured class weights**, pg17.10, one term in three classes scored with
`ts_rank(…, websearch_to_tsquery('english', …))`: name **0.991** (A),
`credit_names` **0.396** (B), overview **0.198** (C) — `ts_rank`'s default
`{0.1, 0.2, 0.4, 1.0}` doing exactly what the class assignment says.

**"A stored generated `tsvector`" is right and the obvious spelling of it
does not compile — measured, not suspected.** `GENERATED ALWAYS AS (…)
STORED` rejects the natural expression with `ERROR: generation expression is
not immutable`, because `array_to_string(anyarray, text)` is `STABLE`:
`anyarray` admits element types whose output depends on a GUC (`timestamptz`
and `TimeZone`). Two further facts fall out of the same check —
`to_tsvector(regconfig, text)` *is* `IMMUTABLE`, so the explicit `'english'`
is load-bearing and a bare `to_tsvector(text)` would not work; and
`array_to_tsvector`, the obvious core-function fix, is **wrong for this
purpose**: it emits array elements as raw, unlexized, case-preserving
lexemes, so `ARRAY['Sci-Fi','Film-Noir','Drama']` stores
`'Drama' 'Film-Noir' 'Sci-Fi'` and a genre search silently matches nothing.
What ships is a custom `IMMUTABLE` SQL wrapper narrowed to `text[]`,
`usher_array_text` — and narrowing the signature is what makes the
immutability promise honest, so it must not be widened to `anyarray` to
"reuse" it.

**Changing that wrapper's body requires a forced rewrite of the column in the
same migration.** `CREATE OR REPLACE FUNCTION` does not recompute stored
generated values — verified — while a later `UPDATE` of a row *does*, which
produces a table where some rows were computed by the old definition and some
by the new with nothing to tell them apart. Migration `fa2b6c1e9d30` carries
the recipe and a test samples rows against a freshly computed document.
[ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md).

**`fastupdate = off` is confirmed and its real argument is the read side.**
Verified with `pageinspect`: after 5,000 inserts the default had 50 pending
pages / 5,000 pending tuples against 0 / 0. The cost that matters is what a
*query* then pays — a 1.6 MB pending list cost **231 buffers against 30, 7.7×
read amplification** on the index stage, invisible in `EXPLAIN` unless you
look at buffers. That is the mechanism behind "mysterious p99 spikes".

### Autocomplete — a separate, narrow path

**Do not route as-you-type queries through the full-text index.** Prefix
matching against a large full-text index is where the latency cliff genuinely
lives.

Instead: a trigram index, candidates capped at a few hundred, then
`levenshtein_less_equal` from the core `fuzzystrmatch` module as a re-rank over
that capped set, ordered by popularity.

**M6 built no narrow `title_search_names` table, and put the trigram index
directly on `titles`.** This section used to specify a narrow
`(title_id, name, kind, popularity)` table "over names and aliases", and its
justification is exactly that — *aliases and people names*, one title
contributing many rows. Neither had a data source in M6 (see weight class B
above), so the table would have held exactly one row per title duplicating
`titles(id, name, kind, popularity)`: a second copy of the same data, a
second thing to keep fresh, and a new instance of precisely the staleness
problem this milestone exists to eliminate. Boundary call 3.

✅ **Built by `m09a`, with five columns and not the four this section
sketches — and the paragraph above still stands.** The trigram index stays
directly on `titles`, and the narrow table duplicates nothing from it: it
carries **no `primary` rows**, because a canonical name is answered by
`ix_titles_name_lower_prefix` on `titles` itself, so a `primary` row would be
exactly the one-row-per-title copy boundary call 3 refused. Its two members
are `alias` and `person`, and **both emitters are now built** — see the alias
half below for the measurement that keeps the "duplicating `titles`" argument
true of the rows actually written, which is that three retained akas rows in
four restate the title's own name and are dropped.

`(title_id, name, kind, region, language)`. **`region` and `language` are new
and are not decoration:** IMDb `title.akas` is the alias source, and without
them a French and a Brazilian alias for the same film are indistinguishable
rows — a defect the loader cannot repair later without a second migration.

🔴 **`popularity` — the fourth column this section specified — is refused,
with a number.** `titles.popularity` is NULL on **all 1,271,138 rows**
(measured 2026-08-03), which is why the shipped suggest ordering was inert and
why the vote-count tiebreak was added. Copying a 100%-NULL column into a narrow
table is precisely the duplication boundary call 3 refused; the re-rank reads
`titles.vote_count`, as it already does. Correspondingly, *"ordered by
popularity"* in the sketch above is aspirational rather than shipped.

**Two tier-1 indexes, not one, and the pre-existing `ix_titles_name_lower_year`
is neither.** That index is `(lower(name), year)` with the *default* opclass,
which under this database's collation cannot answer `LIKE 'pre%'` at all —
measured on `pgvector/pgvector:pg17`, the plan is a `Seq Scan` even with
`enable_seqscan = off`. So `m09a` adds a btree on `lower(name)
text_pattern_ops` to **both** `titles` and `title_search_names`. The `titles`
one: p50 0.6 ms, p95 1.0 ms, max 10 ms, 44 MB, building in 0.559 s over
1,271,138 rows, against the trigram path's 33.3 ms p50 and 734 ms max —
re-measured by B3 on 2026-08-12 at **0.666 s / 44.2 MB**, the size to the
tenth. **The `title_search_names` one is a different size of object and was
never covered by those figures**: **4.527 s / 155.4 MB** over 10,896,525 rows.

✅ **`m09a` builds the shape and both halves that fill it are now written, by
two separate writers.** M6 deferred this to *"the day M7 lands aliases and
people"*. **M7 landed people and not aliases** — a deferral silently rolled
forward is the exact failure [09](09-roadmap.md) names for the tag genome (*"an
obligation recorded only where it was postponed is one nobody plans"*). Both
halves, with an owner:

- ✅ **Aliases come from IMDb `title.akas`, which needs no API call and does
  not touch the crawl's request shape.** The blocker this bullet used to carry
  was real and is about a *different source*: `alternative_titles` is in
  neither `append_to_response` list ([03](03-sources-and-sync.md)), so aliases
  are absent from `raw_payloads` entirely and landing them there would
  re-fetch the whole enriched tier. `BulkCatalogRepository.replace_aliases`
  writes them from the bulk dump instead — `kind = 'alias'`, `region` and
  `language` filled, the delete scoped by `imdb_ids` **and** `kind` so the
  people half survives it.

  **Three akas in four are not aliases at all, and dropping them is what keeps
  boundary call 3 from being reversed by accident.** Measured over a real
  1,271,138-title catalog: of 7,536,366 retained akas rows, **5,693,570
  (75.5%) `lower()`-equal the title's own `name` or `original_name`** and are
  not stored — such a row carries nothing `ix_titles_name_lower_prefix` on
  `titles` does not already answer. What survives deduplicates on
  `(title_id, lower(name))` to **1,663,364 rows in 307,822,592 B** (0.308 GB),
  against a bar of 8M rows and 1.0 GB written down before the measurement.
  **Only 399,046 of 1,271,138 titles (31.4%) gain even one alias**, so this is
  a narrow, cheap win rather than a broad one, and the comparison is `lower()`
  on both sides because that is the function the tier-1 index is built over.
- ✅ **The credited-person half is written by
  `CreditRepository.replace_for_titles`** — the call that already writes
  `credits` and `titles.credit_names`, from the same `credit_names` mapping, in
  the same transaction. **No second writer, no backfill job and no new
  command**: the array and the table were already two spellings of one fact and
  this is the third, so splitting the write is what makes them diverge, and the
  symptom is a *suggest* hit on a name `credits` no longer holds. `kind` is
  `person`, `region` and `language` are NULL on every such row (a credited
  person's name has no locale), and the delete is scoped by `title_id` **and**
  `kind` so the alias loader's rows survive it. The ranking — top ten billed
  then every stored crew name — is carried by the UUIDv7 primary key, because
  the table has no rank column and deliberately does not need one for aliases.
  **A catalog derived before this landed holds no rows here until `usher
  derive` re-runs over it**, which is worth knowing before timing a query
  against it.

The suggest path that *reads* the table is M9's, which
[ADR-0002](decisions/0002-postgres-first-search.md)'s failed gate obliges and
which *replaces* the shipped path rather than extending it — `m09a` builds the
table as part of the design that replaces the path, which is what removes the
"redesigns against a table built for the design it is replacing" objection this
section used to carry.

Stated honestly: **that call rests on a structural argument, not on a
latency measurement.** No variant was built and timed against the direct
index, because the two would answer the same query over the same 1.27M names
with the same operator class and the narrow table's only difference in M6 is
that it is a copy. The number that *is* measured is the index type below.

**The index is GIN, not the GiST this section used to specify, and that
question is now closed on real data.** Measured at 300k rows: build **579 ms
against 1,965 ms**, size **7,968 kB against 22 MB**, p50 lookup **9.01 ms
against 21.1 ms**. Re-measured at 2.08M names, where the honest summary is
that the two answer *different questions*: on the `%` threshold path GIN is
~110× faster (1.671 ms / 205 buffers against 182.5 ms / 31,174), builds in
7.5 s against 23.1 s and is 69 MB against 244 MB — but **GIN has no KNN
operator class at all**, so `ORDER BY name <-> q` under it degrades to a
`Seq Scan` at 3,989.9 ms where GiST answers from the index.

**Settled 2026-08-03 against 1,271,138 real names** by the gate below, which
ran both end to end over the same 2,993 typo cases. They trade rather than
one winning: GIN builds in **5.394 s** and occupies **75 MB** against GiST's
**11.800 s / 139 MB**, and answers at **p50 33.6 ms** against **198.1 ms**;
GiST returns **85.3%** recall@5 against **82.5%**, **47.9%** against 36.1% on
2–4-character names, and a tighter tail (**max 428 ms** against 730 ms,
because KNN traversal cost barely depends on match-set size while `%` does).
**GIN stays**: p50 is what a keystroke pays, and 2.8 points of recall do not
rescue a path already 4× over budget.

**And the two must not both exist.** With a GiST trigram index present
alongside the GIN one, the planner takes GiST for the `%` operator — the
identical shipped configuration went from p50 **33.3 ms to 141.5 ms** with
byte-identical recall. "Add GiST for a KNN path and keep GIN for `%`" is not
available; adding the second index silently taxes the first. **A path that
genuinely needs KNN needs to *replace* the GIN index, not sit beside it.** No
plan-shape test can distinguish the two, so the measurements carry this
choice and the suite does not.

**The cap must be ordered, and an unordered one is an active bug rather than
a simplification.** A `LIMIT` with no `ORDER BY` truncates arbitrarily, which
makes *lowering* the similarity threshold make recall **worse**: measured
66.2% @0.3 → 48.5% @0.1 → 2.6% @0.05 on a 604-case typo set. Any cap is
`ORDER BY similarity(name, q) DESC` under GIN (or `ORDER BY name <-> q` under
GiST). Capping smaller does not help — GiST KNN costs 272 ms at `LIMIT 200`
against 283 ms at `LIMIT 3000`; the cost is the traversal.

**The cap is a latency control and not the recall lever this section
implied.** Over the gate's 2,993 real typo cases the cap truncated **0.0%**
of the shipped configuration's misses and the `levenshtein_less_equal`
re-rank dropped **0.0%** in every configuration measured. Capping *wider*
makes recall worse, not better — GiST KNN at `LIMIT 1000` scores 83.4%
against 85.3% at `LIMIT 200` — because a bigger pool means more
equal-distance competitors for the final ordering to separate.

`pg_trgm.similarity_threshold` stays at its 0.3 default and is set with **`SET
LOCAL`**, never a bare `SET`, which leaks onto the next checkout of a pooled
connection — verified for this GUC and for `hnsw.*`. And **never feature-detect
a contrib GUC**: `SHOW pg_trgm.similarity_threshold` raises on a backend that
has not yet run one of the library's operators, while the `SET LOCAL` succeeds
on that same cold backend.

**0.3 survived the gate, and the case for lowering it does not.** At
1,271,138 real names, dropping the floor to 0.2 or 0.1 leaves recall flat or
slightly worse (82.5% @0.3 → 85.1% @0.1 *only* once the ordering below is
fixed, and 78.3% → 77.6% before it) while costing 4–14× latency (p50 33.6 ms
→ 128.7 ms → 469.2 ms). What a lower floor actually does is move a miss from
one stage to another: the gate's own diagnosis went from 63.6%
below-the-floor / 36.4% out-ranked at 0.3 to 4.0% / 71.2% at 0.1. Note that
`_TRIGRAM_THRESHOLD` in `adapters/search/postgres.py` is **0.1 and is the
*contract suite's* floor, not the shipped one** — a fixture with two rows has
no competitors, so 0.1 rescues a case there that it cannot rescue at scale.
The divergence is stated in that constant's own comment rather than left to
be discovered.

**The result is ordered by popularity *and then by vote count*, because
popularity is sparse.** `titles.popularity` is NULL on all 1,271,138 rows of
a **`--phase imdb`** catalog — the one M6's gate ran against — and on ~77% of
a **`--phase all`** one: `link_crosswalk` writes it from `tmdb_ids`, and Task
36 measured **291,584 of 1,271,570 titles (22.9%) carrying a popularity, of
which exactly 3 are 0.0** (2026-08-05, so the daily export ships real values,
not `NOT NULL DEFAULT 0` filler). So `ORDER BY dist ASC, popularity DESC NULLS
LAST, id ASC` degenerates to ordering equal-distance candidates by a UUIDv7
(insertion order) on the NULL majority, and `vote_count DESC NULLS LAST` goes
*under* popularity, is filled by the bootstrap itself (539,350 rows), and was
worth **+4.2 points of recall@5 overall and +8.3 on 2–4-character names** when
M6 shipped it 2026-08-03.

**Task 36 re-measured the ordering on the populated catalog and kept it
unchanged (2026-08-05).** Same 2,993 typo cases at seed 20260803, the
populated arm against the all-NULL one: the populated catalog costs **1.3
points overall (83.4 → 82.1)**, entirely out-ranked misses where a real
popularity promotes a wrong candidate — within the 2.0-point regression bar,
so the earlier position that a *partially* populated catalog is worse than
either extreme is **refuted**. Making `vote_count` the primary key (dropping
popularity) recovers all 1.3 points and does not hurt the all-NULL arm, but
its behaviour on a genuinely *enriched* tier could not be measured on this
skeleton catalog, so it is an M9 change to re-measure rather than a shipped
one; `NULLIF(popularity, 0)` recovers nothing, since only 3 zeros exist.

**Two numbers in this section are from different runs and must not be
subtracted from each other.** The gate table below reports **82.5%** for the
shipped configuration; Task 36's arms are **83.4% → 82.1%**. They are the same
statement over the same 2,993 cases at the same seed, measured two days apart
against two *different catalogs* — the gate's was a `--phase imdb` bootstrap of
1,271,138 titles, Task 36's a `--phase all` one of 1,271,570 — and the catalog
is the independent variable in both. The comparison that means something is
within a run (83.4 against 82.1, one arm against the other); the comparison
that does not is across them.

**And `ix_titles_popularity` was dropped in the same task** (migration `ffc`).
It was not merely unused: it was **unusable as declared** — a `DESC` btree,
which Postgres builds NULLS FIRST, while every consumer asks
`DESC NULLS LAST`, a different pathkey the planner can never satisfy from it.
`list_owned_by_tag`, added in M7 and the one statement that genuinely orders by
`titles.popularity`, plans as a Merge Semi Join over `pk_titles` and never
touches it. 9,536 kB of index that no statement could take; the migration's
docstring carries the `EXPLAIN`.

**`<%` (`word_similarity`) was measured and not taken.** It separates
fixture-scale examples better than `%` (0.8 / 0.4 / 0.2 against
0.250 / 0.250 / 0.111) and is served by the same `gin_trgm_ops` index, and
over the gate's 2,993 real cases it scores **78.1% at p50 46.1 ms** against
`%`'s 82.5% at 33.6 ms — worse on both axes. A fixture-scale separation is
not a recall figure.

> **Settled in M6 — yes, `suggest` is its own port.** This section already
> treated autocomplete as a separate narrow path while `SearchIndex.suggest`
> was one method on the same port as `search`/`index`/`remove`. It is now
> `SuggestIndex`, a separate ABC with exactly one method and **no write
> path**. The argument that decides it is dual-write visibility, not
> tidiness: if the gate below fails and Meilisearch is added for the
> instant-search box, documents must be written to *both* engines — the cost
> [ADR-0002](decisions/0002-postgres-first-search.md) refused — and splitting
> the port puts that cost in the type system rather than making it look like
> implementing a method that was already there. The shipped pair is the
> evidence: `PostgresSuggestIndex` and `PostgresSearchIndex` share a session,
> the `titles` table, and no SQL, index, GUC or ranking rule.
> [ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md).

### Semantic

`halfvec(1024)` embeddings over name + original name + overview + tagline +
genres + keywords, HNSW indexed, from **either** of two `Embedder` runtimes
chosen by a prefix on `USHER_EMBEDDING_MODEL`:
`fastembed:<checkpoint>` loads the model in-process behind
`uv sync --extra embedding`, and `openai:<checkpoint>` calls
`POST {USHER_EMBEDDING_BASE_URL}/embeddings` on any OpenAI-compatible server.

**The width is 1024 because `BAAI/bge-m3` is, and that made it a migration
rather than a setting.** `m09e` moved `title_embeddings.embedding` and
`user_taste.centroid` from `halfvec(384)`, deleting every stored vector,
centroid and neighbour row on the way: the width is `halfvec`'s typmod, a
typmod is DDL, and there is no honest conversion from 384 lanes to 1024. **It is
also deployment-wide rather than per-model**, so the service-free default had to
move off `bge-small-en-v1.5` (384) and is now `fastembed:BAAI/bge-large-en-v1.5`
— **1.2 GB against bge-small's 0.07**, a real regression in the install with no
inference server and the price of the width. The whole argument, including why
`fastembed` could not simply be pointed at `bge-m3` (it does not ship it, in any
of its five model classes), is in
[ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md).

🔴 **What motivated the change is a relevance observation, and it is the first
this project has: at 384 the semantic lane retrieves *topic* well and *plot*
badly.** Over this catalog with `fastembed:BAAI/bge-small-en-v1.5`, a
plot-description query puts the correct title in the top **0.05–0.3%** and
usually **outside the top 20** — *"a man relives the same day over and over"*
ranked Groundhog Day **64th**, Shawshank **208th**, The Matrix **262nd**,
WALL-E **338th** — while queries naming a title's subject matter directly put
Jurassic Park **1st** and a Harry Potter query **4th**. Those percentages are
of the *embedded* population — the enriched tier, ~130k rows, not the 1.27M
catalog — so a rank of 64 is a lane that is genuinely working and is still
nowhere near a five-row box. **`bge-m3` is a bet on exactly that gap and nothing
has yet measured whether it pays** — the re-embed is in flight and the
comparison is owed.

**A checkpoint of the wrong width narrows the deployment rather than breaking
it.** `composition.embedder` compares the `Embedder`'s own reported width
against the column's, logs once and builds no embedder — so `INDEX` jobs go
unclaimed and the catalog-lookup tier is untouched, exactly as a deployment with
no model behaves. The comparison is only worth something because
`Embedder.dimension` reports *the model's* width and never the schema's.

**The *in-process* runtime is `fastembed`, not sentence-transformers, and this
sentence is a correction rather than a preference.** (It was the only runtime
until `m09e`; everything below is still why a deployment that loads a model
loads it through `fastembed`.) Measured 2026-08-02: sentence-transformers
is 59 packages and **4.8 GiB installed**, ~4.5 GiB of which is GPU runtime
pulled unconditionally on a host that may never have a GPU, against a `usher`
image of 332 MB. `fastembed` is 28 packages and **167 MiB**, has no torch, and
is faster on identical input (252.9 texts/s against 229.5), with min cosine
agreement **0.99999619** and top-1 identical on 205/205 documents. The
dependency lives behind an extra (`uv sync --extra embedding`) and
`USHER_EMBEDDING_ENABLED` is off by default: full-text and trigram serve all
1.27M titles with no model at all, so a deployment without it is *narrowed*
rather than broken.

**The embedded population is the enriched tier, not the catalog** —
`enrichment_state <> 'skeleton'`, for which `ix_titles_enrichment_state` is
already exactly the partial index. This is this section's own two-workload
split taken seriously: catalog lookup is full-text plus trigram over
everything, and the semantic tier is the library experience at 2k–10k titles. A
skeleton is a name and a year, so embedding it produces a vector of the name,
which full-text already does better and cheaper — and a skeleton's search
document is a generated column, so it is fully indexed with no job at all.

**Sizing, quoted as the invariant rather than as a rate** (measured 2026-08-02
on a Ryzen 7 5800X3D, CPU): throughput is linear in **tokens**, not texts, and
holds at **~8,000–10,700 tokens/s** across the whole range — 412.7 texts/s at
19 tokens, 83.5 at 100, 18.7 at 516. A realistic `name + overview + genres +
keywords` document is **~100–130 tokens**. So the enriched tier is **~25
seconds to 2 minutes** at 2k–10k titles; all 1,271,138 titles would be **4–6
hours**, which is the number the population choice avoids paying. Best CPU
batch size is 16, flat to 64, worse at 128. GPU throughput is deliberately
unmeasured — the probe found 210 MiB free of 24,564 and declined to disturb a
running service.

✅ **The invariant survived its first real tier and the sizing derived from it
did not — measured 2026-08-12 over 130,647 enriched titles.** M9 enriched a
priority tier rather than a household library, so the same code ran at 13–65×
the population above. Three things came back. The **document** is exactly what
was assumed: mean **125.4 tokens** over 1,000 sampled titles (median 118, p95
197, max 323, none over the 512 window), inside the ~100–130 band. The
**model** is exactly what was measured: **9,683 tokens/s** on real documents at
one text per call, inside 8,000–10,700. But the **backfill** runs at
**2,988 tokens/s across two workers — about 15% of the model's rate** — because
an `index` job is the model *plus* a claim, three reads, a staged `COPY`
through a temp table and a commit, per title, and because `usher work`
re-counts the whole staleness predicate after every pass of ≤20 jobs (360.9 ms
at this tier, **23% of one measured drain's wall clock**). **So a tokens/s
figure sizes the model and never the queue**, and `usher index`'s printed
estimate — which divides by the invariant — came back **2.5–3.3× optimistic**
against a measured pass (109–145 s predicted, 361 s actual). Two source sites
still describe the staleness scan's population as "2k-10k rows"
(`telemetry.py:546`, `composition.py:1317`).

✅ **And the candidate-pool walk behind `usher similar --rebuild` has a price
for the first time: ~80 minutes over 130,647 embeddings** (2026-08-12). The
exact brute-force scan is **36.5 ms per seed** at `rebuild`'s own 500-seed page
and 37.4 at 50-seed pages, so `130,647 × 36.5 ms = 4,769 s`. Per-seed cost is
**linear in the embedded population**, which makes the walk quadratic in it;
seed-side paging is ~3 s of the total and page size is not a lever. The price
is seed-independent to within 1.9%, so bounding the walk by *seed count* is
sound while bounding it by a `list_embedded` prefix is not — those ids are
UUIDv7 minted in IMDb `tconst` order, so a prefix is ordered by registration
era. Full evidence in `.claude/rules/search-and-embeddings.md`.

🔶 **Every throughput and sizing figure in the four paragraphs above was taken
with `fastembed:BAAI/bge-small-en-v1.5` at 384 lanes on CPU, and none of it has
been re-taken since `m09e`.** The document-length figures (mean 125.4 tokens,
p95 197) are a property of the catalog and survive; the tokens/s invariant, the
2,988 tokens/s backfill rate, the 80-minute pool walk and every extrapolation
from them describe a narrower vector produced by a different model on a
different device. The re-embed through `bge-m3` that would replace them is
running as this is written and **its numbers are owed rather than estimated**
— they land in `.claude/rules/search-and-embeddings.md` beside the figures they
supersede, and nothing here guesses a direction for them.

**Freshness is a predicate, never an inference.** `title_embeddings` records
`model_name` (the runtime *and* the checkpoint, e.g.
`fastembed:BAAI/bge-large-en-v1.5` or `openai:BAAI/bge-m3`) and a
`source_fingerprint` — the `md5` of
the exact text embedded — so "is this stale?" is one SQL query with three
consumers: the backfill's cursor, the `usher.search.embeddings.stale` gauge,
and the test that proves the enqueue-on-enrichment path closes. Editing a
title's overview moves the fingerprint and re-claims the row with nothing being
told; swapping the runtime moves `model_name` and re-claims every row, which is
the scheme replacing a migration. `usher index` reports both counters and
writes nothing; `usher index --backfill` enqueues one job per stale title,
keyset-paged on `titles.id` and re-runnable at zero write cost.
[ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md) carries
the argument and the costs.

⚠️ **"The scheme replacing a migration" is true within one vector width and
stops there** — corrected 2026-08-13. `EMBEDDING_DIMENSIONS` is `halfvec`'s
typmod, so a swap that changes the width is DDL: `m09e` is the first, and it
deleted every stored vector rather than converting one. **And the fingerprint
reaches `title_embeddings` but not `title_neighbors`**, whose
`blend_fingerprint` hashes the blend's constants and not the model — so a model
swap leaves every neighbour row reading as current, and `m09e` empties the
table to fix that instance rather than the class.
[ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md).

**A title whose composed document is degenerate is refused, and the refusal is
written.** Measured: every whitespace-only input embeds to the *identical*
vector — cos = 1.0000 exactly — so a catalog of them is an unbounded cluster
pinned to the top of every "more like this" result rather than a bad result,
and no assertion about norms or dimensions can see it. A refused title
therefore gets a row with a `NULL` embedding and the fingerprint of the
degenerate text: it stops matching the stale predicate, starts matching a
separate countable one, and is re-claimed exactly once when enrichment gives it
content. Refusing by writing nothing would leave it matching the backfill
forever. The threshold is about *empty*, not *thin* — name-only skeletons
measure 0.5867 pairwise and retrieve their own enriched form at 0.7638 against
a 0.4751 cross-title mean.

- `hnsw.iterative_scan = relaxed_order` **must be set explicitly** — it is off
  by default, and without it filtered vector queries suffer severe recall
  collapse.
- **`hnsw.ef_search` is 200, and 2026-08-19 is the first time this project
  priced it against a real index.** Over 132,409 real 1024-lane vectors and 12
  typed plot queries, recall@10 against an exact scan of the whole embedded
  population is **0.858 at the previous default of 100 and 0.917 at 200**, for
  a p50 of 4.77 ms against 10.59 ms and a p95 of 7.30 against 16.18 — beside a
  recorded query embed of p50 5.7 ms. It keeps buying recall (0.967 at 400,
  0.992 at 1000) and stops being affordable: 400 costs a p50 of 20.13 ms. The
  curve is **monotone at every one of the 12 queries**, which is what the
  change rests on rather than the single missing film that prompted it. Under
  a 4.8%-selectivity genre filter the same move buys 0.783 → 0.808 only, so
  **this is an unfiltered-path fix**; on the filtered path the lever is
  over-fetch and re-rank (0.783 → 0.892 at `ef_search` 100, fetching 5× and
  cutting back), which is measured and not built. The full evidence, including
  the two controls that say why an "exact re-score" of an over-fetched
  candidate set is arithmetically a no-op, is in
  `.claude/rules/search-and-embeddings.md`.
- Owned titles skip ANN entirely; exact brute-force cosine is faster and exact
  at that scale. **The claim above that this is "sub-millisecond" is true in
  numpy and false in Postgres**: measured at 10k vectors, 1.820 ms in
  Postgres (seq scan plus top-N) against 0.088 ms in numpy `float32`. The
  conclusion survives comfortably — 1.8 ms exact beats an approximate
  filtered HNSW scan — but **numpy `float16` is 140× slower than `float32`**
  (12.275 ms against 0.088 ms; there is no SIMD GEMM path for half
  precision), so store `halfvec` and convert to `float32` before any numpy
  dot product.
- pgvector pinned ≥ 0.8.5 (CVE-2026-3172, plus HNSW vacuum corruption fixes).
  The image used by the test suite and by compose ships **0.8.6**.

> **Settled in M6 by measurement, and both halves resolve *against* the
> previous wording.**
>
> **No query/document split.** `Embedder` keeps one `embed`. The documented
> BGE query prefix moves MRR by **−0.0028**, 95% CI `[−0.0259, +0.0203]`;
> applying it to *both* sides is significantly harmful at **−0.0663**, CI
> `[−0.1013, −0.0330]`. The experiment carries a power control — a
> deliberately wrong prefix moves MRR **−0.2497** at P(>0) = 0.000 — so this
> is a measured null rather than a blind one, and the port's old "callers are
> responsible for any query-side prefix" clause is deleted because it is the
> hazard: one symmetric loop is the cheapest way to obey it and *is* the
> −0.066 condition.
>
> **Normalisation is real, and it is a property of this checkpoint rather
> than of embedders.** Norms are 1.0 to within 5.96e-08 and the library's
> `normalize_embeddings=False` returns bit-identical vectors, because
> normalisation is a third module baked into the checkpoint; the same
> backbone without it returns norms 8.99–9.46. So the implementation asserts
> the norm on its first batch rather than trusting a model card. Two limits
> the old sentence did not carry: **after the `halfvec` cast the vectors are
> no longer unit** (norm drift 1.19e-07 → 1.21e-04), so "cosine == dot" holds
> only before the cast; and the contract is **load-bearing only under the
> inner-product operator** — `<=>` is normalisation-invariant while `<#>` is
> not, and this design specifies `halfvec_cosine_ops`/`<=>`, so normalisation
> buys speed here, not correctness.
> [ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md).

### Fusion

Combine full-text and vector results with **Reciprocal Rank Fusion**, not
weighted score addition — BM25-style ranks and cosine distances are on
incompatible scales and adding them produces confident nonsense.

### Similarity

⏳ **The route is M9's; the service and the table behind it shipped in M6.**
M6 adds no HTTP route at all (boundary call 1), so `GET /titles/{id}/similar`
does **not** exist. What exists is `SimilarityService`, the precomputed
`title_neighbors` table, and `usher similar` on the command line. M7 is the
first in-process consumer.

`GET /titles/{id}/similar` blends, in application code:

- embedding cosine over overview text,
- Jaccard over genres, keywords, cast, and crew,
- ~~MovieLens tag-genome cosine where available, weighted in only when
  present.~~ 🔴 **Built in M7 at weight 0.25 and REMOVED from the blend on
  2026-08-12 by M9's S7, on a measurement.** The vectors are still imported,
  still stored and still read per pair; they are no longer a term.
  [ADR-0024](decisions/0024-the-genome-is-one-dense-vector-per-title.md)
  carries the full amendment and this is the short form.

  The importer shipped in M7 (`usher bootstrap --phase movielens`,
  `genome_scores`), and Task 36 measured every denominator (2026-08-05, a
  `--phase all` catalog of 1,271,570 titles): 15,565 genome vectors are
  **1.22%** of all titles and **1.73%** of the 899,991 movies; **7.61%** of
  [04](04-catalog-bootstrap.md)'s "≥100 IMDb votes" priority tier (measured at
  204,494 titles) — the denominator that makes the "~7%" this line used to
  carry roughly right; and **10.68%** of a real household's 5,020 owned titles.
  **The number that decides the term's weight is the candidate-pair rate** — of
  the 100 candidates each seed's pool holds, how many carry a `tags` value —
  measured, never squared. M7 put it at **1.81%** (9,069 of 502,000 pairs), and
  M9's S1 then established that those 502,000 pairs are exactly 5,020 owned,
  **name-selected, pre-TMDb** seeds in a scratch database that no longer
  exists: the tier promotion moved a label, not a document, so
  `search_document`'s weight classes C and D were empty and the pool was drawn
  by name.

  **M9's S5 re-measured it over a genuinely enriched population and it is a
  second measurement, never a delta: 2.4746% — 323,297 of 13,064,700 candidate
  pairs, over 130,647 seeds, 15,525 of them carrying a vector (11.883%
  single-side).** What is new is that the genome rate is now known over
  documents that finally carry `overview`, `tagline`, `genres` and `keywords`,
  which is what M9's enrichment existed to produce — and it is **still four
  times below the 10% floor the 0.25 weight assumes**. Both figures stay in the
  record with their populations attached. `coverage²` would have predicted
  1.412%, so the measurement is **1.75×** the independent-draw prediction: pool
  membership and genome membership are positively correlated, which is the
  first time this project has had the correction factor rather than the
  warning.

  So the term comes out — the `_WEIGHTS` key and the `tags=` argument together,
  never a 0.0 weight, which is arithmetically identical to absence while still
  moving `blend_fingerprint`. **The `<=>` and the TOAST fetch per candidate
  pair are NOT saved and that is deliberate**: the cost sentence this bullet
  used to carry ("costing a `<=>` and a TOAST fetch on every candidate pair of
  every rebuild") described the *read*, and the read stays so the rate remains
  reported by `usher similar --rebuild` on every rebuild rather than by a query
  somebody has to think to run. It is also the only remaining consumer of
  ADR-0014's `None`-not-0.0 rule on this field.

  **The ceiling is what makes this unlikely to reverse on more enrichment.**
  `ml-latest` is **movies-only** and **frozen at 2023-07-20**, and scores
  **16,376** movies — 18.9% of its own 86,537-movie list — so coverage of
  anything newer is structurally zero and decays. M9's enrichment moved the
  document and therefore the pool; it could not move the numerator. Two
  vectors are comparable only when they came from the same release, which is
  what `genome_scores.genome_revision` records and what
  `GenomeRepository.get_pair` refuses to blend across,
- collection membership as a strong signal.

Neighbours are precomputed offline into a `title_neighbors` table — item vectors
are static, so this is a cheap batch artifact that makes "more like this"
instant and engine-independent.

**As of M6 two of those four signals have no data in `src/` and the shipped
blend is the other two**, checked against the code rather than against this
prose. Cast and crew had no `Person`/`Credit` table, model or port anywhere at
that point (**M7 landed all three — see the note below the M7 table**);
the MovieLens tag-genome importer had never been built at that point (it
shipped in M7: `movielens` is a bootstrap phase and
`adapters/bulk/movielens.py` exists, so this signal now has data — blending it
in is M7's own similarity work, not M6's); and `titles.collection_id`
is a bare nullable UUID with no table that nothing in `src/` writes. So M6 shipped
**embedding cosine (0.60) plus keyword Jaccard (0.25) and genre Jaccard
(0.15)**, written as a sum of weighted terms over an explicit signal list.

**M7 landed the third signal; M9's S7 removed it. The shipped blend is three
terms, and the surviving weights are M7's rather than M6's:**

| Term | M6 | M7 | **M9 (shipped)** | Renormalised share |
|---|---|---|---|---|
| `cosine` | 0.60 | 0.45 | **0.45** | 0.45 / 0.75 = **0.600** |
| `tags` | — | 0.25 | **removed** | — |
| `keywords` | 0.25 | 0.20 | **0.20** | 0.20 / 0.75 = **0.267** |
| `genres` | 0.15 | 0.10 | **0.10** | 0.10 / 0.75 = **0.133** |

**The three surviving weights are deliberately not "reverted to M6's".** The
measurement licenses removing a term whose coverage cannot support its weight;
it licenses nothing about keywords against genres, which is the only thing
0.45/0.20/0.10 and M6's 0.60/0.25/0.15 differ on once `_blend` renormalises
(0.600/0.267/0.133 against 0.600/0.250/0.150). Leaving them where M7 put them
means the removal changes **no score at all** on the ~97.5% of pairs that
carried no genome — those pairs were already scored under this exact
denominator — so the change is confined to the 2.4746% the evidence is about.
Restoring M6's numbers would be an unevidenced second decision moving every
score in the table.

**The removal costs a full rebuild and nothing else.** `blend_fingerprint()`
moves from `78900b2bd89a649774d7fd3efe082621` to
`78f3ecd20e654c0f6aa4bdf646ec099b`, so every stored `title_neighbors` row reads
as stale until `usher similar --rebuild` runs — a **query**, answered by
`SimilarityService.stale_neighbors()`, not an inference
([ADR-0020](decisions/0020-derived-state-carries-its-fingerprint.md)). At
130,647 embedded titles that rebuild is a full quadratic walk measured at
**85.4 minutes**, so it is a scheduled operation. ✅ **It was run on
2026-08-12 by M9's H7** — 88.3 minutes over the whole embedded population,
`stale_neighbors()` **0**, and **3,266,175 rows** (130,647 seeds × 25) every
one of them stamped `78f3ecd20e654c0f6aa4bdf646ec099b`. The row count is
recorded beside the verdict because *"no stale rows"* is satisfied by an empty
table, and an empty table is exactly what this one was until that run.

⏳ **Cast/crew Jaccard and collection membership are still not terms, and M7 is
the milestone where the distinction between "the data landed" and "the term
landed" has to be said out loud.** `people`, `credits` and `collections` are
real tables as of M7 ([02](02-data-model.md)), so the *data* both signals need
now exists — and `SimilarityService._WEIGHTS` has **three** keys, not six.
`NeighborSeed`/`NeighborCandidate` carry no cast, crew or collection field, so
adding either is the same port-plus-two-fakes-plus-a-surface-pin change the tag
genome turned out to be, **plus** a full `usher similar --rebuild` because
adding a term re-weights the others and moves every stored score. It is
therefore a change with a fingerprint bump attached rather than a small one,
and it is **unassigned** — recorded here at the moment its blocker was removed,
so nobody later reads the shipped blend as the four signals this section
specifies. ⚠️ **And whichever signal arrives next, it must not be called
`tags`.** That key is free as of S7 and it named the *tag genome*; M9's S6
evaluated MovieLens **user tags** under the same word and refused it at 6.0821%
(ADR-0035, which S6 owns). A stored score records only a `blend_fingerprint`,
so a later reader finding
`tags` back in `_WEIGHTS` could not tell which of the two signals a row
contains. The genome, if it returns, is `genome`; a user-tag term is
`user_tags`.

**The three surviving weights sum to 0.75, and that is the whole argument for
these numbers rather than round ones.** `_blend` renormalises over the signals
that are *present*, so the cosine share is **exactly 0.600, unchanged to three
decimal places** against M6, while keywords and genres sit +0.0167 and −0.0167
off it. A pair's score therefore differs from M6's by
`0.0167 × (keywords − genres)`, **bounded by ±0.0167**, and two of them can
only swap if they were already within 0.033 of each other. That is an
arithmetic bound with a real residual, not a claim that the existing ordering
is preserved. **It covered "the pairs with no genome" from M7 until S7 and now
covers all of them**, which is exactly the shape of the removal.

**A pair where only one side has a genome vector carries `None`, never 0.0**
([ADR-0014](decisions/0014-absence-is-not-zero.md)). This is the first site
where `0.0` is not merely uninformative but *unreachable by real data*: every
genome component is positive, so the true cosine of any real pair is well above
zero — measured floor **0.2556** over all 268,157,000 ordered off-diagonal
pairs, mean 0.6101, sd 0.0913. **Since S7 the rule defends the measurement
rather than the blend**: nothing scores this value, and its only consumer is
`NeighborRebuild.pairs_with_tags`, where a `0.0` would report a barely-covered
catalog as fully covered — making a dead signal look live, which is the wrong
direction for the number a later milestone would re-open the decision on.

**The genome term's spread was measured before its weight was chosen, its
coverage was measured twice, and its relevance was never measured at all.** The
saturation bar was written down first — saturated if mean ≥ 0.70, or p1 ≥ 0.50,
or sd < 0.05, or the top-10 neighbour gap < 0.15 — and no clause fired, so the
vectors ship raw rather than mean-centred. That says the term is **not inert**,
and it stays true after the removal: what a pair rate settles is how *often*
the term fires, not how good it is when it does. Nothing here says 0.20 beats
0.25 — the surviving weights remain **chosen with an argument, not measured**,
because nothing in this project measures similarity relevance. The three claims
are kept apart deliberately.

They stay constants rather than settings, because changing one changes what
"similar" means and every stored row was written under the old meaning — which
is now a *detectable* condition rather than a warning, since
`title_neighbors.blend_fingerprint` records which blend produced each row.

**What the third signal actually cost, because M6 published an estimate and it
was optimistic.** M6 wrote that landing a third signal is "one entry and one
accessor rather than a rewritten scorer". True of the scorer exactly — `_blend`
is untouched and no consumer of `title_neighbors` changed — but the value has to
*come from* somewhere, and the neighbour DTOs live on a **port**. The measured
bill: one `_WEIGHTS` entry, one accessor, **two port DTO fields, two widened
statements, both fakes, and the port's abstract-method pin**. The signal list
really is the extension point; the sentence understated the blast radius of a
port change, and is corrected here rather than quoted.

Genres and keywords are **two terms rather than one Jaccard over their union**,
and the reason is vocabulary size: genres are a closed set of about nineteen
values with two to four per title, so genre overlap saturates (any two dramas
score 0.33 or better regardless of subject), while keywords are a long tail
where an overlap of three is evidence. Merged, the five-element genre
contribution disappears inside a forty-element keyword union and the term
nobody weighted does all the work.

**Jaccard of two empty sets is `None`, not `0.0`.** The naive spelling divides
by zero inside a batch job — which aborts a rebuild mid-page and leaves a table
half old and half new — and `0.0` is worse because it is silent: it gives the
same answer for "these two share no genres" (evidence) as for "we do not know
either one's genres" (a fact about enrichment, not about the films). An absent
signal leaves the numerator *and* the denominator, so a thin title's neighbours
are decided by its vector rather than pushed to the bottom of every list.
[ADR-0014](decisions/0014-absence-is-not-zero.md), applied to a set-valued
field.

⚠️ **The genre term was scored over two vocabularies that never co-occur — a
trap that was not sprung, and is now disarmed rather than closed.**
`titles.genres` unioned IMDb's labels and TMDb's, and zero of 1,272,866 titles
carried both spellings of any concept
([ADR-0039](decisions/0039-the-genre-vocabulary-is-usher-owned.md)). A skeleton
science-fiction film and an enriched one scored a hard **0** on this term while
both are science fiction. It cost nothing, because `_POPULATION` excludes
skeletons, so both sides of every stored pair spoke TMDb's vocabulary — and it
would have become real the moment the embedded population widened past the
enriched tier.

**`usher genres --backfill` removes the vocabulary half.** Both sides now speak
one alphabet whoever they are, which is a property of the column rather than of
who happens to be embedded, so widening the population no longer springs it.
The read-time expansion never reached here — `SimilarityService` reads the raw
column, as do `GenreAffinityProvider`, `TasteService`, `CurationPool`,
`BecauseYouWatched` and `Seasonal` — which is exactly why normalising the data
is what closes all six at once.

⚠️ **The other half is untouched and is the residue worth its own issue.**
`_jaccard` still cannot tell "these two share no genres" from "we do not know
either one's genres", and that is about a title whose `genres` is **empty** —
118,856 of them on this catalog. No vocabulary fixes an empty set; it needs the
absence-as-absence treatment the paragraph above describes, applied to a
set-valued field.

**The precompute is exact, not approximate**, and the argument is about the
artefact rather than about the cost: recall loss in a live query is per-query,
while recall loss in a cached artefact is permanent — a neighbour an ANN scan
missed is missed by every read of that row until the next rebuild.

**And this table is the one derived artefact whose freshness is not a per-row
predicate.** A title's neighbours go stale when *some other* title gets an
embedding, which nothing can decide without recomputing the row. So it carries
an **oldest-row `computed_at`** rather than a fingerprint, `None` means never
computed, and it is rebuilt rather than repaired. That is a weaker guarantee
than the rest of the search subsystem and is written down as weaker on purpose:
a freshness predicate that looked like the others and did not mean the same
thing would be worse than an honest gap. Nothing in M6 re-runs the rebuild —
`usher similar --rebuild` is an operator's command or a cron entry — so PRD 06's
"TTL: hours" is a statement about how long a consumer may cache what it read,
not a promise about this table's age.

**There is no fifth term over MovieLens *user tags*, and that is a measured
refusal rather than an omission —
[ADR-0035](decisions/0035-the-tags-similarity-term.md).** `ml-latest/tags.csv`
(21,274,899 rows / 85 MB) reaches **49,055** titles in this catalog against the
genome's 15,565, which is the reason a term over it looked worth building. M9
ran the question as a gate with one pre-registered threshold and walked the real
candidate pool once — **130,647 seeds, 13,064,700 candidate pairs, 2026-08-12** —
measuring the rate that decides a weight, the fraction of pairs carrying the
signal on **both** sides: **6.0821%** (794,606 pairs) at `>= 5` tags, **3.0999%**
at `>= 10`, against the **10%** floor a 0.25 weight assumes. Two things follow
and the second is the one that would otherwise be re-litigated. `>= 10` is
*lower* than `>= 5` **by construction** — those pairs are a strict subset over
an identical denominator — so a stricter threshold can never buy the rate. And
the rate is not the binding reason: on the marginal population the **median pool
pair shares no tag at all and 62.3% share none**, so a `_jaccard` that answers
`None` only for an *empty* set would hand `_blend` a hard `0.0` — a confident
negative — for most of the pairs the term fired on. **Presence with no overlap
is evidence over a closed ~19-value genre vocabulary and is the default over an
open user-tag one**, which is the same vocabulary-size argument this section
already makes for keeping genres and keywords apart, landing the other way. The
follow-up ADR-0035 names is a measurement (the rate at `>= 1` tag, the empty-
overlap share, and whether a different instrument over the same rows puts the
median firing pair above zero), not a build — and it explicitly is **not** "wait
for more enrichment": the archive is frozen at 2023-07-20 and movies-only, so
every further enrichment pass grows the denominator and moves coverage down.

### Mood queries

"Movies about isolation in space" is handled by embedding the query and
searching semantically. The cheaper, better-evidenced lever is **query
expansion**: one LLM call rewriting an emotional query into narrative language
before embedding, which measurably improves retrieval — one call per query,
rather than enriching 1.3M records.

🔴 **"Measurably improves retrieval" was the literature's claim and not this
project's, and on 2026-08-07 this project measured it and got the opposite
result.** The sentence above is kept because it is what was believed and acted
on for eight milestones; it is superseded by the run below. Query expansion is
**built and off by default behind its own setting**, and the two paragraphs
after this one are the evidence and the decision.

#### The measurement that reversed it

Run 2026-08-07 against the local vLLM serving `gemma-4-26b-a4b`. **5 mood
queries × 150 real TMDb overviews** for the 150 most-voted catalog titles,
embedded with the shipped `compose_document` and the shipped
`FastEmbedEmbedder` (`fastembed:BAAI/bge-small-en-v1.5`). **The targets were
written down before any cosine was computed.**

| | raw query | expanded |
|---|---|---|
| MRR | **0.733** | 0.373 |
| recall@10 | **0.800** | 0.533 |

The typed query wins **4 of the 5 queries** outright and ties the fifth.

**A label-free control says it is a mechanism rather than a bad draw.**
Pairwise cosine *between the five queries themselves* rises from **0.5417 to
0.5975 mean** and **0.6328 to 0.7784 max** after rewriting: five deliberately
distinct searches come back more alike than they went in. The top hit's
z-score falls in 3 of 5. The diagnosis follows from that — the rewrites are
generic critic prose (*"A dramatic exploration of profound isolation and
psychological survival…"*) which sits near the centre of a corpus of synopses,
so *Arrival*, *Seven*, *Requiem for a Dream* and *Prisoners* dominate the
expanded top-5 of **unrelated** queries.

⚠️ **The caveat is real and travels with the numbers: one model, one
150-document corpus, five queries.** It is thin evidence. It is also the *only*
evidence there is, against a claim that until now rested on the literature's
authority alone, so the default follows it. M9's `search_queries` is where a
real evaluation set — real typed queries, a full catalog, more than one model —
comes from, and it is what would reverse this back.

⚠️ **A third clause was added to that caveat on 2026-08-13: one *embedding*
model, and it is no longer the one that ships.** The diagnosis above — generic
critic prose collapsing toward the corpus centroid — is a claim about
`bge-small-en-v1.5`'s space, which `m09e` replaced
([ADR-0038](decisions/0038-the-embedding-width-is-deployment-wide-ddl.md)).
Nothing has re-run it. The default stays off, because a measurement is not
reversed by a change that did not repeat it.

✅ **Built in M8, off by default, and reported rather than substituted.**
M6 declined it deliberately — `ports/llm.py` declared `LLMClient` and
`LLMPurpose.QUERY_EXPANSION` with no implementation of that port anywhere in
`src/`, and adding a second unimplemented port dependency to the search path
bought nothing M6 could measure, so **M6 embedded the query exactly as
typed** (boundary call 6). M8 supplies the implementation ([ADR-0027](decisions/0027-the-llm-client-is-one-http-call.md))
and `usher.services.query_expansion.QueryExpansionService` is the wrapper the
seam was left for.

- **Where the call sits.** In front of `SearchService`'s embed, and nowhere
  else. So a `full_text` search buys no completion, a deployment with no
  embedder buys none (there is nothing to embed), a blank query buys none (it
  is refused before the model), and **`usher suggest` buys none** — type-ahead
  has no semantic lane, which is what keeps this off the one path a client
  drives per keystroke. The unit of spend is *one search that was going to
  embed something*, exactly as curation's is one generation.
- **Only the vector is computed from the rewrite.** `SearchRequest.query` is
  still the typed string, so under RRF the lexical lane goes on matching the
  viewer's own words while the semantic lane matches the paraphrase.
- **Off by default, behind its own setting.** `USHER_QUERY_EXPANSION_ENABLED`
  is `false` — including on a deployment that has set `USHER_LLM_ENABLED=true`
  and is curating happily — so `build_pipeline` builds no expander and the
  search path is byte for byte M6's. **The two switches are independent because
  the two spenders have opposite expected values**: curation works, and
  expansion measured worse (above). M8 Task 20 shipped one switch on the
  argument that *"a second setting's only honest default is 'follow the
  first'"*; that was sound while expansion was believed to help, and the
  measurement replaces it.

  The four combinations, of which three are reachable:

  | `USHER_LLM_ENABLED` | `USHER_QUERY_EXPANSION_ENABLED` | |
  |---|---|---|
  | `false` | `false` | The shipped default. No client, no curation, no expander; every search embeds the query as typed. |
  | `true` | `false` | Curated rows, and searches embedded as typed. `usher search` opens no completion client at all. |
  | `true` | `true` | Adds one completion per semantic or fused search that has a model to embed with. Opt-in. |
  | `false` | `true` | **Refused at startup**, naming both variables. With no client there is no completion to put in front of the embed, so this would be a knob that is on and means nothing — [08](08-operations.md)'s dead-config shape. |
- **Reported, never silently substituted, and the implication runs one way.**
  `SearchAnswer.expanded_query` is the text that was embedded, `None` when the
  query was embedded as typed, and `usher search` prints it above the results.
  A viewer who searched for one thing and got results for another cannot
  otherwise tell a good expansion from a bad one, and neither can an operator
  reading their bug report. **A populated field means a completion was bought;
  an absent one means nothing about spend** — a call answering with the wrong
  key is billed in full and still leaves the field `None`.
- **A failure narrows rather than fails** ([08](08-operations.md)): an
  unreachable endpoint, an unparseable answer or a rewrite that is blank or
  over `MAX_QUERY_CHARS` all leave the search to run on the typed query. The
  attempt is still billed — one `llm_calls` row per attempted call, `ok`
  derived from `error`, `generation_id` null because this purpose produces no
  rows ([10](10-telemetry-and-dashboards.md)).
- **Measured, and it is the reason for the setting** — see the run above.
  *(This bullet read "Not measured. The retrieval improvement above is the
  literature's, not this project's" until 2026-08-07. It stopped being true the
  day the measurement ran, and the measurement pointed the other way.)*
- ✅ **No longer billed on searches the semantic lane cannot serve — issue
  #16, closed.** The guard was `embedder is None` rather than *"anything is
  embedded"*, so on a deployment with a model and an empty `title_embeddings`
  (every deployment before its first `usher index --backfill`) a fused search
  with expansion on bought a completion, printed `expanded: …`, and *then*
  reported `semantic_coverage=0.000`: **the warning arrived after the money.**
  It is now `SearchIndex.semantic_coverage(filters) > 0.0`, asked immediately
  before the expansion and inside the same `else` — so `--mode full_text`, a
  blank query, `usher suggest` and a deployment with no model go on buying
  nothing, for the reasons they already did.

  ⚠️ **Two claims in this entry were wrong, and they are why it stayed open a
  milestone.** It said the filtered predicate is *"not answerable before the
  vector that does the filtering exists"* — it is. Nothing in a
  `SearchFilters` is derived from a query vector, and `_COVERAGE` already took
  predicates and no vector, so the honest question was answerable all along
  and merely had no spelling outside `search`. And it priced the fix as *"a
  new `TitleEmbeddingRepository` read … answering a weaker question"*: what
  landed is a `SearchIndex` method over the statement the answer already
  reports through, asking the **same** question rather than a weaker global
  one. **Before pricing a fix as needing a weaker predicate, check whether the
  strong one is already computed somewhere that simply is not callable yet.**

  **Its remaining cost is paid by an ordering rather than defended by an
  argument.** The probe sits behind `expander is not None`, which is false on
  every shipped deployment, so *"a read on every fused search"* is answered by
  an `and`: deployments that never expand never pay for it, and the ones that
  do trade one count over the enriched tier for one completion.

## Ranking

Retrieval is separated from ranking, deliberately:

1. **Retrieve** candidates (full-text, vector, or both fused).
2. **Rank** in application code — relevance, popularity, owned-vs-not, watch
   state, recency, taste-centroid proximity.

Owned titles are boosted but not exclusive: searching should surface things you
don't have, clearly marked, because that feeds discovery.

**All six terms ship as of M9** (`services/search.py`). Relevance, popularity
and owned-vs-not shipped in M6; **watch state, recency and taste-centroid
proximity landed in M9**, each with the seam it was waiting on now filled.

**`SearchService.search` takes a household** (`user_id`), which is the seam
watch state was blocked on for three milestones. It is a keyword on the method
and **`SearchFilters` remains a closed vocabulary with no user field**: every
field of `SearchFilters` is a flag on `usher search` and a query parameter on
`GET /search`, so a user there is a household any caller could name. Both
shipped callers resolve one before they search — the route through
`DefaultUserIdDep`, the CLI through `ensure_default_user` — so until PRD 01's
authentication seam is filled the household is the singleton default user and
no request is unpersonalised. Nothing on the wire reports which household
answered, deliberately: unlike a `fused` request degraded to full text, there
is no reachable alternative for a field to distinguish.

**Watch state is a small boost, never a demotion, and the direction is a
product judgement this PRD had left open.** A search is overwhelmingly a
re-find intent, so demoting what the household has finished buries the film
they just typed the name of; `RediscoverProvider` already treats a finished
title as re-offerable. It reads `WatchStateRepository.played_title_ids`, which
rolls a watched episode up to its series, so a television household is not
answered films-only. The opposite reading is defensible for *discovery* and
renders identically, which is why the choice is written down at the constant.

🔶 **Recency's constant is chosen with an argument, not measured.** The term is
`1 / (1 + age / 25 years)` over `release_date` where the enriched tier has one
and `year` otherwise, **absent and never zero when both are null**
([ADR-0014](decisions/0014-absence-is-not-zero.md), in a fifth place). Twenty-
five years is where the curve should be steepest for a distinction a viewer
would recognise; nothing measures it. **The double-counting caveat stands
unresolved beside it** — TMDb's `popularity` is a rolling engagement figure
that already leans recent, so the two terms are not independent — and what
would settle both is `search_queries`
([10](10-telemetry-and-dashboards.md)), which has no rows until after M9
ships. The term ships anyway rather than leaving "three ranking terms" at two,
and it is bounded so a wrong constant moves a score by at most its weight.

**The weights are constrained rather than chosen freely, and the constraint is
stateable.** The non-relevance weights sum **strictly** below half the
relevance weight, so no combination of ownership, popularity, watch state,
recency and taste can displace an exact match — 0.70 against 0.35 + 0.15 +
0.15 + 0.02 + 0.02 + 0.005. The three M6 weights keep their exact ratio, so a
hit with no popularity, no year and no household scores what M6 scored it; the
blend renormalises over present signals, so adding a term moves only the rows
that term is present on.

🔴 **"Strictly" is a measurement, not a stylistic tightening.** The headroom
left after the five M9 weights is 0.35 − 0.34 = 0.01, and **0.01 itself is not
available**: taken exactly, the challenger's numerator `0.35 + 0.15 + 0.15 +
0.02 + 0.02 + 0.01` is **0.7000000000000001** in IEEE-754 doubles — one ulp
*above* 0.70 — so the rank-1 hit with every signal maximally for it sorts
first and the property above fails. Not a tie broken by id: an inversion, and
one only a case built at that exact corner can see. The usable interval is the
open `(0, 0.01)`; the taste weight is its midpoint. Pinned by
`test_no_combination_of_the_other_five_can_displace_an_exact_match`, which
asserts the arithmetic rather than an ordering — a re-weighting that reorders
nothing changes every score on the wire and is invisible to any number of
ordering cases (M9 F4 measured this: `owned` 0.15 → 0.10 left all ten green).

🔴 **Rank 0 is therefore a pure function of the lexical score — and the
lexical score was putting the wrong row there.** The bound above says nothing
about *which* title the index ranked first; it only says the blend cannot
argue with it. `GET /search?q=The Matrix` returned the 1999 film **fifth**
(0.3501) behind three 2018 video essays repeating the phrase in their own names
(0.8032 each), and popularity was applied and *helped* — without it the film
scores 0.2729. `ts_rank_cd` rewards a document that repeats the query; it has
no idea that the query **is** a title's whole name
([#25](https://github.com/anirudhlath/usher/issues/25)).

**So the lexical lane carries an exact-name key, ahead of its own score.**
`SearchHit.exact_name` is `lower(name) = lower(btrim(query))`, computed in the
lexical statement, ordered ahead of `ts_rank_cd` *and ahead of the `LIMIT`* —
a title whose name is a common phrase could otherwise fall outside the
candidate window and never reach the ranker at all, which no re-weighting
reaches. `SearchService._dense_ranks` then reads it as the leading key, so an
exact match is **alone** at dense rank 0 rather than sharing it: `ts_rank_cd`
ties are pervasive (a tie group of 498 among the top 500 values for one query),
and a shared rank 0 cancels the relevance term and hands the decision back to
popularity. **Every weight above is unchanged and so is the bound** — this is
candidate 1 of the issue's three precisely because candidates 2 and 3 are not:
capping the relevance decay would make rank-0 dominance contestable and
invalidate the taste ceiling derived from it, and breaking ties inside the lane
is narrower than the defect, since the essays *outscore* the film rather than
tying it.

**Measured against a bar written before the run**, over 800 titles drawn from
the live 1,272,866-title catalog (400 `skeleton`, 400 `enriched`), each queried
by its own name through the shipped path: the exact-name miss rate falls
**38.4% → 20.8%**, and the class the defect is about — *retrievable, uniquely
named, and outranked anyway* — falls **234 → 0 of 800 (29.3% → 0.0%)**. Nothing
regressed: of the 493 titles already at rank 1, **493** still are. The 166
remaining misses are 155 titles that lost to a **namesake** (another title
carrying the identical name, which no name-based signal can separate) and 11
that never match their own name at all — a name of nothing but stop words (`In
Between`), or one containing ` - `, which `websearch_to_tsquery` reads as
**negation**: `Regret - Cherie Laurent` compiles to `'regret' & !'cheri' &
'laurent'`. Both are retrieval defects rather than ranking ones and are
recorded, unfixed, in `.claude/rules/search-and-embeddings.md`.

**The exact-name key is deliberately not tier-1 suggest's prefix key**, though
it is the same rule at a different strength — `GET /search/suggest?tier=prefix`
already answered this query correctly, which is what identified the signal. The
three essays are *themselves* prefix matches of `The Matrix`, so a prefix key
would flag all four rows alike and separate none of them, while promoting every
`Matrix Warrior` above `The Matrix` on the query `Matrix`. On tier 1 the whole
candidate set is prefix matches and popularity does the ordering; here the set
is mixed. `mode=semantic` carries no exact-name key either: that statement is
handed a vector and no text, and a `lower(name) =` predicate there would be a
lexical signal inside the lane that exists not to have one.

**Taste-centroid proximity is a term, and it is *read* rather than computed.**
`TasteService.centroid` needs an embedder and a request holds none
([ADR-0022](decisions/0022-the-embedder-is-optional-and-its-contract-is-measured.md)),
so routing the term through it would have shipped a weight that is inert on
the shipped default — the failure [06](06-rows-and-recommendations.md) already
corrected once, for `GenreAffinityProvider`. `TasteRepository.latest(user_id)`
answers the household's stored `user_taste` row **whatever model wrote it**,
read-only and with no staleness predicate: the predicate answers *"should I
recompute?"*, which a process with no model cannot act on, and inheriting it
would withhold the term from exactly the households that watch things.
`centroid()` is untouched — it still refuses without an embedder and still
writes its refusals — and read-only is what stops a request minting a
`user_taste` row under a model it does not have.

**The stored row's `model_name` is the filter on the other side.**
`TitleEmbeddingRepository.list_for_titles` gained a keyword-only, optional
`model_name`; the ranking path passes the centroid's, and `TasteService.
centroid` and `CandidatePoolService` keep the unscoped call they argue for.
Comparing a centroid computed under one checkpoint against vectors stored
under another is the ST-vs-fastembed divergence — max pairwise-similarity delta
1.41e-03, 6x the halfvec quantisation error — arriving as a confident cosine.

🔶 **The term is `max(0, cos)` clamped into [0, 1], and `None` — never 0.0 —
when there is no centroid or no vector under that model**
([ADR-0014](decisions/0014-absence-is-not-zero.md), in a sixth place). A zero
cosine is a real orthogonality claim about two vectors; "no worker has run" and
"the backfill has not reached this title" are not claims about a title at all,
and the absent case is the population rather than a corner — that sentence read
*"`title_embeddings` is currently empty on every catalog this project holds"*
until M9's S3/S4 filled the priority tier, and it is still the population
afterwards: **130,647 of 1,272,367 titles (10.3%)** carry a vector, so nine
titles in ten reach this term with nothing under that model. **What 0.005 can
move is small and is stated rather than implied**: it cannot overturn `owned`
or `played` at any cosine gap, and it overturns one step of relevance only from
about rank 11 downward even at an impossible cosine gap of 1.0. Where it
decides is where the other five have already tied, which equal index scores
(one dense rank) make ordinary. **The magnitude was set by a full weight table,
not by a measurement of what taste proximity is worth** — the alternatives were
to take weight from popularity or owned, or to raise relevance's share, and
both end the M6 byte-for-byte claim above to buy a larger weight for the
weakest-evidenced term in the set.

**Relevance enters the blend as a rank, never as a raw score.** A `ts_rank` is
around 0.06, an RRF score around 0.016–0.033 and a cosine is in [-1, 1];
adding any of those to a popularity term in [0, 1) is
[ADR-0002](decisions/0002-postgres-first-search.md)'s incompatible-scale
prohibition committed one layer up, where the SQL-side rule cannot see it. The
service reads the outcome as an *ordering* and derives `1 / (1 + rank)` from
the position, with equal index scores sharing a rank.

**An absent signal is excluded from the blend, not scored zero.**
`titles.popularity` is null for every title TMDb has never described — **77.1%
of a `--phase all` catalog and 100% of a `--phase imdb` one**, measured above
rather than described as "most of it" — and `popularity or 0.0` would rank a
title nobody measured
identically to one measured as unpopular — the same rule
[ADR-0014](decisions/0014-absence-is-not-zero.md) states for watch
history, applied to a ranking term. The observable consequence: at equal
relevance, unknown popularity ranks above a measured zero.

**"Owned" has one definition, and both consumers cite it.** A copy the nightly
availability sweep retracted (`available = false`, PRD 02's soft delete) still
counts, because a ranking that flipped when a source went down would move
results for a reason unconnected to the query; and the read is restricted to a
title's own `media_items` row (`episode_id IS NULL`), which costs the bound
that a library reporting episodes but never their series row reads as not-owned
for that series. The `owned_only` *filter* and the owned *boost* are the same
predicate on purpose — two definitions is how a filtered list and a boosted
list stop agreeing.

## The upgrade path

`SearchIndex` is an ABC. Adding Meilisearch means implementing it once; nothing
above the port changes.

> **Settled in M6.** All four named defects are fixed, and the port is now a
> candidate-generation contract rather than a description of Postgres's own
> operations.
>
> - `index(title_id)` became **`index_many(documents)`** — the port takes a
>   `SearchDocument` the *service* assembles from a `Title` it already holds,
>   so no implementation ever fetches a title back out.
> - `SearchRequest.filters: dict[str, Any]` became **`SearchFilters`**, a
>   frozen dataclass with a closed vocabulary (`kinds`, `year_from`,
>   `year_to`, `genres`, `owned_only`, `min_enrichment`). A backend that
>   cannot express one **raises** rather than ignoring it, because an ignored
>   filter returns *more* results and reads as working.
> - `SearchRequest` gained **`query_vector`**, computed by the caller — which
>   is what makes the port engine-neutral and simultaneously settles who
>   applies a model's query prefix (nobody: see `### Semantic`).
> - **No `rebuild`, deliberately.** It would be a second path to the same
>   state, exercised only by an operator, and the predicate-driven backfill
>   already rebuilds from scratch by construction. A port method whose only
>   test is its own test is a liability.
>
> The fifth change is the split above: `suggest` left this port entirely
> ([ADR-0021](decisions/0021-the-suggest-path-is-its-own-port.md)).

**The gate is measurable, not a judgement call.** Build a typo test set from
real catalog titles, weighted toward short names — `Up`, `Her`, `Dune`, `Alien`
— where trigram similarity is genuinely weak (a four-character word yields ~5
trigrams; one typo destroys most of them, and transpositions are close to a
blind spot). If recall@5 on that set falls below the bar after honest tuning,
add Meilisearch for the instant-search box only.

**The gate was run on 2026-08-03 against a real 1,271,138-title catalog, and
it failed.** 2,993 single-edit typo cases over 750 real movie names — five
equal-sized length bands, `vote_count ≥ 500`, 81,054 non-unique lower-cased
names excluded, four typo classes at a uniformly random position, seed
20260803 — driven through the shipped `PostgresSuggestIndex`. Full tables,
the bar as it was written down beforehand, the miss diagnosis and the
regeneration procedure are in
[ADR-0002](decisions/0002-postgres-first-search.md)'s "Evidence — the gate,
measured". The headline, per typo class and length band, for the shipped
path:

| name length | substitution | deletion | transposition | doubled letter | all | n per cell |
|---|---|---|---|---|---|---|
| 2–4 | 19.3% | 12.5% | **0.0%** | 78.7% | **27.8%** | 144–150 |
| 5–7 | 90.7% | 48.0% | 35.3% | 99.3% | **68.3%** | 150 |
| 8–11 | 99.3% | 88.7% | 94.7% | 99.3% | **95.5%** | 150 |
| 12–19 | 100.0% | 99.3% | 100.0% | 100.0% | **99.8%** | 150 |
| 20+ | 99.3% | 98.7% | 100.0% | 100.0% | **99.5%** | 150 |
| **all** | **81.7%** | **69.9%** | **66.1%** | **95.5%** | **78.3%** | 2,993 |

p50 33.3 ms, p95 208.8 ms, max 734 ms. The best configuration found under any
threshold, any cap and either index type reaches **85.3% overall and 47.9% on
the 2–4 band at p95 304 ms** — so the failure is not a tuning oversight.
**This section's own examples were exact**: `similarity('dune','dnue') = 0.111`,
`('her','hor') = 0.143`, `('up','uo') = 0.200`, all below the 0.3 default, and
transposition on a 2–4-character name measures **0.0%** — a total blind spot,
not merely a near one.

**Above 8 characters it works and needs nothing** — 95–100% at every typo
class, which is 91% of this catalog by row count. **The failure is the short
one-word name**, which is where this section always said it would be.

**M6 does not add Meilisearch** (boundary call 7): a second stateful service
bolted on at the end of a milestone is not what a measurement with a decision
attached is for. What the numbers support instead, and what
[09](09-roadmap.md) gives an owner to, is a **two-tier suggest**: a btree
`lower(name) text_pattern_ops` prefix probe on every keystroke — measured at
**p50 0.6 ms / p95 1.0 ms / max 10 ms** over the same 2,993 queries, 200–330×
faster than any fuzzy configuration and the only thing measured that fits
inside a keystroke — with the trigram + `levenshtein_less_equal` path
**debounced behind it**. They are complements: the btree has no typo
tolerance at all (1.9%) and the trigram path cannot meet a keystroke budget
at any setting.

✅ **Tier 1 is built.** `PostgresPrefixSuggestIndex`
(`adapters/search/prefix.py`) is the probe: `lower(name) LIKE 'typed%'` over
`titles` **and** `title_search_names` as one `UNION`, so a person's name
reaches their films from the first keystroke, ordered by the same three keys
tier 2 uses under its distance (`popularity DESC NULLS LAST, vote_count DESC
NULLS LAST, id ASC`) so the box does not reshuffle when the debounced tier
arrives behind it. It reads the two `text_pattern_ops` indexes `m09a` ships and
**writes nothing**, so ADR-0021's dual-write cost is still unpaid by a second
implementation of that port.

✅ **The shipped statement is now measured, and the union stays.** B3 ran it on
2026-08-12 against the gate's own 1,271,138-title catalog with a
`title_search_names` **person** arm of 10,896,525 rows over 1,191,768 titles
(the `alias` arm is still empty — T7's), on a box verified quiet, against a bar
committed before the run. Over the gate's 2,993 typo strings — the only
workload comparable to the 0.6 ms figure above — the union answers at **p95
1.465 ms** against a 10 ms bar, and `titles` alone at **0.947 ms**, reproducing
the probe figure almost exactly. So the union does **not** cost tier 1 its
budget, and the narrowing B3 was authorised to make is **not** made.

🔶 **Tier 1 is a keystroke path from seven characters up and nowhere below
it**, and that is the finding B5 has to design around rather than inherit.
p95 by prefix length, union against `titles` alone:

| prefix length | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| `titles` only | **291 ms** | 51 ms | 15 ms | 5 ms | 19 ms | 14 ms | 2.0 ms | 2.3 ms |
| union | **2,707 ms** | 809 ms | 303 ms | 112 ms | 100 ms | 86 ms | 2.3 ms | 2.6 ms |

Both arms miss a 10 ms keystroke budget below seven characters, so **narrowing
the union would not have bought a keystroke path** — it would have moved a
291 ms first keystroke to where a 2,707 ms one had been. **The mechanism is not
the sort**, which is a top-N heapsort in 26 kB: it is the `UNION`'s
de-duplication spilling 47 MB to disk and a bitmap heap scan going lossy
(5,664,971 rows rechecked to keep 1,069,834). An *ordered* inner per-arm cap is
therefore much cheaper than it was priced at, because it would bound the
de-duplication's input rather than pay a sort that is already free — **not
made here**, because B3 measures and does not tune. Coverage is the other
lever and the curve is steep: at the 10,000-title enriched tier the same
one-character probe is **489 ms**, and by four characters **5.5 ms**.

✅ **Tier 2 is on the wire beside it, and the minimum prefix length is
decided.** `GET /search/suggest?q=&tier=prefix|fuzzy&limit=` is one route with
two separately-askable tiers, defaulting to `prefix`, echoing the tier that
answered — and **declining to run tier 1 below a four-character prefix**, where
the answer is `200` with no results, no query issued, and a
`min_query_length` on every response so an empty box is legible and a client
can apply the same rule without sending the request at all.

**Four is derived from the curve above rather than chosen**: it is the shortest
prefix at which tier 1's p95 is below tier 2's (112 ms against 211 ms; at three
characters tier 1 is 303 ms and therefore *slower* than the tier it exists to be
cheaper than). It is deliberately **not** the 10 ms keystroke bar, which is met
only from seven characters up and which would leave the keystroke tier
answering nothing for most of a typed word — abandoning the short one-word names
that made the gate fail. Tier 2 is bounded at one character only, because
nobody has measured *it* per prefix length and its defence is the client's
debounce; **the server debounces nothing**. `usher suggest --tier` defaults to
`fuzzy` and has no minimum at all, because a command is typed once.

The whole argument, the alternatives — two routes with different cache TTLs, an
ordered inner per-arm cap (now known to be much cheaper than it was priced at,
and the first thing a follow-up should measure), a minimum of seven — and the
two bars B3 failed with their attributions are in
[ADR-0031](decisions/0031-the-two-tier-suggest.md).

**The gate as this section defined it measured the wrong half, and that
correction stands.** A synthetic dry run over 604 cases first showed it, and
the real run confirmed the shape: recall is the half that is arguable, and a
configuration can look acceptable on recall while being 4–6× too slow for the
box it exists to serve. Both dimensions are now recorded together, per cell,
with sample sizes.

If Meilisearch is ever taken: precompute embeddings and use `userProvided`,
run ≥ v1.39 (a memory leak existed from v1.12–v1.38), configure
`filterableAttributes` granularly *before* loading documents (changing them
forces a full reindex), and hydrate hits from Postgres by ID so stale index
entries are invisible.

**Typesense is ruled out** regardless: fully memory-resident with no on-disk
mode, so every restart returns HTTP 503 for 2–15 minutes while it rebuilds. The
maintainers have explicitly declined to fix this outside a 3-node cluster. That
is a poor fit for a home server that reboots for kernel and driver updates.
