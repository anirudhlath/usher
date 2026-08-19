# ADR-0039 — The genre vocabulary is Usher's own, and the fix is at read time except where enrichment was deleting

**Status:** Accepted — closes [issue #30](https://github.com/anirudhlath/usher/issues/30)'s
user-visible half and stops its structural half. **Corrects PRD 02, 05 and 07**,
and records what the read-time scope deliberately leaves split. Measured
2026-08-19 against the live catalog (1,272,866 titles) and the real
`title.basics.tsv.gz`.

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
correctly restales every affected title: **~1.8 h of re-embedding plus a 3.3 h
`usher similar --rebuild`** on this catalog by the 2026-08-13 run. That bill is
scheduled deliberately alongside other document-staling work, not incurred by a
bug fix.

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
one.** Every one of these reads `titles.genres` verbatim:

- **`search_document`'s weight class D** — the two spellings share no lexemes.
  `to_tsvector('english','Sci-Fi')` is `'fi':3 'sci':2 'sci-fi':1` against
  `'fiction':2 'scienc':1`, so a query reaching the genres segment matches one
  half of the catalog.
- **The embedded population** — `compose_document` puts genres in segment 6 of
  7, so every stored vector carries whichever spelling its tier had.
- **`GenreAffinityProvider`, `TasteService`, `CurationPool`,
  `BecauseYouWatched`, `Seasonal`** — all read the raw column, and
  `list_owned_by_tag` is deliberately *not* expanded by this change.
  `library_genre_counts()` therefore still offers a household two buttons for
  one concept. Effect size unmeasured: this household owns 180 titles.
- **`SimilarityService`'s genre Jaccard (0.10)** — the trap issue #30 names and
  says is not sprung yet, and it is still not sprung for the same reason:
  `_POPULATION` excludes skeletons, so both sides of every stored pair speak
  TMDb's vocabulary. It springs the moment the embedded population widens past
  the enriched tier, which is the direction this project is going. `_jaccard`
  cannot distinguish "these two share no genres" from "we do not know either
  one's genres", so a skeleton sci-fi film and an enriched one score a hard 0
  while both are science fiction.
- **The browse cursor.** `CursorSpec.filters` carries the genre string
  verbatim, so a cursor minted under `?genre=Sci-Fi` and replayed under
  `?genre=Science Fiction` is still a `400 invalid_cursor` even though the two
  now name one population. Left alone deliberately: canonicalising the digest
  would make two spellings one cursor identity, and no client that gets its
  labels from `?facets=true` can reach the case.

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
