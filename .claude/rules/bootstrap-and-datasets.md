---
paths:
  - "src/usher/adapters/bulk/**"
  - "src/usher/services/bootstrap.py"
  - "src/usher/db/repositories/bulk.py"
  - "src/usher/domain/people.py"
  - "src/usher/domain/bootstrap.py"
  - "scripts/measure_bulk_load.py"
  - "scripts/measure_imdb_people.py"
  - "scripts/measure_people_provenance.py"
---

# IMDb, TMDb id exports, Wikidata and MovieLens

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

## The commands these findings are about

```bash
uv run usher bootstrap --phase all      # the six steps, in FULL_SEQUENCE's order
uv run usher bootstrap --phase imdb     # or credit-names | aliases | tmdb-ids | crosswalk | movielens
uv run usher bootstrap --phase ratings  # an alias, not a step — title.ratings.tsv.gz (8.2 MiB) alone
uv run usher bootstrap-status           # titles, genome vectors, vocabulary, per-dataset checkpoints
```

Every step is **resumable** and a resume finishes at the identical row count —
verified by `SIGKILL` for `imdb`, `aliases` and `credit-names` below, all three
against real Postgres and the whole dumps. `POST /admin/bootstrap/{phase}` and
`argparse`'s `choices=` are the same enum, so a phase cannot exist on one
boundary and not the other. **`--phase credit-names` must run before the TMDb
enrichment crawl** and `--help` says so; the measurement is under
"Filling `titles.credit_names`".

A ratings rebuild deletes the checkpoint first, because `--phase ratings` writes
`--phase imdb`'s own `import_runs` row and a completed run at an unchanged
upstream revision resumes at EOF and writes nothing:

```sql
DELETE FROM import_runs WHERE dataset = 'imdb.title.ratings';  -- verify 0 remain
```

...then assert on **`rows_written`**, never on the exit status. Full argument
under `--phase ratings` below.

The measurement scripts are not tests — each downloads real dumps or opens real
sockets, and each is phased so a figure has a trail:

```bash
uv run python scripts/measure_bulk_load.py                        # downloads the real dump
uv run python scripts/measure_imdb_people.py --phase head         # M9 T3; 1.49 GiB
#   then counts | relations | names | titles | blast | drop
uv run python scripts/measure_people_provenance.py --phase head   # ADR-0036; ~700 MiB
#   then extract | keys | load | dedup | overlap | blast | latency | apply | seed | drop
```

**`--phase` is required on both, and `measure_people_provenance.py` re-hashes
its pre-registered bar (`/var/tmp/t4r/BAR.md`) at the start of every phase and
refuses to run if it has moved.** `/var/tmp`, never `/tmp` — tmpfs erases the
proof that a bar predates its numbers.

## MovieLens — the archive, the join and the genome

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
**`genome-tags.csv` is 1,128 rows and every one is usable, measured 2026-08-07
through the shipped `CachedDatasetFile.member_lines`.** `tagId` is exactly
`1...1128` and ascending; every name is non-empty and distinct; the longest is
65 characters; **no name contains a comma**; and the member is
**CRLF**-terminated (8,359 compressed / 18,103 uncompressed bytes). Two of those
decide the parser:

- **The CRLF is invisible only because `member_lines` decodes through
  `io.TextIOWrapper` in universal-newline mode** — verified by reading, not
  assumed, since a stray `\r` would land in every stored tag name. That
  property was asserted in *three* source files and covered by nothing: every
  fixture in `tests/unit/test_adapters_bulk_movielens.py` is built with
  `"\n".join(...)`, so planting `newline=""` passed all 2,882 unit cases while
  putting a trailing `\r` on all 1,128 stored names.
  `test_a_crlf_bodied_member_stores_no_carriage_return_in_a_tag_name` is the
  one case in the suite that fails against that plant.
- **Comma-free names are a property of the snapshot, not a promise**, which is
  why the parse is `partition(",")` — only the first comma separates — and
  `partition` rather than `split(",", 1)` because a row with *no* comma has to
  be distinguishable, and `split` hands that back as a one-element list whose
  `[0]` parses as a perfectly good `tagId`. The shipped reader (now
  `MovieLensDataset.tag_vocabulary` / `_vocabulary`,
  `adapters/bulk/movielens.py:273,305`) read exactly that field and was blind to
  it — harmless while names were thrown away, not since `m08b` stores them.

The empty-name refusal is `not name.strip()`, not `not name`, because
`ck_genome_tags_tag_not_empty` is `tag <> ''` and accepts `'   '`. Unreachable
in the measured file (all 1,128 names are `strip()`-stable), which is why the
CHECK was left alone rather than re-spelled `btrim(tag) <> ''` in a migration
for a value nothing can produce.

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
## IMDb — the cache key, the parser, and what the dumps really contain

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
  — *today*. `BulkCatalogRepository.replace_aliases` refuses an over-long name
  for the **whole call**, so the parser drops them rather than letting one row
  take a ten-thousand-row batch with it. *(This sentence named a
  `SearchNameRepository` until T7 landed the writer; no port of that name has
  ever existed here. Measured on a real database once the writer did exist: the
  refusal is a `RepositoryConflict` naming
  `ck_title_search_names_name_within_btree_bound`, and it takes the whole call
  including its DELETE — so a batch carrying one long alias leaves every title
  in scope with the aliases it already had rather than with none.)*
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
belongs in the parser: it costs 7 aliases.** Joined to a 1,272,367-title catalog
built from this host's cached `title.basics` (`"128751cb2f3132bd73bdf08c7f4def5d
-27"` — **a different upstream snapshot**, the same not-one-snapshot hazard
recorded below, which is why these differ from T3's by ~0.07%): of 7,541,357
in-catalog akas rows, 1,272,135 are flagged and **1,272,111 (99.998%)
casefold-equal the title's own `name` or `original_name`**. Only **24** disagree,
17 of those repeating a name a non-flagged row already carries — so after
dedup the alias total goes 1,663,330 → 1,663,323, a loss of **7**. The hazard
that would have made the filter unsafe is empirically zero: **0 catalog titles
have no `originalTitle`**, so no title's flagged aka is its only carrier.
**The filter is a cheap prefix of the writer's rule and never a substitute** —
of the 6,269,222 retained rows that survive it, **4,426,783 (70.6%) still
casefold-equal the title's own name**, and only a comparison against the stored
`Title` can see that.

**The plan's "heavily-filtered file yields row-less batches" risk is inverted by
the measurement.** `title.akas` keeps **78.4%** of its lines, making it the
*least* filtered dataset `_ImdbDataset` has ever streamed — `title.basics` keeps
**10.0%** and has shipped that way since M2. So `_ImdbDataset` was not changed
to emit cursor-advancing empty batches: a trailing run of filtered lines costs a
re-read on resume and never a lost row, because `position` counts lines consumed
and every downstream write is an upsert.

**The alias writer compares with SQL `lower()` while T3/T5 measured with Python
`casefold()`, and the three candidate foldings genuinely disagree on real IMDb
names — 2026-08-11 (M9 T7).** `BulkCatalogRepository.replace_aliases` drops an
alias equal to the title's own `name` or `original_name` **under `lower()`** and
deduplicates on `(title_id, lower(name))`; `scripts/measure_imdb_people.py:353`
folds with `str.casefold()`:

| pair | Postgres `lower()` | Python `str.lower()` | Python `str.casefold()` |
|---|---|---|---|
| `ΟΔΟΣ` / `Οδος` (Greek final sigma) | **not equal** | equal | equal |
| `STRASSE` / `Straße` | not equal | not equal | **equal** |

Streamed through the shipped `parse_akas_row` over the whole pinned
`title.akas.tsv.gz` (`"19810e3eb2b0f1fa774bf4e4af94d7c6-61"`, md5
`b2ae74057227953a917e6d26f7a841d0`, 510,168,971 B on disk = the pin's own
`Content-Length`, 58,906,369 lines, **46,202,631 retained rows**):
**32,223 rows (0.070%) have `str.lower() != str.casefold()`**, in exactly two
families — German `ß` (`Das große Finale`, 21 rows) and Greek final sigma
(`Οθέλλος`, 25). `casefold()` folds strictly *more* pairs together, so T7 could
only bound the shipped rule: **at least 1,663,364, at most that plus 32,223**,
under 1.70M against an 8,000,000-row ceiling. ✅ **T8's end-to-end run settled it
to 1,663,455 — +132, a gap of 0.008% rather than the 1.9% the bound allowed**
(below). T7 itself wrote no parser and read no dataset, and said so rather than
implying a run it had not taken.

**Postgres is authoritative here by construction, which is why the divergence
is recorded and not repaired.** The test for keeping an alias is whether it
reaches anything `ix_titles_name_lower_prefix` does not already answer, and that
index is a btree over the *database's* `lower(name)` — so under the rule that
matters, `Οδος` really is a distinct entry and the row really does add
reachability. A `casefold()` comparison would drop it and lose recall for a
claim about an index it does not describe. **The consequence is a fake/Postgres
divergence** (Python's `str.lower()` applies the contextual final-sigma rule and
the database does not), enumerated in `tests/fakes/bulk_catalog_repository.py`'s
own list and pinned by an integration-only case rather than by the shared
contract.

✅ **`replace_aliases` requires whole titles per call, and `IMDbAkaDataset`
groups by title — `group_of` returns `row.imdb_id`
(`adapters/bulk/imdb.py:824`), shipped by T8.** This entry read *"does not
group by title … T8 has to close that gap"* while T7 was the current state;
the gap is closed and the fix was priced both ways under "The batch-boundary
bug T7 handed to T8 was real" below.

The mechanism is worth keeping because it is invisible from either side of the
port. `_ImdbDataset` batches on a row count, so a title straddling a batch
boundary is delivered in two calls, and the second call's scoped replace
**deletes the first call's rows**: the title keeps whichever aliases landed in
the later batch and silently loses the rest. Nothing reports it — every row is
in scope in both calls, so the port's `ValueError` guard cannot fire, and the
phase's report sums the calls. `IMDbCreditNamesDataset` solves the identical
problem in the identical file, by closing a title's run before it closes a
batch and carrying a `boundary` line number as the cursor rather than
`position`. **Any new caller of a scoped-replace port needs that shape or a
scope-and-rows accumulator of its own.**

## T3 — sizing `people`/`credits` against IMDb (2026-08-11). ⚠️ Its refusal was reversed; read T4R with it

**`title.principals` + `name.basics` will not fit a `people`/`credits` design
for this catalog, and the refusal is a size measurement rather than a row
count — measured 2026-08-11 (M9 T3).** The bar was written to
`/tmp/m9-t3/BAR.md` before the first byte was downloaded, transcribed from the
plan: **(A)** people + credits is affordable only if retained credits ≤ 20M
**and** added relation size including indexes ≤ 2.0 GB **and** `people` ≤ 6M;
**(B)** akas only if retained deduplicated aliases ≤ 8M **and** ≤ 1.0 GB;
**(C)** if (A) fails the fallback is the names-only design and *the deliverable
is the recorded refusal, not a shrunken (A)*.

**Every number below comes out of a named phase of
`scripts/measure_imdb_people.py`, and the whole chain was re-run end to end
from the pinned files to make that true.** Row counts and parser-side filters
are `--phase counts`; every relation size is `pg_table_size` /
`pg_total_relation_size` after `VACUUM ANALYZE` from `--phase relations`; the
`titles` growth is `--phase titles`; the `credit_names` shape is
`--phase names` + `--phase blast`. Nothing is arithmetic on another figure
except where the text says so — **and the phases exist because the first
version of this entry could not have been written.** Several byte-exact figures
were `psql` measurements taken at a prompt outside the script; a reviewer
walking the phases found no trail for any of them and blocked. **Byte-exact
precision on an untraceable number, sitting beside traceable ones, is
indistinguishable from a number somebody computed in their head.** The fix was
to move the measurement into the script, not to soften the prose.

**(A) failed on its byte clause and (B) passed.** Two of (A)'s three clauses
passed comfortably — **12,626,452 retained credits** of a 20M ceiling and
**3,211,941 people** of a 6M one — and the third failed on both readings of its
unit: **2,701,697,024 B, i.e. 2.702 GB or 2.516 GiB, against a 2.0 GB bar**.
Trimming `credits` to the five columns a credit cannot do without saves
306,282,496 B and still lands 395 MB over, so *no version of (A) fit by
trimming* and (C) was written first.

⚠️ **That refusal was reversed on 2026-08-12: the 2.0 GB bar had nothing behind
it. Read "T4R" below before quoting any of it.** Re-measured against a real
ceiling, the whole design is 3.375 GB — 13.5% of it. What (A) still supplies is
the *shape*: those two counts and the 2.702 GB are the sizing a future load
starts from, and T4R's own 3,374,514,176 B is the number to quote.

**The consequence taken at the time, and how much of it stands.** The IMDb half
of M9's Track 2 fell back to the **names-only design**: `titles.credit_names`
filled directly from `title.principals` × `name.basics`, with **no `people` and
no `credits` rows written from IMDb**. ✅ **That half is still what ships** —
there is no IMDb `BulkDataset` writing either table, and `adapters/tmdb/
mapping.py` remains the only writer of a `Credit`. ⚠️ **The rest was reversed.**
T4's provenance rule and the withdrawn `m09b` grant came back as **`m09d`**
(`credits.source`, `people.imdb_id`, `ix_credits_source_natural_key`) and
`CREDIT_SOURCE_PRECEDENCE` in `domain/people.py`, so the schema and the
arbitration vocabulary exist while nothing fills them: **both `m09d` indexes
are empty at that revision**. T5 keeping only its `title.akas` parser, and T7/T8
re-scoping to aliases and `credit_names`, are what shipped.

*(That is why the sentence above about `credits`' unique key is dated. Until
`m09d` its only unique key was `tmdb_credit_id`, NULL on every IMDb row, and the
obvious `(title_id, person_id, kind)` **cannot be UNIQUE** — 1,341,798 retained
credits collide on it. The key that landed instead is `(title_id, source,
billing_order)`, partial on `source <> 'tmdb'` with `NULLS NOT DISTINCT`;
`db/models/people.py:271-278` carries the whole argument and T4R measured the
candidates below.)*

**(B) passes both clauses — 4.8× under on rows, 3.2× under on bytes — because
three akas in four are not aliases at all.** Of 7,536,366 retained rows,
**5,693,570 (75.5%) casefold-equal the title's own `name` or `original_name`**
and carry nothing a `titles` prefix index does not already hold; 1,842,796
survive, and dedup on `(title_id, casefold(name))` leaves **1,663,364** against
an 8M ceiling, in **307,822,592 B** at `m09a`'s `title_search_names` shape
against a 1.0 GB one. **Only 399,046 of 1,271,138 titles (31.4%) gain even one
alias** — a narrow, cheap win rather than a broad one. Zero retained rows are
empty and **zero exceed `SEARCH_NAME_MAX_CHARS`** (512), so `m09a`'s
btree-bound CHECK rejects nothing in this snapshot. *(T8 then ran it for real:
1,663,455 stored, 313 MB.)*

⚠️ **1,653,088 survivors carry a `region` and the matching `language` count is
deliberately not quoted.** The dedupe is `DISTINCT ON (title_id, folded) ...
ORDER BY title_id, folded, region NULLS LAST`, so among rows tying on region the
survivor is arbitrary and its `language` is whichever row won — two runs over
the identical pinned file gave 410,634 and 410,596, **a 38-row wobble that is a
property of the measurement, not of the data**. `region` is stable because it is
an `ORDER BY` key; a loader that needs a stable `language` has to add one.

**Denominators for everything above**, all against the same 1,271,138-title
catalog: 12,626,452 of 101,151,422 principals rows retained (12.5%);
**1,192,241 titles (93.8%) have ≥1 principal**; 3,212,911 distinct `nconst`
referenced, of which 3,211,941 carry a `primaryName` — **one referenced person
in the whole file has none**. That is why 12,626,452 retained principals store
only **12,625,259** credits: the 1,193-row difference is credits naming one of
the 970 unusable `nconst` (969 absent + 1 nameless), because a credit whose
person cannot be stored is dropped rather than orphaned. 7,536,366 of 58,906,368
akas rows retained (12.8%), over **1,270,074 titles (99.92%)** before the
canonical-name filter. *(T4R re-took all of these against a newer pin and a
1,272,367-title catalog; see its own Denominators.)*

**The snapshot was pinned and the pin is the finding's date.**
`title.principals` `"08ce60665889cb40c7371e1eab44a1f2-93"`, `name.basics`
`"a3b9681921c92e5917182d1ecc05bd2d-37"`, `title.akas`
`"19810e3eb2b0f1fa774bf4e4af94d7c6-61"` — written by `--phase head` to
`/tmp/m9-t3/pin.json`; every later phase passes the pinned value to
`ensure_local` and aborts if upstream serves a different one. Two truncation
checks, run at a shell rather than in the script: each file's on-disk length
equals the `Content-Length` its own `HEAD` reported, and `gzip -t` exits clean
on all three, which validates the trailing CRC32 and ISIZE of the whole stream.

⚠️ **IMDb does not regenerate the seven files together, and every join across
them has to assume it.** The pin made it visible: five carried `Last-Modified:
Tue, 11 Aug 2026 00:47–00:48 GMT` and `name.basics`/`title.akas` carried
`Mon, 10 Aug 2026 12:53 GMT`. The consequence is measurable — **969 `nconst`
referenced by that day's `title.principals` do not exist in that day's
`name.basics`** — so **an importer joining across these files must treat a
dangling `nconst` as routine and drop the credit, not raise**. It is also why
`IMDbCreditNamesDataset` checkpoints a *composite* revision
(`name.basics=<etag>;title.principals=<etag>`): a single-file revision would
call two genuinely different pairs one snapshot and replay a stored cursor
across them. The count moves with the pin every time it is re-measured — 969,
then 3,734, then 995 — which is the finding, not noise.

**Filling `titles.credit_names` costs four times more in bytes than the names
themselves weigh — measured on a real 1,271,138-row copy of `titles` at the
`m09a` schema, not estimated.** **1,192,217 of 1,271,138 titles (93.8%) gain a
non-empty `credit_names`**, mean **9.11** names each, 10,862,893 names and
**158,479,368 B of text**. `titles`' total relation size goes **873,177,088 B →
1,496,850,432 B after `VACUUM FULL`: +623,673,344 B, +71.4%**, i.e. 3.9 bytes
stored for every byte of name. Decomposed, and the third term is a confound
worth stating rather than hiding: heap + toast **+558,948,352 B (+126.7%)**;
`ix_titles_search_document` **4.54×, 40,304,640 → 183,017,472 B
(+142,712,832)**, which is the class-B lexemes; and the remaining nine indexes
**net −77,987,840 B**, because `VACUUM FULL` rebuilds every index by sort while
the baseline's btrees were built incrementally by `COPY`. So the honest
statement is that the fill costs ~624 MB *net of* an index-rebuild saving that
an operator only collects if they actually run the vacuum.
The *transient* figure is the one an operator's disk sees, and it is more than
double the settled one: before any vacuum the same table is **2,240,831,488 B,
+1,367,654,400 B over baseline** (GIN alone at 207,970,304), because a single
`UPDATE` of 1.19M rows leaves a dead tuple for every live one. **That peak, not
the settled 624 MB, is the number to budget against PRD 08's 8–12 GB.**
*(`--phase titles`' numbers, from the re-run that gave them a trail. The first
pass measured the same quantities through a `psql` pipe and differed in the
third significant figure — a 0.05% page-level wobble at baseline — while
`+624 MB`, `×4.54` and every row count are identical to both. **No timing figure
was taken**: every number here is a count or a byte size, neither of which moves
with host load, which is why this measurement was allowed to run while a dozen
sibling suites had the box.)*

**The embedding blast radius of that fill is zero in every ordering, and the
ordering constraint is about coverage instead.** `db/repositories/search.py:180`
pins the embedded population to `t.enrichment_state <> 'skeleton'`, and
`fill_credit_names` writes only where that expression is *false* — the two sets
are complements, so the fill cannot invalidate a vector whenever it is run.
**That is a precedence rule, not an optimisation**: `CreditRepository.
replace_for_titles` owns the column for enriched titles, and a
`credit_names = '{}'` guard would overwrite TMDb's own answer for a title it
derived no cast for.

What the ordering decides is whether the names arrive at all: of the **204,335
titles with ≥100 votes** (161,519 of them movies — what tier group S is sized
on), **203,969 (99.82%)** would gain a `credit_names`, and 161,486 of the
161,519 movies (99.98%) — *while they are still skeletons*. After a
priority-tier crawl those titles are deferred to TMDb permanently, on that run
and every later one, and re-running the phase does not repair it.
**Backfill `credit_names` before the TMDb crawl, not after.**

*Corrected 2026-08-12: this paragraph read "zero today and ~100% tomorrow" and
said the late ordering "invalidates nearly all of" the tier, which the
`AND m.ours` predicate refuses outright. The counts and the recommendation
stand; the mechanism was wrong, and it had propagated to five other statements
including two an operator reads.*

**`name.basics` and `title.principals` at the parser level, measured over the
whole pinned files rather than over the catalog-retained slice — 2026-08-11
(M9 T6).** T3's figures above are all *retained* ones; these are what a parser
sees, since it has no catalog. Headers are exactly `nconst primaryName
birthYear deathYear primaryProfession knownForTitles` (6 columns,
15,563,615 data rows, `"a3b9681921c92e5917182d1ecc05bd2d-37"`) and `tconst
ordering nconst category job characters` (6 columns, 101,151,422 data rows,
`"08ce60665889cb40c7371e1eab44a1f2-93"`), and **zero rows in either split to
any other count**.

- **89 rows of `name.basics` carry no `primaryName`.** The longest name is
  **105** characters and **none exceeds 512**, so a length filter mirroring
  `title.akas`' would reject nothing — which is why the credit-names parser
  has none: `titles.credit_names` is an unbounded `text[]` with no CHECK to
  mirror, and a bound would be a number the module invented.
- **138 names contain a literal `"` and 7 open with one** — the csv trap
  confirmed on a fourth file, and the seven are people whose names would be
  rewritten on every title they are credited on.
- **`name.basics` is sorted lexicographically by the `nconst` string, not
  numerically: 738,680 descents in the integer sequence.** An eight-digit id
  sharing a seven-digit id's first seven characters sorts before it. **This is
  a correctness trap for the obvious in-memory index** — a sorted array plus
  `bisect` answers `None` for millions of real people, and each miss is a
  title quietly losing a name. Same family as the migration-id padding trap in
  `db-and-sql.md`.
- **`ordering` is present and integral on every principals row**, min 1, max
  75, and **ascends within every one of the 11,491,032 titles**. So a sort on
  it is unobservable against production data and only a deliberately
  disordered fixture can pin it.
- **13 categories**, `actor` 23,895,326 down to `archive_sound` 13,782. None
  is filtered: IMDb has already applied its own editorial selection at a
  **mean of 8.8 rows per title**, and the two that read like noise
  (`archive_footage`, `archive_sound`) are 0.65% of the file.
- **9,404,442 rows repeat a person already credited on the same title** — a
  director who also wrote it. Deduplication is not defensive; it is 9.3% of
  the file.
- **3,734 distinct `nconst` over 7,701 rows dangle**, i.e. are named by
  `title.principals` and absent from `name.basics`. T3 measured 969 against a
  different pin over the retained slice; the number moves with the pin because
  **the seven dumps are not one snapshot**, which is the point. **156 titles
  have every principal dangling** and must yield no record at all — an empty
  name list would *blank* a `credit_names` another source filled.

**The whole `nconst -> primaryName` map fits in 345 MiB and 19.5 s, and that
is the price of doing this join with no `people` table.** Measured on the
pinned file: 211,630,156 B of name text + 87,819,488 B of address table +
62,254,108 B of offsets = **361,703,752 B**, against a peak RSS of **361.3
MB**; the `title.principals` pass over it is a further **157 s**. Structure is
one `bytearray` of names end to end, an `array("i")` of offsets, and a
direct-address table keyed on the integer inside the `nconst` — chunked at
65,536 entries. **The chunking is not a micro-optimisation:** a single flat
array is sized by the largest id, and this project's own reserved synthetic
band (`nm99\d{6}`) starts at 99,000,000, so a *two-person test index*
allocated **396 MB** and the parser test file took **28.9 s**. Chunked, the
same file takes **0.27 s**. A fixture id nine orders of magnitude from the
real id space is a realistic input here precisely because the licensing guard
mandates one.

**A TMDb `credits.cast[]`, `credits.crew[]` or `created_by[]` entry carries no
IMDb `nconst`, so people cannot be merged across TMDb and IMDb without a second
request each.** Read, not inferred, from four sources that agree: the recorded
payloads (no `imdb_id` and no `nm`-shaped value under any of the three arrays);
`adapters/tmdb/mapping._append`, which reads exactly `id`, `name`,
`known_for_department`, `credit_id` and `character` and has nowhere to put one;
`domain/people.py`'s docstring, which records the same field lists and states
that `imdb_id`, `birth_year`, `death_year` and `biography` **live on
`/person/{id}`, one request per person**; and `tests/fixtures/tmdb/README.md`'s
2026-08-01 live shape diff over 29 movies and 30 series, which found no live key
absent from the fixtures and named the only six live-only ones — `imdb_id` is
not among them. `mapping._imdb_id` reads a *title's* IMDb id from the top level
or `external_ids`; there is no person analogue. The only merge key the two
sources share for a person is the name, which is not identity — two directors
who share a name are two rows, by ADR-0003. **Both merge directions were later
priced live and both have a low yield**; see T4R's second pass.

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
`BootstrapService.import_dataset`: the loser's failure handler overwrote the
winner's checkpoint.** Once `self._runs.get(dataset.name)` after a caught
`RepositoryConflict` stopped raising and started returning a row, it returns the
*other*, winning process's row — the loser never got one of its own. The except
handler re-fetched by dataset name unconditionally and evolve+saved `FAILED`
onto whatever it found, which is right when that row is the caller's own (a
`_drain` failure, after `start()` succeeded) and **silently corrupts a
legitimately `RUNNING` or `COMPLETED` import when it belongs to someone else**
(a `start()` conflict) — worse than the crash it replaced, because the crash was
loud and a later resume reads exactly that corrupted record.

The fix is a clean split because `RepositoryConflict` can only reach
`import_dataset` from `start()`: once any row exists for a dataset, every later
`start()`/`save()` updates that same row rather than competing for a new one, so
`_drain`'s own saves cannot trigger it. A conflict from `start()` now goes to
`_concede_to_other_owner`, which touches nothing (no `save`, no `commit`) and
returns the owner's row as stored; every other `UsherPortError` path is
unchanged. Verified against real Postgres with a forced two-session race
(`tests/integration/test_bootstrap_concurrency.py`) — the overwrite reproduced
on the pre-fix code first, the winner's row reading back `FAILED` with the
loser's unrelated conflict message.

⚠️ **The unit fakes could not have caught either bug.** The original conflict
double raised `RepositoryConflict` with **no competing row present at all**, so
asserting "the caller didn't crash" passed before and after both. It needs a
real winner row seeded first and an assertion that it comes back byte-for-byte
unchanged.

## Runs against real Postgres and the real dumps

**The pipeline is runnable for real, verified against a scratch
`pgvector/pgvector:pg17` on 2026-07-30 — real IMDb/TMDb dumps, live Wikidata,
nothing mocked (M2's final group).** `bootstrap --phase imdb` killed mid-run at
700,000/1,271,138 titles committed; the re-run logged `resuming
imdb.title.basics from position 6033908 (700000 rows already seen)` and finished
at the identical 1,271,138 titles an uninterrupted run reaches. A full
`--phase all` then ran end to end: 1,271,138 titles (899,828 movies / 371,310
series), 538,937 rated, 291,737 linked to a `tmdb_id` (236,712 movies / 55,025
series, zero `(tmdb_id, kind)` duplicates — ADR-0011 holds under real data),
50,793 linked to a `tvdb_id`. Two known titles spot-checked end to end:
`tt0111161` (The Shawshank Redemption) landed with `tmdb_id=278` and an IMDb
rating of **9.3**; `tt0944947` (Game of Thrones) with `tmdb_id=1399`,
`tvdb_id=121361`, **9.2**.

⚠️ **That spot-check named the column `community_rating`, and there is no such
column now.** `m10a` (ADR-0040, 2026-08-19) split three dual-written columns
into five that each name their source, and `apply_ratings`
(`db/repositories/bulk.py:622`) writes **`imdb_average_rating` and
`imdb_num_votes`** — `community_rating` became `tmdb_vote_average`, which this
phase does not write at all. Re-run the spot-check against the IMDb pair. The
old name survives on the wire only, through
`domain/title.py`'s `WIRE_FIELD_NAMES`, so a HTTP response saying
`community_rating` and a `SELECT` saying `imdb_average_rating` are consistent
rather than contradictory. Every "with a community rating" count in this file
is a count of `imdb_average_rating`.

`bootstrap-status`' final report on that run — **and note the shape has since
gained two lines**, `genome vectors:` and `genome vocabulary:`, which `m08b` and
`--phase movielens` added:

```text
titles in catalog: 1271138
wikidata.crosswalk       completed  position=30 seen=386364 written=385805
tmdb.ids.series          completed  position=228100 seen=228100 written=228100
tmdb.ids.movie           completed  position=1226544 seen=1226544 written=1226544
imdb.title.ratings       completed  position=1700616 seen=1700615 written=538937
imdb.title.basics        completed  position=12678891 seen=1271138 written=1271138
```

**A live end-to-end run needs a real catalog, and building one costs three
minutes and no API key.** `--phase all` pulls IMDb's
`title.basics`/`title.ratings` dumps, TMDb's *public daily id export files*
(not the API — no key), and Wikidata's public SPARQL endpoint. Re-run
2026-07-31 against a scratch `pgvector/pgvector:pg17`: **1,271,314 titles,
291,772 with a `tmdb_id`, 539,006 rated**, in 2 min 59 s wall clock. That is the
catalog M4's match ladder has to be measured against — an empty one sends
everything to tier 5 and measures nothing.

### T8 — `credit-names` and `aliases`, whole pinned dumps, 22 min 26 s (2026-08-11)

**The two IMDb expansion phases were run end to end against real Postgres and
the whole pinned dumps — 2026-08-11 (M9 T8), 22 min 26 s of wall clock for
1,194,047 titles given a cast and 1,663,455 aliases stored.** Not a
measurement script: `usher.cli._bootstrap` itself, driven with
`CachedDatasetFile.revision` patched to answer the sidecar the cache already
holds, so the run reads T3's *pinned* snapshots (`title.akas`
`"19810e3eb2b0f1fa774bf4e4af94d7c6-61"`, `name.basics`
`"a3b9681921c92e5917182d1ecc05bd2d-37"`, `title.principals`
`"08ce60665889cb40c7371e1eab44a1f2-93"`) rather than whatever IMDb
regenerated that morning, and transfers no byte. The catalog under it is a
real `--phase imdb` over the cached `title.basics`
`"128751cb2f3132bd73bdf08c7f4def5d-27"` — **1,272,367 titles** in 114.9 s,
which is T5's join catalog exactly, so every prediction below is comparable
against its own measurement rather than against one taken on a different
snapshot.

| phase | wall clock | rows read | result | predicted |
|---|---|---|---|---|
| `imdb` | 114.9 s | 12,707,541 lines | 1,272,367 titles, 539,780 rated | — |
| `credit-names` | 826.1 s | 101,151,423 lines → 11,490,876 titles | **1,194,047 titles filled (93.84%)** | 93.8% (T3) |
| `aliases` | 520.1 s | 58,906,369 lines → 46,202,631 retained | **1,663,455 aliases over 399,018 titles**, 313 MB | 1,663,323 (T5), ≤1.70M (T7) |

*Wall clocks are from a shared box — a dozen sibling suites and testcontainers
had it — so they are upper bounds on a quiet machine and are not comparable
with each other at better than ±30%. Every count is exact.*

**Three predictions landed, and the third settles T7's open bound to a
number.** `credit-names`' 1,194,047 filled against T3's projected 1,192,217
is the same figure on a catalog 1,229 titles larger. `aliases` read exactly
**46,202,631** retained rows, which is T7's parser-level count to the row.
And the shipped SQL-`lower()` rule stored **1,663,455** where T5's
`casefold()` join over this same catalog predicted **1,663,323** — **+132**,
against T7's bound of "at least 1,663,323 and at most that plus 32,223". So
the direction T7 argued is confirmed and the real gap is **0.008%** of the
population rather than the 1.9% the bound allowed. The canonical filter
dropped 4,426,750 rows under `lower()` against T5's 4,426,783 under
`casefold()` — 33 fewer, the same direction — and 179,017 were dropped as
duplicates.

**The batch-boundary bug T7 handed to T8 was real, and running it both ways
priced it: 319 stored aliases, silently, on 46,202,631 rows.** With
`IMDbAkaDataset.group_of` returning `None` — which is exactly the shipped
`_ImdbDataset` row-count batching T5 left in place — the same run against the
same catalog reports **1,663,458 written** and leaves **1,663,139** rows in
`title_search_names`. The 319-row gap is titles delivered in two batches whose
second call's scoped `DELETE` took the first call's rows, and **21 titles end
with no alias at all**. Nothing anywhere reports it: each call really did
write what it claimed, the port's `ValueError` guard is about rows outside the
scope and both halves are inside their own, and the phase's own report sums
the calls. With the fix, *written* and *stored* are the same number to the row
— **1,663,455 = 1,663,455** — which is the invariant to check if this is ever
touched again. The parser-level figure, measured separately over the whole
file, is that **all 924** of a 50,000-row-batched import's boundaries land
inside a title and **3,867 retained rows** cross one; 319 of those survive the
canonical filter and the dedupe, which is why the stored loss is two orders of
magnitude smaller than the rows-at-risk count and no less silent.

**Grouping a batch by title is only sound because both dumps are contiguous by
title, and that is measured, not assumed.** Over the whole pinned files:
`title.akas`' `titleId` has **zero lexicographic descents in 58,906,368 rows**
(12,703,713 runs, longest 300 rows, `tt0168366`) and `title.principals`'
`tconst` has **zero in 101,151,422** (11,491,032 runs — T3's title count
exactly — longest 75). Non-decreasing implies contiguous: a run reopening
after some other id would have to descend to do it. The *integer* inside the
id descends 1,250,830 times in `title.akas`, so these files are string-sorted
in the same way `name.basics` is, and any check has to be on the string.
**A guard refusing a dump whose order descends was declined**: it checks a
strictly stronger property than the writer needs, and this repository's own
`tests/fixtures/bulk/title.akas.slice.tsv` is contiguous and *not* sorted
(`tt99000020`, `tt99000030`, `tt99000010`), so the guard would refuse a file
that is fine. Contiguity itself cannot be checked without remembering 12.7M
ids.

**`credit-names` deferred to TMDb exactly 0 times, and that is the expected
reading of a bootstrap-only catalog rather than a broken counter.** Every one
of the 1,272,367 titles is `skeleton`, so `fill_credit_names`' precedence rule
had nothing to defer on; the counter grows only as the crawl advances, which
is what makes the ordering constraint visible in the report. 10,296,829 of the
11,490,876 titles `title.principals` credits are not in this catalog — 89.6%,
the shape T3 predicted, and the number that would look identical to a broken
join if it were not printed.

**Both phases resume at the identical final row count under `SIGKILL`, which is
what the boundary cursor most needed re-checking.** `aliases` killed at position
37,415,494/58,906,369 (63.5%) with 1,386,064 written — and **1,386,064 rows in
the table at that instant**, so the crash left no half-written title; the re-run
finished `position=58906369 seen=46202631 written=1663455` and the table holds
**1,663,455 aliases over 399,018 titles**, the uninterrupted run's numbers to
the row. `credit-names` killed at 48,065,977/101,151,423 (47.5%) with **642,449**
filled — matching the 642,449 titles then holding a non-empty `credit_names` —
and the re-run finished at **1,194,047**, again exact.

**`rows_seen` comes back *equal* to the uninterrupted total rather than larger,
which is the boundary cursor showing its work**: it points at the last line of a
completed title, so a resume replays no retained row at all, where a mid-title
cursor would have replayed some and destroyed the rest.

`credit-names`' resume is the expensive one, and that is the part worth planning
around: the
`nconst -> primaryName` index is rebuilt from the whole of `name.basics`
before the first batch of **every** run, resumed or not — a fixed 19.5 s and
345 MiB — so a resume pays the join's setup again and only the
`title.principals` scan is skipped. `position` is a line offset into
`title.principals` alone and says nothing about `name.basics`, which is
correct precisely because the index is never partial.

## `bulk_load_window()` is entered before the ownership race is known, and a route makes that reachable (2026-08-12, M9 E5)

**A defect in a shipped M2 path, not in the new route. Recorded, not fixed.**
Found by asking E3's question of a bootstrap phase: *what has changed between
the enqueue and the claim, and does the handler still have the right to do the
work?*

`composition.run_bootstrap` (was `cli._bootstrap`) opens
`catalog.bulk_load_window()` **around** the two IMDb `import_dataset` calls, and
`import_dataset` is where a `RepositoryConflict` from
`ImportRunRepository.start()` is discovered. So the window is entered before
anybody knows who owns the dataset, and its guard — `count_titles() == 0` — is a
point-in-time read taken at that same moment. Two processes bootstrapping an
**empty** catalog both see zero, both `DROP INDEX IF EXISTS
ix_titles_sort_name, ix_titles_name_lower_year`, and both commit. The loser
concedes inside `import_dataset` (`_concede_to_other_owner` touches nothing and
**does not raise**), exits its `async with`, and runs `CREATE INDEX IF NOT
EXISTS` for both — **while the winner is still streaming 1.27M rows**.

**The cost is exactly the saving the window exists for, so the suspension
silently buys nothing:** 40.2 s instead of 35.8 s and a rebuilt pair of 127 MB
instead of 97 MB. Second-order, the loser's `CREATE INDEX` takes a `SHARE` lock
on `titles`, blocking the winner's batch writes for the length of the rebuild.

**The window of exposure is the *download*, not the whole run.** Once the winner
commits its first batch (50,000 rows at `USHER_BULK_BATCH_SIZE`'s default)
`count_titles()` is non-zero and a later loser suspends nothing — but that first
commit is behind a `HEAD`, an `ensure_local` and 224 MB, so the window is
minutes wide on a cold cache.

**What changed in M9 is reachability, not the code.** This used to need two
`usher bootstrap` processes started by hand. `POST /admin/bootstrap/{phase}`
makes the ordinary shape *worker claims the job while an operator has the CLI
running in a terminal* — and note what does **not** reach it: `(kind, key)`
unique means two presses of one phase are one job, and the single `JobWorker`
lane serialises the jobs that exist, so no pair of *jobs* can race. It takes a
second process. Both candidate fixes (an advisory lock around the window, or
entering it only after `start()` is won) are behaviour changes to an M2 path
that E5 was scoped to extract verbatim. What E5 does add is
`tests/integration/test_admin_bootstrap.py::test_the_load_window_declines_on_a_
live_catalog_and_keeps_both_indexes` — the half that stops an unauthenticated
route taking browse ordering away from every reader.

## `bootstrap-status`' two aggregates cost a third of a second at catalog scale (2026-08-12, M9 E6)

**Measured, not estimated**, because `GET /admin/bootstrap/status` makes the
same four reads on every request and the alternative to stating the number was
a cache nobody had measured either. Against a real **1,272,367-title** catalog
with a 15,565-vector genome (the T8 catalog), `psql \timing`, median of five, on
a **busy** box — so every figure is an upper bound:

| read | median | range |
|---|---|---|
| `count_titles()` — `SELECT count(*) FROM titles` | **80.6 ms** | 79.9–95.3 |
| `genome_coverage()` — the five-way aggregate | **248.6 ms** | 242.0–260.4 |
| `genome_coverage()` — `genome_revision GROUP BY` | **2.0 ms** | 2.0–2.5 |

`list_runs()` is six rows and is not worth a figure. So one report is **~331
ms**, and the shape of the cost is the part that matters: **three of
`_GENOME_COVERAGE`'s five terms are themselves full scans of `titles`**
(`count(*)`, `kind = 'movie'`, `enrichment_state <> 'skeleton'`), so the report
scales with the catalog rather than with what is on the screen and re-reading
`titles` four times per request is where the quarter-second goes.

**No cache was added and the number is stated instead.** A cache here would be
an unmeasured mechanism on the one page an operator opens precisely because
they do not trust what they last saw, and it would have to be invalidated by a
writer in another process — the `bootstrap` job runs on the worker lane. The
consequence to carry: **this shape is an admin page's and nothing else's.** A
client route assembling `BootstrapReport` would be paying a full-catalog scan
per request.

## A `--phase imdb` run raises 61 `bootstrap.progress` frames (2026-08-12, M9 E7)

Derived arithmetic over measured counts, **not** an observed frame total, and
labelled as such because the number it is compared against is measured.
`_ImdbDataset` yields a batch every `bulk_batch_size` **retained** rows
(`batch.append(parsed)` then `len(batch) >= self._batch_size`), and E7
publishes one frame per committed batch, so at the shipped default of 50,000:

| dataset | retained rows | frames |
|---|---|---|
| `imdb.title.basics` | 1,271,138 | 26 |
| `imdb.title.ratings` | 1,700,615 | 35 |
| **`--phase imdb`** | | **61** |

Against `sync.progress`' **measured** 1,127 for one nightly walk of the one
library this project has measured (`services/reconcile.py:276`), so this is the
lower-rate of the two producers by an order of magnitude and the SSE bus's
queue bound is not in play. The retained counts are M2's own end-to-end run,
recorded further up this file.

**The number that matters is not the total, it is that it is >1 per run.** It
is what makes deferring these frames behind `DeferredEventPublisher` a
0%-to-100% jump rather than a rounding error, which is why
`composition.build_worker` hands the `bootstrap` registration `pipeline.events`
where every other registration gets `worker.events`. A `--phase all` run adds
`credit-names`, `aliases`, both TMDb id exports, the crosswalk and MovieLens on
top.

## T4R — the IMDb/TMDb provenance design, re-measured against a bar that means something (2026-08-12)

⚠️ **What shipped out of this is `m09d`'s schema and nothing else.**
`credits.source`, `people.imdb_id` and `ix_credits_source_natural_key` exist;
**no IMDb row has ever been written to either table**, both new indexes are
empty, and `CREDIT_SOURCE_PRECEDENCE` arbitrates a second source that does not
yet exist. Every figure below is a *priced* design, not a deployed one — which
is what makes it re-usable and what makes quoting it as current wrong.

**T3's refusal is reversed, and two of its three reasons did not survive
scrutiny.** The entry above stands as a measurement; what follows is what
changed. Bar written to `/var/tmp/t4r/BAR.md`
(`sha256 fbb9ced3f33840989d81841c48b51dcaeefb1d4ada5bfb2ad5df157ded223e30`,
2026-08-12T14:49:10-05:00) **before the first byte was downloaded**, and
`scripts/measure_people_provenance.py` re-hashes it at the start of every
phase and refuses to run if it has moved. Catalog: **1,272,367 titles**,
130,647 enriched, at `m09c`. Pin: `title.principals.tsv.gz`
`"f4422fc329ee8db79fb20dc7e3b64775-93"`, `name.basics.tsv.gz`
`"77f3a29e65e01ccaedb639e4d83e6db5-37"` — `Last-Modified` a day apart, so
**the seven dumps are still not one snapshot**, reconfirmed on a new pin.

🔴 **The 2.0 GB ceiling was not a constraint and the refusal it produced was
false.** It was derived from PRD 08's `~8–12 GB`, which is one row of a table
headed *Resource envelope* — a sizing estimate for an operator that no code,
host or policy reads. Re-measured against 25 GB (~3% of this host's free
disk): the whole design is **3,374,514,176 B = 3.375 GB = 3.143 GiB**, 13.5%
of the ceiling, for 12,637,249 credits over 3,215,476 people after
`VACUUM (FULL, ANALYZE)`. **A bar with nothing behind it turns a correct
measurement into a false refusal**, and PRD 08's row now says what it is for.

🔴 **`(title_id, ordering)` is UNIQUE over the whole `title.principals`, and
the M9 plan's proposed key is two columns wider than it needs to be.** Over
**101,170,912 data rows** (0 at a wrong column count): **0 rows lack an
`ordering` and 0 repeat one within a `tconst`.** Over the 12,638,471 rows this
catalog retains (12.49%):

| candidate | distinct | verdict |
|---|---|---|
| `(title_id, ordering)` | 12,638,471 | **UNIQUE** |
| `(title_id, nconst, category, ordering)` | 12,638,471 | UNIQUE, redundant |
| `(title_id, nconst, category)` | 12,276,307 | 362,164 collide |
| `(title_id, nconst, kind)` | 11,294,913 | 1,343,558 collide |

The plan named `(title_id, person_id, category, ordering)`. `category` is not
a column on `credits` at all — IMDb's 13 categories fold into `CreditKind`'s
two — and `person_id` is redundant once `ordering` is in the key. T3 measured
1,341,798 collisions on `(title_id, person_id, kind)` against a different
pin; 1,343,558 is the same fact one snapshot later. **Measure the key over the
whole file and not only the retained slice**: a key unique on one catalog's
slice and not on the file breaks on somebody else's catalog.

**The dedup bar, demonstrated in both directions rather than asserted.** The
pre-`m09d` shape — `tmdb_credit_id` its only unique key, NULL on every IMDb row
— loaded twice from the identical pinned bytes goes **12,637,249 → 25,274,498**,
exactly 2×. The scoped delete plus `(title_id, source, billing_order)` — which
is what `m09d` then shipped as `ix_credits_source_natural_key`, partial on
`source <> 'tmdb'` with `NULLS NOT DISTINCT` — leaves the count and the key-set
md5 unchanged. **The failing arm is the half that matters**: a dedup key never
shown to be load-bearing is a key nobody measured.

🔴 **The blast-radius correction is confirmed at catalog scale, and the
denominator is the point.** The fill would write a non-empty `credit_names`
to **1,063,418 of 1,272,367 titles**, all of them among the 1,141,720
skeletons the `AND m.ours` predicate permits. **0 of them carry an
embedding** — the embedded population is 130,647 of 130,647 non-skeleton
titles, and `embedded_skeletons` is **0**. So the intersection is empty by
measurement as well as by construction, and the ten sites that claimed the
fill "stales ~100% of the priority tier" were stating an arithmetically
impossible number.

🔴 **887,161 requests, not 1,536,654 — and the larger figure is real, it is
just counting something else.** `raw_payloads`' `credits.cast[]`/`crew[]`
arrays hold **5,614,150 entries over 1,536,654 distinct person ids**, both
reproduced exactly. But `adapters/tmdb/mapping._CAST_LIMIT` stores at most 50
cast per title, so the catalog **holds** 2,877,486 credits over **887,161**
people. Resolving the people that exist is **1.73× cheaper** than resolving
the people a payload mentions. **A count taken off the cache and a count taken
off the table are different numbers, and the request budget wants the second.**

**Do not price a TMDb crawl from any observed lane rate — price it from the
policy ceiling.** 887,161 requests is **6.2 h** at TMDb's stated ~40 rps,
**8.2 h** at the shipped 30 rps default, **9.9 h** at ADR-0005's self-imposed
~25 rps. **9.9 h is the number to quote**, because 25 rps is Usher's own policy
and leaves headroom for the enrich lane. ⚠️ **M9's 18.3 rps is not that number
and its mechanism no longer exists**: it was an artifact of `JobWorker.
run_once`'s `for job in claimed: await self._run(job)` with a commit per
iteration — exactly 1 in-flight request per process — and W1 replaced that loop
with a bounded `asyncio.wait` pool at `Settings.job_concurrency` (default 12,
`config.py:259`), measuring **1 → 12 in flight**. The conclusion survives the
fix, and is strengthened by it: W1 found the token bucket binding at every
setting it tried, and that bucket *is* the policy. See W1 in
`tmdb-and-enrichment.md`.

**The ≤6-month cache term applies to `raw_payloads` and not to derived columns,
so where the `nconst` lands decides whether a crawl recurs.**
`RawPayloadStore`'s docstring makes `fetched_at` the compliance clock and
`oldest_fetched_at(provider)` the compliance query (ADR-0016). A cached person
payload is on that clock and expires; `people.imdb_id` is a derived column, in
the same class as `titles.imdb_id`. **Cache the response and the crawl is
recurring; store the derived id and it is not.** `_UPSERT_PEOPLE` cannot blank
it — its `DO UPDATE SET` names three columns and `imdb_id` is not one, so
`usher derive` cannot discard a crawl. That is an accident of a column list
somebody could extend without thinking, which is why it is now pinned by a test.

🔴 **The latency bar passed, and the carve-out that let it pass is the finding
rather than a caveat.** Nine probes before and after the load, 30 reps each,
probe values fixed first, on a box the quiet-check confirmed idle (drift
−0.001; an earlier run was discarded because the same check caught a sibling
worktree's `pytest`). Eight routes moved within ±5.4%. **`PeopleProvider`'s
recurring-people join went 11.08 → 76.08 ms p95, +586%** — and the bar excused
it, because *"for any probe whose baseline p95 is < 20 ms, treat a regression as
unproven"*, written to stop a 0.6 ms wobble reading as a failure, swallowed a
**65 ms** one. **A noise floor expressed as a percentage of a small baseline is
not a noise floor; express it as an absolute.**

**The regression is recoverable and needs both halves — measured, because
neither alone does it.** `credits` grows 2,877,486 → 15,514,735 (5.4×), and
the join walks it:

| configuration | p95 |
|---|---|
| baseline, before the load | 11.08 ms |
| after the load, as shipped | 76.08 ms |
| + `AND source = 'tmdb'` at the read | 59.4 ms |
| + `(title_id, source)` composite index, no filter | 81.0 ms |
| **+ both** | **11.71 ms** |

So the read-side arbitration is load-bearing for *performance* and not only
for correctness, and the index is useless without it. The index is
deliberately **not** shipped: nothing filters on `source` yet, and an index
with no reader is write cost this repository has already paid once
(`ix_titles_popularity`, dropped in `ffc`).

**The overlap, which is what the merge decision turns on.** 130,436 titles
carry TMDb credits, 1,194,003 carry IMDb credits, and **130,402 carry both —
99.97% of the TMDb-covered titles.** So the wholesale arbitration rule fires
on essentially every enriched title rather than in a corner. 887,161 TMDb
people against 3,215,476 IMDb people, **0 carrying both ids** (nothing merges
today). **534,412 lower-cased names appear in both sets** — a proxy with error
in *both* directions, not a count of duplicated humans: two people sharing a
name inflate it and one spelled differently across sources is missed. ADR-0003
is the reason it can only ever be a proxy.

**Denominators**, T3's shape re-taken on the newer pin: 1,194,030 of 1,272,367
titles (93.84%) have ≥1 principal; 3,216,472 distinct `nconst` referenced,
3,215,476 (99.97%) with a `primaryName`, 1 nameless and 995 absent; 12,637,249
credits stored from 12,638,471 retained principals, the 1,222-row difference
being credits naming one of the 996 unusable `nconst`.

### T4R, second pass — `/find` works, and the yield is what settles the merge (2026-08-12)

🔴 **The first pass called branch (c) "removed by a mechanism, not a cost"
because `/find/{nconst}?external_source=imdb_id` was unverified. It works** —
200, 240 live requests over three bursts, driven from a throwaway script outside
the tree reading the operator's `.env`. The uncertainty was flagged rather than
asserted and the conclusion drawn from it was still an overstatement: **an
unverified "cannot" doing the work of a measured "does not pay"**, the same
error that withdrew this task the first time, one scale down. **If a credential
exists on the box, probe the endpoint; do not reason about it.**

`person_results[0]` carries `adult, gender, id, known_for,
known_for_department, media_type, name, original_name, popularity,
profile_path` — **`name` non-empty, and every field `Person` stores**, so the
IMDb→TMDb direction needs **no follow-up `/person/{id}` call**. (A key list
circulated in review omitted four of the ten; read the response, not a summary
of it.) **No rate-limit differential** between `/find` and
`/person/{id}/external_ids`: no `x-ratelimit-*` or `Retry-After` on either, no
429 at concurrency 8, 27.8–32.4 req/s for both. ADR-0005's ceiling governs both.

🔴 **Both merge directions have a low yield, and that is a better argument
than any reversibility tie-break.** Uniform samples, live:

| direction | sample | resolved | rate |
|---|---|---|---|
| `/person/{id}/external_ids` over stored TMDb people | 200 of 887,161 | 100 | **50.0%** (CI ≈ 43–57%) |
| `/find/{nconst}` over retained IMDb people | 200 of 3,215,476 | 41 | **20.5%** (CI ≈ 15–27%) |

**Half the TMDb people this catalog stores carry no IMDb id at all**, so
887,161 requests buy ~444,000 resolvable people — **at most 13.8% of the
3,215,476 IMDb person rows**. Four in five IMDb people are unknown to TMDb, so
the other direction costs ~5 requests per successful merge. **Two rows per
human is irreducible for most people whichever branch is bought.**

**Sample the whole file, not its head.** `name.basics` is sorted by `nconst`,
so the first N rows are the oldest and best-documented people on IMDb. The
first pass shuffled only the first 40,000 lines; a reservoir over the whole
slice is what makes 20.5% a rate rather than a flattering one.

**Live cost of `m09d` itself, separated from T6's predicted cost — measured by
cloning a real catalog at `m09c`, probing, migrating, and probing again.**
`PeopleProvider`'s join goes **10.04 → 10.37 ms p95 (+3.2%)** and every other
route's worst measurable delta is **+0.0%**. So the **+586% regression is not
live at this revision** — it is T6's data load priced ahead of time on a table
built for the dedup probe and dropped. What *is* live is the migration's own
cost: **50.4 s** at 2,877,486 credits, and **+637,034,496 B (+80.2%)
transient** on `credits` (794,050,560 → 1,431,085,056) because one `UPDATE` of
2.88M rows leaves a dead tuple per live one. `VACUUM FULL` settles it at
**740,130,816 B — 53,919,744 B *below* baseline** despite a new column and two
indexes, because the vacuum rebuilds every index by sort while the baseline's
btrees were built incrementally by `COPY`. **The migration does not run that
vacuum**, so budget the peak. Same confound, same framing, as T3's
`credit_names` figures.

**Both new indexes are empty at this revision** (8,192 B each) — every row is
`source = 'tmdb'` and the natural key is partial on `source <> 'tmdb'`.

## `--phase ratings` shares `--phase imdb`'s checkpoint, so a rebuild deletes the row first (2026-08-19, ADR-0040)

`usher bootstrap --phase ratings` re-imports `title.ratings.tsv.gz` (8.2 MiB)
alone, writing **`titles.imdb_average_rating` and `titles.imdb_num_votes`** and
nothing else (`db/repositories/bulk.py:622`). `--phase imdb` downloads
`title.basics.tsv.gz` (214.4 MiB) first and rewrites every name and year — and a
changed name stales that title's embedding, which a *rating* refresh against a
live catalog should not pay for.
It is an **alias, not a step**: `FULL_SEQUENCE` and `PHASE_ALIASES` partition
`BootstrapPhase`, and `--phase all` reaches these rows inside its IMDb arm and
never dispatches this one. Adding it to both arms imports the file twice.

**Its dataset name is `imdb.title.ratings` — deliberately the same `import_runs`
row `--phase imdb` checkpoints against**, so the two cannot disagree about what
revision a catalog holds. **The cost is that a completed run at an unchanged
upstream revision resumes at EOF and writes nothing, which looks exactly like a
successful run.** So a rebuild:

1. `DELETE FROM import_runs WHERE dataset = 'imdb.title.ratings'` first, and
   verify 0 rows remain;
2. asserts on **`rows_written`**, not on the exit status.

⚠️ **Do not rely on "the ETag will probably have moved."** It had, on the run
that established this — the stored revision and the pinned one differed, so
`import_dataset` would have discarded its cursor unaided — and the checkpoint
was deleted explicitly anyway, because a run whose correctness depends on an
upstream having changed is a run that reports success for doing nothing.
Measured: `rows_seen` 1,707,194, `rows_written` 540,850 in 145.5 s.

**It opens no `bulk_load_window()`**, and not only because that window declines
on a non-empty `titles`: M9 E5's race in this file has two processes over an
*empty* catalog both read the guard as zero, both `DROP INDEX`, and the loser's
`CREATE INDEX IF NOT EXISTS` then take a SHARE lock that blocks the winner's
batch writes. Opening it here would add a third racer, on a catalog nobody asked
to have reindexed.

### It refuses an empty catalog, and the refusal is not defensive

`apply_ratings` is an `UPDATE ... FROM ... WHERE t.imdb_id = s.imdb_id`, so on
an empty catalog it matches nothing — **and matching nothing is not an error.**
The run reaches EOF, writes 0 rows and checkpoints `imdb.title.ratings`
`completed`, which is the same row `--phase imdb` resumes from: every later
bootstrap then resumes past the whole file and imports no ratings at all,
**permanently**, with `bootstrap-status` green throughout. Measured end to end
against real Postgres before the guard existed: `--phase ratings` on an empty
catalog left `completed / rows_seen=3 / rows_written=0 / position=4`, the
`--phase imdb` that followed left the *identical* row, and the catalog held 5
titles with **0** carrying `imdb_num_votes`.

This is the **fourth** phase joining `titles` on `imdb_id` and was the last to
gain the refusal the other three always carried. It had stood as a *comment*
asserting the precondition, and a comment does not run. **A phase that writes
through a join can poison a shared checkpoint by succeeding**, which is a
failure mode no per-phase status field can express — the row says `completed`
and it is telling the truth.

⚠️ **The guard belongs in the phase's own function, not inline in the
dispatch.** Written inline it returns from the whole of `run_bootstrap`, so a
`--phase all` that wrongly reached this arm would abandon every later phase
instead of merely doubling an import — a strictly worse failure introduced by
the fix. Its three siblings each return from their own function; this one now
does too.


## `--phase` is `BootstrapPhase`, and two of its members are aliases

Moved out of `CLAUDE.md` on 2026-09-01, because it is detail about one command's
argument and **every path in this file's `paths:` frontmatter is a path from
which it matters** — the seven are `adapters/bulk/**`, `services/bootstrap.py`,
`domain/people.py`, `domain/bootstrap.py`, and the three `measure_*` scripts.
`domain/bootstrap.py` is the one that declares the split.

**The *steps* of a full run, in execution order:** `imdb`, `credit-names`,
`aliases`, `tmdb-ids`, `crosswalk`, `movielens` —
`domain/bootstrap.py:93-100`. Two members are **aliases rather than steps** and
`--phase all` dispatches neither: `all` itself, and **`ratings`**, which
re-imports `title.ratings.tsv.gz` (8.2 MiB) alone rather than paying
`--phase imdb`'s 214.4 MiB of `title.basics.tsv.gz` and the rewrite of every
name and year that stales an embedding (ADR-0040). `FULL_SEQUENCE` and
`PHASE_ALIASES` are declared side by side and a unit case asserts they partition
the enum — so a member added to neither is a red rather than a phase `argparse`
offers, the route accepts and `run_bootstrap` silently ignores.

⚠️ **`--phase all` is six steps, and a comment in this file called it three
until 2026-09-02** — a figure that was true of M2's run, when `credit-names`,
`aliases` and `movielens` did not exist, and is the reason the two steps that
dominate the runtime measured above (826.1 s + 520.1 s of a 22 min 26 s run)
read as unaccounted for.

The order is measured rather than stylistic: `credit-names`, `aliases` and
`movielens` all join `titles` on `imdb_id`, so all three follow `imdb`; and
`credit-names` precedes anything that *enriches* a title, because
`fill_credit_names` writes only skeletons and a title already enriched is
deferred to TMDb permanently (**203,969 of 204,335** ≥100-vote titles gain names
in this order and none in the other). It stales no embedding in either order,
the embedded population being the exact complement of what it writes.

**M9 Track 2's boundary call, moved here from `milestone-boundary-calls.md` on
2026-09-01.** Its numbered items 2, 3 and 5 were the same measurements this file
already carries — the skeleton precedence rule, the `credit_names` sizing
(+624 MB settled / +1,368 MB transient / GIN ×4.54), and the contiguity finding
— and were collapsed into their own sections on 2026-09-02 rather than kept as a
second copy. Item 4 was the one mechanism with no home:

**`replace_aliases` is scoped by `imdb_ids` *and* `kind = 'alias'`**, so B1's
`person` rows survive an alias re-import. The caller's scope is necessarily the
batch's own titles, which means a title whose akas IMDb has **withdrawn
entirely** keeps its stale aliases — a streaming importer has no other scope
available, and the alternative is one call naming 1.27M titles.
