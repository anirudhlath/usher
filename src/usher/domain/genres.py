"""One genre vocabulary over two importers that share none."""

from collections.abc import Iterable
from types import MappingProxyType

# : Usher's own vocabulary: every concept either importer can name, spelled : once.
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

# : Every source spelling that is not already canonical, and the concepts it : names.
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
