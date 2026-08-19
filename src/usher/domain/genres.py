"""One genre vocabulary over two importers that share none.

`titles.genres` is written from two places with no agreement between them. The
IMDb bulk phase (`adapters/bulk/imdb.py`) writes IMDb's 28 labels; `EnrichService`
lists `genres` among the fields it replaces wholesale from TMDb, which writes
TMDb's 19 movie genres or its 16 television ones. Measured on the live catalog
2026-08-19 over 1,272,866 titles: **37 distinct labels, and the two alphabets
are disjoint on every concept they both name.** 20,051 titles carry `Sci-Fi`
and 6,223 carry `Science Fiction`; **zero carry both**, and the same holds for
all nine alias pairs below. `/browse?genre=` was exact containment, so it
answered half a concept under either spelling and `?facets=true` offered both
as separate buttons.

**The vocabulary is Usher's own rather than TMDb's** — [ADR-0039](
../../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md). TMDb's
is smaller and is what the enriched tier already speaks, which is the argument
for taking it; the reason not to is that seven concepts have nowhere to go in
it. `Biography` (24,552 titles), `Musical` (13,546), `Sport` (19,918),
`Short` (6,248), `Game-Show` (10,729), `Film-Noir` (49) and `Adult` are named by
IMDb and by no TMDb genre in either id space, so a TMDb-canonical vocabulary
does not *rename* them — it has no name for them at all.

**Two rules, and the second is the one that is easy to get wrong.**

1. A label maps to the canonical labels it *names*. Six do not name themselves.
2. **A fused label names two concepts, not one.** TMDb's television vocabulary
   fuses concepts its movie vocabulary keeps apart, and all three fusions are
   in this catalog: `Sci-Fi & Fantasy` (165), `Action & Adventure` (154),
   `War & Politics` (25). Collapsing one onto a single canonical label deletes
   the other half of what it says. `War & Politics` is the asymmetric case —
   there is no canonical `Politics` for its second half to land in, so it maps
   to one label and not two.

**What this module does not do.** It normalises nothing at write time. The
column still holds whatever its two importers wrote, and this map is applied by
the reader — `PostgresTitleRepository._browse_filters` expands a filter into
every spelling of the concept, and `browse_facets` collapses the counts back.
ADR-0039 records what a write-time normalisation would cost (it changes segment
6 of `compose_document`, so `_FINGERPRINT_SQL` correctly restales every affected
title: ~1.8 h of re-embedding plus a 3.3 h `usher similar --rebuild` on this
catalog) and what stays split until it is paid.

**The facet collapse sums its spellings' counts, and that is exact only while
no title carries two spellings of one concept.** Measured zero across all nine
alias pairs on 1,272,866 titles, and `EnrichService` cannot create one — it
preserves a label only when the provider's vocabulary has no name for its
concept, which is by definition a concept with a single spelling. Write-time
normalisation is what would make a title carry both, and it is also what would
make the collapse unnecessary. The exact spelling (`SELECT DISTINCT (id,
canonical)`) was measured at **1,789 ms against 199 ms** on the live catalog,
which is why the sum is what ships.
"""

from collections.abc import Iterable
from types import MappingProxyType

#: Usher's own vocabulary: every concept either importer can name, spelled
#: once. The rule that picks each spelling is ADR-0039's and it has exactly one
#: clause — **TMDb's spelling wherever TMDb names the concept, IMDb's verbatim
#: wherever it does not** — so `Science Fiction` beats `Sci-Fi`, `Reality` beats
#: `Reality-TV`, and `Film-Noir` keeps IMDb's hyphen because nothing else names
#: it. One clause rather than a per-label judgement, because a vocabulary whose
#: spellings are decided one at a time is a vocabulary nobody can extend.
CANONICAL_GENRES: frozenset[str] = frozenset(
    {
        "Action",
        "Adult",
        "Adventure",
        "Animation",
        "Biography",
        "Comedy",
        "Crime",
        "Documentary",
        "Drama",
        "Family",
        "Fantasy",
        "Film-Noir",
        "Game-Show",
        "History",
        "Horror",
        "Kids",
        "Music",
        "Musical",
        "Mystery",
        "News",
        "Reality",
        "Romance",
        "Science Fiction",
        "Short",
        "Soap",
        "Sport",
        "Talk",
        "Thriller",
        "TV Movie",
        "War",
        "Western",
    }
)

#: Every source spelling that is not already canonical, and the concepts it
#: names. Deliberately **only** the non-identity entries: a table that also
#: restated the twenty-odd labels both sources spell identically would be a
#: table where a missing row and a correct row look the same.
#:
#: `MappingProxyType` for immutability and not for hashability — CLAUDE.md's
#: rule, and `mappingproxy` delegates `__hash__` to the dict it wraps, which is
#: `None`.
GENRE_ALIASES: MappingProxyType[str, tuple[str, ...]] = MappingProxyType(
    {
        # IMDb's spelling of TMDb's `Science Fiction`. The pair issue #30 is
        # named for: 20,051 against 6,223, and zero titles with both.
        "Sci-Fi": ("Science Fiction",),
        # TMDb's *television* vocabulary, which fuses what its movie vocabulary
        # separates. Two concepts each, except the third.
        "Sci-Fi & Fantasy": ("Science Fiction", "Fantasy"),
        "Action & Adventure": ("Action", "Adventure"),
        "War & Politics": ("War",),
        # IMDb's hyphenated television labels against TMDb's television ones.
        # Both TMDb spellings are in this catalog (`Reality` 57, `Talk` 4), so
        # these are re-spellings and *not* the vocabulary gap — which is why
        # `EnrichService` lets TMDb overwrite them.
        "Reality-TV": ("Reality",),
        "Talk-Show": ("Talk",),
    }
)

#: The inverse of `GENRE_ALIASES`, built once. A canonical label's non-canonical
#: spellings, so `genre_spellings` is a lookup rather than a scan of the alias
#: table on every `/browse` request.
_SPELLINGS_OF: MappingProxyType[str, tuple[str, ...]] = MappingProxyType(
    {
        canonical: tuple(
            sorted(source for source, targets in GENRE_ALIASES.items() if canonical in targets)
        )
        for canonical in CANONICAL_GENRES
    }
)


#: Both tables keyed by `casefold()`, built once, so a hand-typed `?genre=`
#: resolves without the client knowing a source's capitalisation. Keyed on the
#: fold rather than lower-cased in place because the *values* stay in the
#: sources' own casing — they are compared against `titles.genres` verbatim.
_FOLDED: MappingProxyType[str, tuple[str, ...]] = MappingProxyType(
    {label.casefold(): targets for label, targets in GENRE_ALIASES.items()}
    | {canonical.casefold(): (canonical,) for canonical in CANONICAL_GENRES}
)


def canonical_genres(label: str) -> tuple[str, ...]:
    """The concepts `label` names, in Usher's vocabulary.

    A label that is already canonical, and a label from outside the vocabulary
    entirely, are both **themselves**. The second case is not a fallback — the
    *column* is open even though the vocabulary is not, and a third source (or
    a TMDb genre minted after this table was written) has to keep filtering
    exactly as it did rather than vanishing from every answer.

    **Case-insensitive on the way in, exact on the way out.** `?genre=` is a
    URL an operator edits by hand, and the vocabulary exists precisely so a
    client need not know how a source spells a concept — requiring its
    capitalisation hands that back: `?genre=sci-fi` returned an empty page
    with no way to tell "no such genre" from "no titles". The fold applies
    only to the *lookup*. An unmapped label is returned exactly as it
    arrived, never folded, because it is about to be compared against the
    column verbatim and lower-casing it would stop it matching anything at
    all — that is the invariant this function's second case has always
    carried, and the one a fold applied a line earlier would quietly break.
    """
    return _FOLDED.get(label.casefold(), (label,))


def genre_spellings(label: str) -> tuple[str, ...]:
    """Every spelling a `/browse` filter for `label` has to match.

    Symmetric in what the client sent: a bookmarked `?genre=Sci-Fi` and a
    facet-driven `?genre=Science Fiction` expand to the same set, because the
    label is resolved to its concepts first and the concepts are what carry
    spellings. An unmapped label expands to itself alone, so the filter is
    byte-identical to the `@>` containment it replaced.
    """
    found: dict[str, None] = {}
    for canonical in canonical_genres(label):
        found[canonical] = None
        for spelling in _SPELLINGS_OF.get(canonical, ()):
            found[spelling] = None
    return tuple(sorted(found))


def canonicalise_genres(labels: Iterable[str]) -> tuple[str, ...]:
    """`labels` in Usher's vocabulary, deduplicated, first-seen order kept.

    Order is preserved rather than sorted because a title's genre order is the
    provider's own relevance order, which `RowCard` and the curation prompt both
    render; the dedupe is what makes a title carrying `Sci-Fi & Fantasy` and
    `Sci-Fi` name Science Fiction once.
    """
    found: dict[str, None] = {}
    for label in labels:
        for canonical in canonical_genres(label):
            found[canonical] = None
    return tuple(found)
