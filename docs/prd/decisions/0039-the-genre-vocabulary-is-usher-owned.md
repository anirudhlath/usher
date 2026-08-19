# ADR-0039 — The genre vocabulary is Usher's own, and the fix is at read time except where enrichment was deleting

**Status:** Accepted — closes [issue #30](https://github.com/anirudhlath/usher/issues/30)'s
user-visible half and stops its structural half. **Corrects PRD 02, 05 and 07**,
and records what the read-time scope deliberately leaves split. Measured
2026-08-19 against the live catalog (1,272,866 titles) and the real
`title.basics.tsv.gz`.

⚠️ **Amended 2026-08-19, the same day: the deferral in point 2 was priced from
a number this ADR did not derive, the number was wrong by three orders of
magnitude, and the write-time normalisation it deferred now ships as `usher
genres --backfill`.** The bill is **304 embeddings**, not 132,000. Read the
Amendment at the foot of this file before reading point 2 or the "Still split"
list, both of which are corrected in place.

## Context

`titles.genres` is written by two importers that share no vocabulary. The IMDb
bulk phase (`adapters/bulk/imdb.py`) writes IMDb's 28 labels; `EnrichService`
lists `genres` among the fields it **replaces wholesale** from TMDb, which
writes TMDb's 19 movie genres or its 16 television ones. Nothing in `src/`
normalised, mapped or aliased a genre, and `/browse?genre=` was exact
containment (`TitleRow.genres.bool_op("@>")`).

**The two alphabets are disjoint on every concept they both name.** 37 distinct
labels in the column; `Sci-Fi` on 20,051 titles, `Science Fiction` on 6,223,
**zero** carrying both — and the same zero for all nine alias pairs. So
`/browse?genre=Sci-Fi` and `/browse?genre=Science Fiction` returned disjoint
sets for one concept and `?facets=true` offered both as separate buttons.

**The split follows the enrichment boundary exactly, and the deletion is now
observed rather than inferred.** Issue #30 inferred the deletion from the
replace-list plus the distribution. Joining the real IMDb dump to the live
catalog measures it directly:

| | |
|---|---|
| enriched titles the dump also gives genres for | 132,116 |
| …that lost **at least one** IMDb label | **53,724 (40.7%)** |
| total label deletions | **69,160** |
| …of a concept TMDb **cannot express** | **11,466** |
| skeletons that lost a label — the control | **0 of 1,021,623** |

The mechanism is confirmed on the same catalog: of 132,407 enriched titles with
a cached TMDb payload, **130,826 of the 130,826 whose payload supplied any
genre have `titles.genres` byte-identical to that payload's genre list, and
zero differ.** The 1,581 exceptions are titles whose payload supplied *no*
genres, where `_changes` skips the field — which is also why only 108 enriched
titles retain an IMDb-only label at all.

**Two defects, not one.** The synonym pair is a mapping. The vocabulary *gap*
is the larger half and a synonym table leaves it in place: seven concepts have
no TMDb name in either id space, so enrichment did not re-spell them.

| deleted label | deletions | survivals |
|---|---|---|
| `Biography` | 5,562 | 34 |
| `Musical` | 2,767 | 39 |
| `Sport` | 2,115 | 13 |
| `Film-Noir` | **827** | **0** |
| `Short` | 174 | 0 |
| `Game-Show` | 21 | 2 |

Against those, `Drama` was deleted 13,141 times and `Crime` 5,506 — TMDb
*disagreeing* with IMDb about a film, which it is entitled to do.

**Two of issue #30's own claims are refuted by the same measurement and are
corrected here.** (1) `Reality-TV`, `Talk-Show` and `News` are listed there as
having *no* TMDb equivalent; TMDb's television vocabulary has `Reality`, `Talk`
and `News`, so they are re-spellings and not gaps. (2) The issue records as
unmeasured "whether TMDb's TV vocabulary ever reaches this column" and notes
none of the five appears. **All of them do** — `Sci-Fi & Fantasy` 165,
`Action & Adventure` 154, `Reality` 57, `War & Politics` 25, `Kids` 19, `Soap`
19, `Talk` 4 — so the adapter does not map them away, and three of them *fuse*
concepts the movie vocabulary keeps apart.

## Decision

**1. The canonical vocabulary is Usher's own, not TMDb's** — 31 concepts in
`usher/domain/genres.py`. TMDb's is smaller and is what the enriched tier
already speaks, which is the argument for taking it; the reason not to is that
it has no name at all for `Biography` (24,552 titles), `Sport` (19,918),
`Musical` (13,546), `Game-Show` (10,729), `Short` (6,248), `Film-Noir` (49) or
`Adult`. A TMDb-canonical vocabulary does not rename those, it deletes them,
which is the defect rather than the fix.

**One clause picks every spelling, rather than a per-label judgement: TMDb's
where TMDb names the concept, IMDb's verbatim where it does not.** So `Science
Fiction` beats `Sci-Fi` even though `Sci-Fi` has 3.2× the titles, `Reality`
beats `Reality-TV` on the same rule against a 566× population, and `Film-Noir`
keeps IMDb's hyphen because nothing else names it. A vocabulary whose spellings
are decided one at a time is one nobody can extend.

**A fused label names two concepts.** `Sci-Fi & Fantasy` → `Science Fiction` +
`Fantasy`, `Action & Adventure` → `Action` + `Adventure`. `War & Politics` →
`War` alone, because there is no canonical `Politics` for its other half.

**2. The `/browse` fix is at read time.** `_browse_filters` expands the label
into every spelling of the concepts it names and matches with `&&`;
`browse_facets` collapses the `GROUP BY` into canonical keys. Write-time
normalisation would fix the lexical lane, the embeddings and every row provider
too — and it changes segment 6 of `compose_document`, so `_FINGERPRINT_SQL`
correctly restales every affected title. ~~That bill is **~1.8 h of re-embedding
plus a 3.3 h `usher similar --rebuild`** on this catalog by the 2026-08-13 run,
and is scheduled deliberately alongside other document-staling work rather than
incurred by a bug fix.~~

⚠️ **Struck 2026-08-19. Both figures are wrong and this ADR had the evidence to
know it.** They price re-embedding the *whole* embedded population; the
population a genre normalisation stales is **304 titles**, because 79,613 of
the 79,913 rows it rewrites are skeletons and a skeleton has no vector. The
re-embed is seconds. Point 2's read-time scope stands as what shipped that day
and is no longer what the project does — see the Amendment.

**3. The facet collapse is a *sum*, and its premise is measured.** Summing a
concept's spellings overcounts exactly when one title carries two of them, and
that is zero across all nine alias pairs on 1,272,866 titles. The exact
spelling (`SELECT DISTINCT (id, canonical)`) was written and timed on the live
catalog at **1,789 ms against this query's 199 ms**, against a facet block
whose B7 bar (p95 ≤ 200 ms) is already missed at 330.81 ms. Write-time
normalisation is both what would break the premise and what would remove the
need for the collapse.

**4. `EnrichService` stops deleting what the provider cannot name.**
`MetadataProvider` gains an abstract `genre_vocabulary` — the canonical
concepts that provider can express, i.e. **the set it is entitled to delete** —
and `_genres_after` keeps any existing label whose concept is outside it. TMDb
derives its set from `TMDB_GENRE_NAMES` rather than restating it.

This is a write-time change and it is in scope anyway because it is not a
backfill: it changes what the *next* enrichment writes, and `_apply` already
enqueues an `INDEX` job for every successful enrichment, so a title reaching
the merge was going to be re-embedded on that pass regardless. **Nothing is
restaled that was not already stale.** The 53,724 titles already enriched keep
their deletions until someone chooses to pay point 2's bill.

## Consequences

**Fixed.** `/browse?genre=` answers one concept under either spelling.
`?facets=true` offers one button per concept, and each count is the size of the
page pressing it serves. A future enrichment of a `Biography` skeleton keeps
`Biography`.

**Still split, and this is the list to read before assuming the concept is
one.** Every one of these reads `titles.genres` verbatim. **The verdicts are
2026-08-19's, after `usher genres --backfill` shipped** — the list is kept as
written and each entry now says whether the backfill closes it, because a list
of consequences that quietly loses members is one nobody can audit:

- ✅ **`search_document`'s weight class D** — the two spellings share no
  lexemes. `to_tsvector('english','Sci-Fi')` is `'fi':3 'sci':2 'sci-fi':1`
  against `'fiction':2 'scienc':1`, so a query reaching the genres segment
  matched one half of the catalog. **Closed by the backfill, and for free**:
  `search_document` is `GENERATED ALWAYS AS (...) STORED`, so the same `UPDATE`
  that moves the label recomputes the tsvector in the same statement. No job,
  no second pass.
- ✅ **The embedded population** — `compose_document` puts genres in segment 6
  of 7, so every stored vector carried whichever spelling its tier had.
  **Closed, but not by the backfill alone**: it stales the affected rows
  through `_FINGERPRINT_SQL` and an operator has to run `usher index
  --backfill` then `usher work` to re-embed them. 304 titles on this catalog.
- ✅ **`GenreAffinityProvider`, `TasteService`, `CurationPool`,
  `BecauseYouWatched`, `Seasonal`** — all read the raw column, and
  `list_owned_by_tag` is deliberately *not* expanded by this change, so
  `library_genre_counts()` offered a household two buttons for one concept.
  **Closed by the column itself**: normalising the data fixes every reader at
  once, which is the argument for a write-time fix that a read-time one cannot
  make five times over. Effect size still unmeasured — this household owns 180
  titles — so what is closed is the *defect*, not a measured improvement.
- ⚠️ **`SimilarityService`'s genre Jaccard (0.10)** — the trap issue #30 names
  and says is not sprung yet. **Disarmed rather than closed, and the
  distinction matters.** It was safe because `_POPULATION` excludes skeletons,
  so both sides of every stored pair spoke TMDb's vocabulary — a property of
  *who is embedded*, which the next milestone can change. It is now safe
  because both sides speak one vocabulary whoever they are, which is a property
  of the column. `_jaccard` still cannot distinguish "these two share no
  genres" from "we do not know either one's genres", and **that half is
  untouched**: ADR-0014's absence-is-not-zero problem is about a title with an
  *empty* `genres` array, 118,856 of them here, and no vocabulary fixes an
  empty set. **Follow-up: that is the residue of #30 worth its own issue**, and
  it is a `SimilarityService` question rather than a genre one.
- ❌ **The browse cursor.** `CursorSpec.filters` carries the genre string
  verbatim, so a cursor minted under `?genre=Sci-Fi` and replayed under
  `?genre=Science Fiction` is still a `400 invalid_cursor`. **Not closed, and
  now unreachable for a different reason.** The digest is still uncanonicalised
  on purpose — making two spellings one cursor identity is a decision about
  identity, not about vocabulary — but after the backfill no *facet* offers
  `Sci-Fi` at all, so the only client that can mint the mismatched pair is one
  typing both spellings by hand. Left as it was.

**The premise this ships with.** `_canonical_facet` is exact only while no
title carries two spellings of one concept. Point 4 cannot create one — a
concept with no TMDb name has exactly one spelling — but a third importer, or a
write-time normalisation that unions rather than replaces, would.

## Evidence

- Live catalog 2026-08-19, 1,272,866 titles: the 37-label distribution by
  enrichment tier; nine alias pairs at zero co-occurrence; `SELECT DISTINCT`
  facet at 1,789 ms against 199 ms.
- `title.basics.tsv.gz` (2026-08-10) joined to the live catalog: 53,724 /
  132,116 enriched titles lost a label, 69,160 deletions, per-label table
  above, control 0 / 1,021,623 skeletons.
- `raw_payloads` joined to `titles`: 130,826 enriched titles' genres are
  byte-identical to their TMDb payload's, 0 differ.
- `tests/unit/test_domain_genres.py`, the four browse cases in
  `tests/contract/title_repository_contract.py` (both arms), the two
  `tests/unit/test_services_enrich.py` cases, and
  `test_the_genre_vocabulary_is_every_tmdb_name_as_a_canonical_concept`.

## Amendment — 2026-08-19: the deferral was priced from a number nobody derived, and the write-time half now ships

**Status of the amendment:** Accepted — closes the structural half of issue
#30. The vocabulary, the one-clause spelling rule and the enrichment fix above
all stand unchanged. What changes is point 2: the column is normalised at write
time by `usher genres --backfill`, and the read-time expansion stays as the
belt to that braces.

### The cost that justified the deferral was wrong by three orders of magnitude

Point 2 deferred write-time normalisation citing **~1.8 h of re-embedding plus
a 3.3 h `usher similar --rebuild`**, taken from issue #30, which took it from
the 2026-08-13 run. That is the cost of re-embedding **the whole embedded
population**, and it is not the population a genre rewrite stales.

**This ADR contained the refutation on the day it was written.** Its own
Context says the split *"follows the enrichment boundary exactly"* and that
*"only 108 enriched titles retain an IMDb-only label at all"*; the search
subsystem's `_POPULATION` is `enrichment_state <> 'skeleton'`. Put together:
the rows carrying a source spelling and the rows carrying a vector are very
nearly disjoint. Nobody put them together.

Re-derived 2026-08-19 against the live catalog, with the predicate taken from
`GENRE_ALIASES` and `CANONICAL_GENRES` rather than hand-listed — *"affected"*
is spelled as `genres IS DISTINCT FROM canonicalise_genres(genres)`, evaluated
as a `VALUES` join generated from those two tables:

| | count |
|---|---|
| titles | 1,272,869 |
| …carrying at least one genre | 1,153,968 |
| **…the sweep rewrites** | **79,913** |
| **…of those, embedded** | **304** |
| total embedded | 132,440 |
| currently stale under `openai:BAAI/bge-m3` | **0** |

So the re-embed after a full backfill is **304 documents**, which is seconds,
and `usher similar --rebuild` is the operator's usual call about 304 moved
vectors rather than a 3.3 h precondition. **The deferral bought nothing it was
sold on.**

### Two ways to get "affected" wrong, and this repair hit both

The prompt for this work carried a hand-listed figure of **158,632** affected
and **108** embedded. Both are wrong, in opposite directions, and each error is
instructive:

- **158,632 counts every non-TMDb label, and half of them are canonical.**
  Usher's vocabulary keeps IMDb's spelling wherever TMDb names nothing —
  `Biography`, `Sport`, `Musical`, `Short`, `Game-Show`, `Film-Noir`, `Adult`
  are the *decision above*, not a defect. `canonicalise_genres` leaves every
  one of them alone. Only the six rows of `GENRE_ALIASES` rewrite anything:
  `Reality-TV` (32,238), `Talk-Show` (27,986), `Sci-Fi` (20,051),
  `Sci-Fi & Fantasy` (165), `Action & Adventure` (154), `War & Politics` (25).
- **108 misses the fused television labels, which are 100% embedded.**
  `Sci-Fi & Fantasy`, `Action & Adventure` and `War & Politics` are *TMDb's own*
  spellings, so they exist only on the enriched tier — all 344 label instances
  of them are on embedded titles. They are the reason the real figure is 304
  and not the ~18 the movie-vocabulary aliases contribute.

**And a hand-listed predicate cannot see the third case at all.** 12 titles
carry a *duplicate* label (`{Drama,Drama}`, `{Action,Drama,Romance,Action,
Drama,Romance}`) and normalise to a shorter array with no alias involved,
because `canonicalise_genres` deduplicates. That is why the shipped sweep
canonicalises **every** row in Python rather than filtering in SQL: the map is
the only definition of affected, and any `WHERE` clause restating it is a
second one that drifts.

### It is a command, not a migration

**The vocabulary is data, not schema.** `GENRE_ALIASES` will grow — a third
importer, a TMDb genre minted after the table was written — and a one-shot
Alembic migration normalises the catalog as of the day it ran with no way to
re-run it. It would also execute inside `alembic upgrade head`, which every
integration test and every container start runs, holding one transaction over
1.27M rows.

So it takes `usher index --backfill`'s shape: **sweep, write, report**, and
run it again whenever the map moves.

- **Its own subcommand, `usher genres`.** Not a flag on `usher index`, which is
  about `title_embeddings` and whose backfill enqueues jobs for a worker that
  owns a model; not a flag on `usher derive`, which needs a `MetadataProvider`,
  reads `raw_payloads` and writes four other tables. This needs no provider, no
  model and no cache, and writes one column.
  [ADR-0026](0026-the-cli-boundary-names-families.md)'s family rule applied to
  what a command is *about*.
- **Bare form reads, `--backfill` writes** — `index` and `derive`'s bargain, so
  a report is safe on a production box.
- **`--batch-size` is an argument and the batch is the transaction.** The right
  batch is a property of a deployment's `work_mem` and its operator's patience.
  An interrupted sweep loses at most one batch and leaves a normalised prefix,
  which is not a wrong catalog — the readers already expand both spellings.
- **`--limit` bounds rows *scanned*, `--after` resumes.** Compared against rows
  *written*, a limit never fires on a re-run — where the honest answer is zero
  writes — so the brake an operator reached for would sweep the whole catalog.
  That is `usher index --backfill`'s own recorded defect, avoided by having
  seen it.
- **Re-running is free and the statement is what makes it so.**
  `replace_genres` is `UPDATE titles SET genres = v.genres FROM (VALUES ...) v
  WHERE titles.id = v.id AND titles.genres IS DISTINCT FROM v.genres`, so
  `rowcount` is rows *changed*. Without that clause a second sweep reports work
  it did not do and writes 1.15M dead row versions — each also re-evaluating
  the `search_document` generated column and its GIN index — for no state
  change. Same argument as `_ENQUEUE`'s `AND jobs.priority < excluded.priority`.
- **No staging table**, deliberately. `usher.db.staging` exists for `COPY`-sized
  batches and costs DDL inside the transaction; an `UPDATE` keyed on the primary
  key has no conflict target, so none of `db/repositories/bulk.py`'s three
  `ON CONFLICT` traps apply and a `VALUES` join is the whole statement.

### The staling is the fingerprint's, and that was verified rather than assumed

`titles.genres` is segment 6 of `compose_document`, so a rewritten row stops
reproducing its stored `source_fingerprint` and `usher index` claims it. **The
backfill contains no staling mechanism of its own** — a second definition of
stale beside `_FINGERPRINT_SQL` is exactly the failure
`db/repositories/search.py` records as a dashboard reading zero while a worker
still claims rows.

That it *actually* happens is pinned by
`tests/integration/test_genre_backfill.py::test_the_rewrite_stales_the_embedding_through_the_shipped_fingerprint`,
which embeds a title at its own document, asserts as its **premise** that it is
not stale, runs the backfill, and asserts both that the label moved and that
`usher index --backfill` then enqueues it. Red was demonstrated by mutation
rather than claimed, and the careless spelling is not enough: deleting the
genres segment from `_FINGERPRINT_SQL` alone fails the *premise*, because SQL
then assembles six segments against the composer's seven and no title agrees.
The **careful** spelling — the segment emptied on *both* sides, so the two
still agree — passes the premise and fails the assertion the case is named for:

```
AssertionError: the genre moved and the title did not become stale --
segment 6 of compose_document is not reaching _FINGERPRINT_SQL
```

### What the reported numbers mean

`usher genres --backfill` prints rows scanned, rows rewritten, rows unchanged,
embeddings staled and a resume cursor. **`embeddings staled` is the difference
in what the stale predicate claims, not a count of rewritten rows carrying a
vector** — the two disagree by exactly the rows that were already stale. On
this catalog, whose stale count is currently 0, the full run reports
`rows rewritten: 79,913` and `embeddings staled: 304`.

### What this does not change

**Point 3's facet collapse stays, and its premise is now stronger rather than
weaker.** `_canonical_facet` sums a concept's spellings and is exact only while
no title carries two of them; a normalised catalog has one spelling per concept
by construction, so the sum is over a single key. The collapse is left in place
because it is what makes an *un*-normalised catalog — a fresh bootstrap, a
partially-swept one, a deployment that has not run this command — answer
correctly, and removing it would make `/browse` correct only after an
operator's action.

**Point 4 still does the work it was written for.** `EnrichService` preserving
what TMDb cannot name is about the *next* enrichment; this backfill is about
the rows already written. The 53,724 titles that lost a label to a past
enrichment still have it deleted — **normalisation is not restoration**, and
recovering those needs the IMDb dump rather than a vocabulary map. That is the
one part of issue #30 neither change closes.
