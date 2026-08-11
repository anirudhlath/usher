---
paths:
  - "src/usher/adapters/bulk/**"
  - "src/usher/services/bootstrap.py"
---

# IMDb, TMDb id exports, Wikidata and MovieLens

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**`links.csv`'s `tmdbId` is NOT unique — 162 duplicate rows over 38 ids —
while `imdbId` is.** So the genome joins `titles` through
`'tt' || lpad(imdbId, 7, '0')` and **never** through `tmdbId`: a `tmdbId` join
fans one TMDb id out across several MovieLens movies and attaches one film's
genome vector to another's title, on ids that are all real. The `lpad` is the
second half and is not decoration — over all 86,537 rows `imdbId` is 7
characters wide on 79,978 and 8 on 6,559, never shorter and never empty, so
`'tt' || imdbId` happens to be correct *today* while silently depending on a
padding convention the file documents nowhere, and one unpadded row would join
to nothing rather than raise. Same family as M4's 11-of-885 bare-digit `Imdb`
values.
**`genome-scores.csv`'s physical grouping is a property of the snapshot, not a
promise, and the importer verifies it.** 16,376 contiguous `movieId` runs,
strictly increasing, every run exactly 1,128 rows with `tagId` 1…1128 — which
is what makes a single-pass streaming importer possible without buffering an
18.5M-row matrix. A run of the wrong length, a duplicate `tagId` within a run,
or a `movieId` that reappears after its run closed is a **hard failure naming
the offending `movieId`**. What is deliberately *not* enforced is `tagId` ordering
*within* a run: the vector is built by index rather than by append, and the
case that proves that shuffles a run and expects the right vector — so
enforcing the observed order would make the by-index property unprovable. The
run check is `len(run) == n and |{tagIds}| == n`.
**`genome-tags.csv` is 1,128 rows and every one of them is usable, measured
2026-08-07 through the shipped `CachedDatasetFile.member_lines`.** `tagId` is
exactly `1...1128` and already ascending; every name is non-empty; **no name
contains a comma**; all 1,128 are distinct; the longest is 65 characters; and
the member is **CRLF**-terminated (8,359 compressed / 18,103 uncompressed
bytes). Two of those matter to the parser. The CRLF is invisible because
`member_lines` decodes through `io.TextIOWrapper` in universal-newline mode,
so the `\r` is gone before `rstrip("\n")` runs -- **verified by reading, not
assumed**, since a stray `\r` would land in every stored tag name. And the
comma-free names are a property of *this snapshot*, not a promise, which is
why the parse is `partition(",")` rather than the `csv` module: only the first
comma separates. `partition` rather than `split(",", 1)` because a row with no
comma at all has to be distinguishable, and `split` hands that back as a
one-element list whose `[0]` parses as a perfectly good `tagId` -- the shipped
`_tag_count` read exactly that field and was blind to it, which was harmless
while the names were being thrown away and is not now that `m08b` stores them.

**Two follow-ups from the review of that measurement, 2026-08-07.** The
universal-newline property was asserted in *three* source files and covered by
no test — every fixture in `tests/unit/test_adapters_bulk_movielens.py` is
built with `"\n".join(...)`, so `newline=""` on that `TextIOWrapper` passed all
2,882 unit cases while putting a trailing `\r` on all 1,128 stored names.
`test_a_crlf_bodied_member_stores_no_carriage_return_in_a_tag_name` is the
CRLF-bodied fixture that closes it, and it is the only case in the suite that
fails against that plant. And the empty-name refusal is now `not name.strip()`,
not `not name`: `ck_genome_tags_tag_not_empty` is `tag <> ''`, which accepts
`'   '`, so before the parser was tightened a whitespace-only lane would have
been stored by both layers. Unreachable in the measured file (all 1,128 names
are `strip()`-stable), which is why the CHECK was left alone rather than
re-spelled `btrim(tag) <> ''` in a migration for a value nothing can produce.

**`ml-32m` has no genome and `ml-25m` may not be redistributed, so `ml-latest`
is forced rather than preferred.** `ml-32m.zip` (05/2024) is the newest full
release and dropped the genome entirely — four members only. `ml-25m` has one
and is the only genome-bearing archive whose licence says *"the user may not
redistribute the data without separate permission."* `ml-latest` has a genome
**and** the permissive clause. Three of PRD 04's numbers were wrong on the
strength of the archive confusion: **18,472,128** relevance scores (not
"15.6M"), **334.6 MiB** (not "250 MiB" — that is `ml-25m`'s size, the right
number for the wrong archive, which is why it survived review), and **16,376**
movies (not 13,816). The 1,128 tags was exactly right. **Measured, `ml-latest`
has not moved in three years** (`Last-Modified: Thu, 20 Jul 2023 20:20:32 GMT`)
despite its own README calling it a *development* dataset — the same
`CachedDatasetFile` hazard shape as IMDb's daily regeneration, opposite
conclusion. Range-fetching only the three members the importer reads (~96 MB of
335 MB) is possible and was **measured and declined**: re-implementing resume,
`If-Range` and the stale-snapshot interlock against per-member local headers is
new failure surface for a saving an operator pays once.
**The bootstrap that produced the catalog, and a cache finding with it.**
74.8 s wall clock end to end (`title.basics` 41.6 s → 1,271,138 rows,
`title.ratings` 22.2 s → 538,937 rows written, suspended-index rebuild
10.9 s), 899,828 movies / 371,310 series, `titles` at 928 MB total relation
size and the database at 937 MB. **Nothing was re-downloaded — but only
because the run pinned the snapshot, and the shipped path would have
re-downloaded.** `CachedDatasetFile.ensure_local` short-circuits on
`path.exists() and stamp.read_text() == revision`, where `revision` is what
`revision()` resolved *this run* from a `HEAD`. IMDb regenerates
`title.basics.tsv.gz` daily, so four days after the cache was filled the
upstream ETag had moved and `bootstrap --phase imdb` would have fetched
224 MB and imported a *different* snapshot. Pinning `revision()` to the
sidecar's own value made the import read the cached bytes: NIC counters moved
**1.2 MB** across the whole bootstrap and `data/bulk` was byte-identical
before and after. **"The dumps are on disk, so nothing re-downloads" is false
as stated** — the cache is keyed on the upstream token, not on local
presence.
**IMDb TSVs have no quoting mechanism** and their title fields contain
literal `"` (21 in the first 553,395 rows of `title.basics.tsv.gz`).
`csv.reader`'s default `QUOTE_MINIMAL` silently strips them — verified. Parse
with `line.split("\t")`. Re-confirmed on the other three files 2026-08-11:
across 101,151,422 + 15,563,615 + 58,906,368 data rows, **zero** lines split to
the wrong column count, so a wrong count is a real signal in every one of them
and not noise to be tolerated.

**`title.akas`'s parser-level shape, measured over the whole pinned file
rather than over the catalog-retained slice of it — 2026-08-11 (M9 T5).** T3's
figures are all *retained* ones (a `titleId` the catalog holds); these are over
all **58,906,368 data rows** of `"19810e3eb2b0f1fa774bf4e4af94d7c6-61"`,
because a parser sees every row and cannot know which titles exist. Header is
exactly `titleId ordering title region language types attributes
isOriginalTitle` — 8 columns, and **zero rows split to any other count**.

- **`isOriginalTitle` is 21.6% of the file — 12,703,704 rows — and not one of
  them carries a `region`** (0 of 12,703,704). `types` reads exactly `original`
  on all of them, and the two counts are *identical*, so in this snapshot the
  flag and the type are one signal spelled twice. There is no `\N` and no `\r`
  in that column: the vocabulary is exactly `0` and `1`.
- **33 rows exceed `SEARCH_NAME_MAX_CHARS` (512) and the longest title is 831
  characters.** T3 measured 0 among the retained rows and that still holds
  (0 of 7,541,357 on a re-run), so all 33 belong to titles outside the catalog
  — *today*. `SearchNameRepository`'s contract refuses an over-long name for
  the **whole call**, so the parser drops them rather than letting one row take
  a ten-thousand-row batch with it.
- **`title` is never empty and never `\N`** — 0 of 58,906,368. The empty-name
  drop is unreachable in this snapshot and exists because
  `ck_title_search_names_name_not_empty` is `name <> ''`.
- **39,880 titles contain a literal `"` and 6,344 open with one**, so
  `csv.reader`'s `QUOTE_MINIMAL` would silently rewrite 6,344 alias names.
  This is the third file the finding has been confirmed on and by far the
  largest count.
- **`types` and `attributes` are multi-valued inside one tab-delimited column
  and the separator is `\x02`** — 429 rows carry a two-valued `types`,
  commonest `imdbDisplay\x02dvd` (207). A reader assuming a tab there would
  call those 429 rows nine-column and malformed. 23 distinct `types` values,
  185 distinct free-text `attributes` values, 251 distinct regions whose seven
  largest (IN, DE, JP, FR, ES, IT, PT) are 5.4–5.8M rows each.
- **`ordering` is present and integral on every row**, min 1, max 300.
- **12,748,984 rows carry no `region` and 19,243,152 no `language`**, and they
  are not the same rows.

**And the decisive one, which settles whether an `isOriginalTitle` filter
belongs in the parser: it costs 7 aliases.** Joining the pinned akas file to a
1,272,367-title catalog built from this host's cached `title.basics`
(`"128751cb2f3132bd73bdf08c7f4def5d-27"` — **a different upstream snapshot**,
which is the same not-one-snapshot hazard recorded below and is why the
numbers differ from T3's by ~0.07%): 7,541,357 akas rows are in the catalog,
1,272,135 of them are flagged, and **1,272,111 (99.998%) casefold-equal the
title's own `name` or `original_name`**. Only **24** disagree, and 17 of those
repeat a name a non-flagged row already carries — so after deduplicating on
`(title_id, casefold(name))` the alias total goes 1,663,330 → 1,663,323, a
loss of **7**. The hazard that would have made the filter unsafe is
empirically zero: **0 of the 1,272,367 catalog titles have no
`originalTitle`**, so there is no title whose flagged aka is its only carrier.
**The filter is a cheap prefix of the writer's rule and never a substitute** —
of the 6,269,222 retained rows that survive it, **4,426,783 (70.6%) still
casefold-equal the title's own name**, and only a comparison against the
stored `Title` can see that.

**The plan's "heavily-filtered file yields row-less batches" risk is inverted
by the measurement.** It was written for three files at once; of the one that
survives, `title.akas` keeps **78.4%** of its lines, which makes it the
*least* filtered dataset `_ImdbDataset` has ever streamed — `title.basics`
keeps 1,271,138 of 12,678,891, i.e. **10.0%**, and has shipped that way since
M2. So `_ImdbDataset` was not changed to emit cursor-advancing empty batches:
a trailing run of filtered lines costs a re-read on resume and never a lost
row, because `position` counts lines consumed and every downstream write is an
upsert.

**`title.principals` + `name.basics` will not fit a `people`/`credits` design
for this catalog, and the refusal is a size measurement rather than a row
count — measured 2026-08-11 (M9 T3).** The bar was written to
`/tmp/m9-t3/BAR.md` before the first byte was downloaded, transcribed from the
plan: **(A)** people + credits is affordable only if retained credits ≤ 20M
**and** added relation size including indexes ≤ 2.0 GB **and** `people` ≤ 6M;
**(B)** akas only if retained deduplicated aliases ≤ 8M **and** ≤ 1.0 GB;
**(C)** if (A) fails the fallback is the names-only design and *the deliverable
is the recorded refusal, not a shrunken (A)*.

**(A) fails. (B) passes.** Two of (A)'s three clauses pass comfortably —
**12,626,452 retained credits** of a 20M ceiling and **3,211,941 people** of a
6M one — and the third fails on both readings of its unit: **2,701,697,024 B,
i.e. 2.702 GB or 2.516 GiB, against a 2.0 GB bar**. It is not a fat column.
Stripped to the five load-bearing columns `(id, person_id, title_id, kind,
billing_order)` — no `character`, no `job`, no `department`, no
`tmdb_credit_id`, no `created_at` — and carrying only its primary key and the
two foreign-key indexes, the same 12,625,259 rows plus `people` still measure
**2,395,414,528 B (2.395 GB / 2.231 GiB)**, over the bar by 20%. The whole text
payload it sheds to get there is 89,306,409 B of `character` and 20,325,470 B
of `job` — **110 MB against an overrun of 702 MB**, so deleting every string in
the table still leaves it 592 MB over. The cost is 12.6M tuple headers and
three uuids apiece, so *there is no version of (A) that fits by trimming*,
which is exactly why (C) was written first. Nor does the shipped `credits`
shape even support the load: its only unique key is on `tmdb_credit_id`, which
is NULL on every IMDb row, and the obvious idempotency index `(title_id,
person_id, kind)` **cannot be UNIQUE** — 1,341,798 retained credits collide on
it — while adding a further **682,950,656 B (651.3 MiB)**.

**The consequence of (A) failing, taken here rather than deferred.** The IMDb
half of M9's Track 2 falls back to the **names-only design**:
`titles.credit_names` is filled directly from `title.principals` ×
`name.basics` with **no `people` and no `credits` rows written from IMDb at
all**, so the two bulk sources never own one entity and the question that
design existed to answer does not arise. T4's provenance rule, T6's credits
write and **the `m09b` migration grant are withdrawn** — the names-only design
mints no table and needs no revision, and `m09c` stays spare and still has to
be *requested*. T5 keeps only its `title.akas` parser. T7 and T8 re-scope to
aliases and `credit_names` alone. `people`/`credits` remain exactly what M7
made them: TMDb-derived, enriched-tier only, never bulk-loaded.

**(B) passes both clauses — 4.8× under on rows, 3.2× under on bytes — and the
reason is that three akas in four are not aliases at all.** Of the 7,536,366
retained akas rows, **5,693,570
(75.5%) casefold-equal the title's own `name` or `original_name`** and carry no
information a `titles` prefix index does not already hold; 1,842,796 survive
that, and deduplicating on `(title_id, casefold(name))` leaves **1,663,364** —
against an 8M ceiling — in **307,822,592 B (0.308 GB / 0.287 GiB)** at
`m09a`'s exact `title_search_names` shape, both indexes included, against a
1.0 GB ceiling. **Only 399,046 of 1,271,138 titles (31.4%) gain even one
alias**, so the alias half is a narrow, cheap win rather than a broad one.
1,653,088 of the survivors carry a `region` and 410,634 a `language`, which is
what those two columns are for. Zero retained akas rows are empty and **zero
exceed `SEARCH_NAME_MAX_CHARS`** (512), so `m09a`'s btree-bound CHECK rejects
nothing in this snapshot.

**Denominators for everything above**, all against the same 1,271,138-title
catalog: 12,626,452 of 101,151,422 principals rows retained (12.5%);
**1,192,241 of 1,271,138 titles (93.8%) have ≥1 principal**; 3,212,911 distinct
`nconst` referenced, of which 3,211,942 appear in `name.basics` and 3,211,941
carry a `primaryName` — **one referenced person in the whole file has none**.
That is also why the 12,626,452 retained principals store only **12,625,259**
credits: 1,193 of them name one of the 970 `nconst` that resolve to no usable
person, and a credit whose person cannot be stored is dropped rather than
orphaned. 7,536,366 of 58,906,368 akas rows retained (12.8%), over **1,270,074
of 1,271,138 titles (99.92%)** before the canonical-name filter.

**The snapshot was pinned and the pin is the finding's date.**
`title.principals` `"08ce60665889cb40c7371e1eab44a1f2-93"`, `name.basics`
`"a3b9681921c92e5917182d1ecc05bd2d-37"`, `title.akas`
`"19810e3eb2b0f1fa774bf4e4af94d7c6-61"`; each file's on-disk length equals the
`Content-Length` its own `HEAD` reported and `gzip -t` validates all three
whole streams, so nothing here is measured over a truncation.
**IMDb does not regenerate the seven files together**, which the pin made
visible: five carried `Last-Modified: Tue, 11 Aug 2026 00:47–00:48 GMT` and
`name.basics`/`title.akas` carried `Mon, 10 Aug 2026 12:53 GMT`. The
consequence is measurable rather than theoretical — **969 `nconst` referenced
by that day's `title.principals` do not exist in that day's `name.basics`** —
so any importer joining across these files must treat a dangling `nconst` as
routine and drop the credit, not raise.

**Filling `titles.credit_names` costs four times more in bytes than the names
themselves weigh — measured on a real 1,271,138-row copy of `titles` at the
`m09a` schema, not estimated.** **1,192,217 of 1,271,138 titles (93.8%) gain a
non-empty `credit_names`**, mean **9.11** names each, 10,862,893 names and
**158,479,368 B of text**. `titles`' total relation size goes **872,759,296 B →
1,496,825,856 B after `VACUUM FULL`: +624,066,560 B, +71.5%**, i.e. 3.9 bytes
stored for every byte of name. Decomposed, and the third term is a confound
worth stating rather than hiding: heap + toast **+559,407,104 B (+126.9%)**;
`ix_titles_search_document` **4.54×, 40,304,640 → 182,951,936 B
(+142,647,296)**, which is the class-B lexemes; and the remaining nine indexes
**net −77,987,840 B**, because `VACUUM FULL` rebuilds every index by sort while
the baseline's btrees were built incrementally by `COPY`. So the honest
statement is that the fill costs ~624 MB *net of* an index-rebuild saving that
an operator only collects if they actually run the vacuum.
The *transient* figure is the one an operator's disk sees, and it is more than
double the settled one: before any vacuum the same table is **2,240,970,752 B,
+1,368,211,456 B over baseline** (GIN alone at 208,003,072), because a single
`UPDATE` of 1.19M rows leaves a dead tuple for every live one. **That peak, not
the settled 624 MB, is the number to budget against PRD 08's 8–12 GB.**
**And the embedding blast radius of that fill is zero today and ~100% tomorrow,
which is an ordering constraint rather than a reassurance.**
`db/repositories/search.py:180` pins the embedded population to
`t.enrichment_state <> 'skeleton'`, and on this catalog that population is
**0 of 1,271,138** — a pure bootstrap has no enriched title, so filling
`credit_names` first invalidates no embedding at all. Run it *after* a priority
tier lands and it invalidates nearly all of one: of the **204,335 titles with
≥100 votes** (161,519 of them movies — the tier group S is sized on),
**203,969 (99.82%)** would gain a `credit_names`, and 161,486 of the 161,519
movies (99.98%). **Backfill `credit_names` before the TMDb crawl, not after.**

**No timing figure was taken.** Every number above is a count or a byte size,
and neither moves with host load — which is the whole reason this measurement
was allowed to run while a dozen sibling suites and testcontainers had the box.
Parse rates, load durations and `EXPLAIN ANALYZE` times were deliberately not
recorded, because under that contention they would measure the host. If a
timing baseline for these three files is ever wanted it needs a quiet box and
a separate run.

**A TMDb `credits.cast[]`, `credits.crew[]` or `created_by[]` entry carries no
IMDb `nconst`, so people cannot be merged across TMDb and IMDb without a
second request each.** Read, not inferred, from four places that agree: the
recorded payloads (`movie.json`'s `credits.cast[]` is `adult, cast_id,
character, credit_id, gender, id, known_for_department, name, order,
original_name, popularity, profile_path`; `credits.crew[]` swaps
`character`/`order`/`cast_id` for `department`/`job`; `series.json`'s
`created_by[]` is `credit_id, gender, id, name, original_name, profile_path` —
no `imdb_id` and no `nm`-shaped value anywhere under any of them);
`usher.adapters.tmdb.mapping._append`, which reads exactly `id`, `name`,
`known_for_department`, `credit_id` and `character` and has nowhere to put one;
`usher/domain/people.py`'s docstring, which records the same three field lists
against the same payloads and states that `imdb_id`, `birth_year`,
`death_year` and `biography` **live on `/person/{id}`, one request per person**;
and `tests/fixtures/tmdb/README.md`'s 2026-08-01 live shape diff over 29 movies
and 30 series, which found **no key in the live response absent from these
files** and named the only six live-only keys (`softcore`, `iso_3166_1`,
`networks`) — `imdb_id` is not among them. `mapping._imdb_id` reads a *title's*
IMDb id from the top level or `external_ids`; there is no person analogue. The
only merge key TMDb and IMDb share for a person is the name, which is not
identity — two directors who share a name are two rows, by ADR-0003.

**Wikidata's crosswalk is seconds, not an hour.** The three property joins
measured 14.5 s / 2.1 s / 1.1 s unchunked. WDQS's timeout surfaces as
`HTTP 504 text/plain "upstream request timeout"` after ~65 s with no
`Retry-After`. A live end-to-end run stored 336,200 pairs.
**Suspending `ix_titles_sort_name`/`ix_titles_name_lower_year` during Phase 0
is a real, if modest, win — kept, not emptied.** Measured 2026-07-30 against
the live `title.basics.tsv.gz` (1,271,138 retained titles): 35.8 s suspended
vs 40.2 s kept (11.0% faster), and the rebuilt pair is ~24% smaller (97 MB
vs 127 MB) than building them incrementally across the same load. Only
applies to a first bootstrap (`bulk_load_window` declines on a non-empty
`titles`), so the saving costs nothing when it doesn't apply. See PRD 04's
Phase 0 section for the full numbers.
**`PostgresImportRunRepository.save()` must roll back on a caught
`IntegrityError`, not just translate it.** Without the rollback, Postgres
leaves the *session* — not just the failed call — with an aborted
transaction, so the very next statement on it raises `sqlalchemy.exc.
PendingRollbackError` instead of running. `BootstrapService.import_dataset`'s
except handler is exactly such a next statement, so the missing rollback
broke its documented "does not re-raise" contract for real, verified against
real Postgres with two engine-bound sessions racing to bootstrap the same
dataset (`tests/integration/test_import_run_repository.py`). Deliberately a
full `session.rollback()`, not a `PostgresTitleRepository`-style SAVEPOINT —
see `usher/db/repositories/import_run.py`'s module docstring for why this
repository's one caller never has independent pending work on the session
worth a SAVEPOINT protecting.
**Fixing that session-poisoning bug surfaced a second one, one layer up, in
`BootstrapService.import_dataset` itself: the loser's failure handler
overwrote the winner's checkpoint.** Once `self._runs.get(dataset.name)`
after a caught `RepositoryConflict` stopped raising and started actually
returning a row, it returns the *other*, winning process's row — the loser
never got one of its own (`start()` never returned it one). The except
handler used to re-fetch by dataset name unconditionally and evolve+save
`FAILED` onto whatever it found, which is correct when that row is the
caller's own (a `_drain` failure, after `start()` succeeded) but silently
corrupts a legitimately `RUNNING` or already-`COMPLETED` import when it
belongs to someone else (a `start()` conflict) — worse than the crash it
replaced, because the crash was loud and this would not have been: a
subsequent resume reads exactly that corrupted record. `RepositoryConflict`
can only ever reach `import_dataset` from `start()` itself — once any row
exists for a dataset, every later `start()`/`save()` call updates that same
row rather than competing for a new one, so `_drain`'s own `save()` calls
(which always update the id `start()` already returned) cannot trigger it.
That made the fix a clean split: a `RepositoryConflict` from `start()`
specifically now goes to `_concede_to_other_owner`, which touches nothing
(no `save`, no `commit`) and returns the current owner's row exactly as
stored; every other `UsherPortError` path is unchanged. Verified against
real Postgres with a forced two-session race
(`tests/integration/test_bootstrap_concurrency.py`) — reproduced the
overwrite on the pre-fix code first (the winner's row read back `FAILED`
with the loser's unrelated conflict message), then confirmed the fix
leaves it untouched. The unit-level fakes needed a matching fix to even be
capable of catching this: the original conflict test double raised
`RepositoryConflict` with no competing row present at all, so asserting
only "the caller didn't crash" passed both before and after either bug —
it needs a real winner row seeded first, and an assertion that it comes
back byte-for-byte unchanged.

Verified working as of M2's final group (end-to-end integration, the index
measurement, and documentation) — the bulk-dataset bootstrap pipeline is
runnable for real, not just under test:

```bash
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run python -m usher bootstrap --phase all       # import IMDb + TMDb ids + crosswalk
uv run python -m usher bootstrap --phase imdb      # one phase at a time
uv run python -m usher bootstrap-status            # progress and catalog size
uv run python scripts/measure_bulk_load.py         # NOT a test -- downloads the real dump
```

Verified directly against a scratch `pgvector/pgvector:pg17`, 2026-07-30,
downloading the real IMDb/TMDb dumps and querying live Wikidata — nothing
mocked. `bootstrap --phase imdb` killed mid-run at 700,000/1,271,138 titles
committed; re-run logged `resuming imdb.title.basics from position 6033908
(700000 rows already seen)` and finished at the identical 1,271,138 titles
an uninterrupted run reaches. A full `bootstrap --phase all` then ran end to
end: 1,271,138 titles (899,828 movies / 371,310 series), 538,937 with a
community rating, 291,737 linked to a `tmdb_id` (236,712 movies / 55,025
series, zero `(tmdb_id, kind)` duplicates — ADR-0011 holds under real data),
50,793 linked to a `tvdb_id`. Two known titles spot-checked correct end to
end: `tt0111161` (The Shawshank Redemption) landed with `tmdb_id=278`,
`community_rating=9.3`; `tt0944947` (Game of Thrones) landed with
`tmdb_id=1399`, `tvdb_id=121361`, `community_rating=9.2`. `bootstrap-status`'s
final report:

```text
titles in catalog: 1271138
wikidata.crosswalk       completed  position=30 seen=386364 written=385805
tmdb.ids.series          completed  position=228100 seen=228100 written=228100
tmdb.ids.movie           completed  position=1226544 seen=1226544 written=1226544
imdb.title.ratings       completed  position=1700616 seen=1700615 written=538937
imdb.title.basics        completed  position=12678891 seen=1271138 written=1271138
```

**A live end-to-end run needs a real catalog, and building one costs three
minutes and no API key.** `bootstrap --phase all` pulls IMDb's
`title.basics`/`title.ratings` dumps, TMDb's *public daily id export files*
(not the API — no key), and Wikidata's public SPARQL endpoint. Re-run
2026-07-31 against a scratch `pgvector/pgvector:pg17`: **1,271,314 titles,
291,772 with a `tmdb_id`, 539,006 with a community rating**, in 2 min 59 s
wall clock end to end. That is the catalog M4's match ladder has to be
measured against — an empty one sends everything to tier 5 and measures
nothing.
