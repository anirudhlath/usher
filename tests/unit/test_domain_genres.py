"""The canonical genre vocabulary (ADR-0039). No database, no network.

`titles.genres` is written by two importers with no shared vocabulary — the
IMDb bulk phase writes IMDb's 28 labels, `EnrichService` writes TMDb's 19
movie + 16 TV ones — and on the live catalog (2026-08-19, 1,272,866 titles)
the two alphabets are **disjoint on every concept they both name**: 20,051
titles carry `Sci-Fi`, 6,223 carry `Science Fiction`, and **zero** carry both.

These cases are about the map that makes those one concept, and about the two
properties a reader has to be able to trust: that expanding a filter is
symmetric in the spelling the client sent, and that an unknown label is left
exactly alone.
"""

from usher.domain.genres import (
    CANONICAL_GENRES,
    GENRE_ALIASES,
    canonical_genres,
    canonicalise_genres,
    genre_spellings,
)


def test_the_two_source_spellings_of_science_fiction_share_one_canonical_label() -> None:
    """The visible half of issue #30. `Sci-Fi` is IMDb's and `Science Fiction`
    is TMDb's, and a viewer picking one of the two `/browse` facet buttons
    silently loses 6,223 or 20,051 titles."""
    assert canonical_genres("Sci-Fi") == ("Science Fiction",)
    assert canonical_genres("Science Fiction") == ("Science Fiction",)


def test_expanding_a_filter_is_symmetric_in_the_spelling_the_client_sent() -> None:
    """A bookmarked `?genre=Sci-Fi` and a facet-driven `?genre=Science Fiction`
    are one query over one population. Asserting **set equality of the whole
    expansion** rather than membership: an implementation that expanded only
    the canonical spelling passes a membership check on the canonical arm and
    still serves the legacy client half a concept."""
    assert set(genre_spellings("Sci-Fi")) == set(genre_spellings("Science Fiction"))
    assert set(genre_spellings("Sci-Fi")) == {"Sci-Fi", "Science Fiction", "Sci-Fi & Fantasy"}


def test_a_fused_tmdb_tv_label_decomposes_into_both_concepts_it_names() -> None:
    """TMDb's *television* vocabulary fuses concepts its movie vocabulary keeps
    apart, and all three fused labels are in this catalog — `Sci-Fi & Fantasy`
    165, `Action & Adventure` 154, `War & Politics` 25 (measured 2026-08-19).
    Collapsing one of them onto a single canonical label would delete the other
    half of what it says."""
    assert canonical_genres("Sci-Fi & Fantasy") == ("Science Fiction", "Fantasy")
    assert canonical_genres("Action & Adventure") == ("Action", "Adventure")
    # `War & Politics` is the asymmetric one: there is no canonical `Politics`
    # for the second half to land in, so it names one concept and not two.
    assert canonical_genres("War & Politics") == ("War",)


def test_an_unknown_label_is_its_own_canonical_and_its_own_only_spelling() -> None:
    """The vocabulary is Usher-owned but the *column* is open — a third source,
    or a TMDb genre minted after this table was written, must filter exactly as
    it did before this change rather than vanishing from every answer."""
    assert canonical_genres("Sword & Sandal") == ("Sword & Sandal",)
    assert genre_spellings("Sword & Sandal") == ("Sword & Sandal",)


def test_canonicalising_a_title_s_labels_dedupes_and_keeps_first_seen_order() -> None:
    """What the facet collapse and `GenreNormalisationService` both need, and
    the dedupe is what makes the sweep idempotent: a title carrying
    `Sci-Fi & Fantasy` and `Sci-Fi` names Science Fiction once, not twice, so
    re-running the backfill over its own output is a no-op."""
    assert canonicalise_genres(("Sci-Fi & Fantasy", "Sci-Fi", "Drama")) == (
        "Science Fiction",
        "Fantasy",
        "Drama",
    )


def test_canonicalising_is_idempotent_over_the_whole_vocabulary() -> None:
    """**The property `usher genres --backfill` rests on, asserted directly
    rather than inferred from one example.**

    A backfill over 1.27M rows has to be safe to interrupt and safe to re-run,
    and both reduce to `f(f(x)) == f(x)`. Written over *every* member of both
    tables rather than over a chosen pair, because the way this breaks is a
    future alias whose target is itself an alias — a two-hop map, which
    `canonical_genres` does not perform — and one hand-picked example cannot
    see it. `test_no_canonical_label_is_also_an_alias` guards the same hazard
    structurally; this one guards it behaviourally, and neither implies the
    other.
    """
    for label in sorted(CANONICAL_GENRES | set(GENRE_ALIASES) | {"Sword & Sandal"}):
        once = canonicalise_genres((label,))
        assert canonicalise_genres(once) == once, label

    # And over a whole array, where the dedupe is what has to be stable: the
    # second pass sees an input the first pass shortened.
    messy = ("Sci-Fi & Fantasy", "Sci-Fi", "Drama", "Drama", "reality-tv")
    once = canonicalise_genres(messy)
    assert once == ("Science Fiction", "Fantasy", "Drama", "Reality")
    assert canonicalise_genres(once) == once


def test_every_alias_resolves_into_the_canonical_vocabulary() -> None:
    """The guard that keeps the two tables in step. An alias pointing at a
    label `CANONICAL_GENRES` does not hold is a facet button no filter can
    reach, and nothing else in the system would say so."""
    for source, targets in GENRE_ALIASES.items():
        assert targets, f"{source!r} maps to nothing"
        for target in targets:
            assert target in CANONICAL_GENRES, f"{source!r} -> {target!r} is not canonical"


def test_no_canonical_label_is_also_an_alias() -> None:
    """A label on both sides of the map is a two-hop resolution nothing here
    performs — `canonical_genres` reads the alias table exactly once."""
    assert not CANONICAL_GENRES & set(GENRE_ALIASES)


def test_a_hand_typed_label_resolves_regardless_of_case() -> None:
    """`?genre=` is a URL an operator edits by hand and a bookmark they keep.

    The alias table is written in the sources' own casing, so before this the
    lookup was exact: `Sci-Fi` expanded to both spellings and `sci-fi` matched
    the `@>` containment literally and returned **nothing** -- an empty page
    with no way to tell "no such genre" from "no titles". The vocabulary
    exists so a client need not know a source's spelling; requiring its
    capitalisation gives that back with the other hand.
    """
    assert canonical_genres("sci-fi") == canonical_genres("Sci-Fi")
    assert canonical_genres("SCIENCE FICTION") == ("Science Fiction",)
    assert genre_spellings("science fiction") == genre_spellings("Science Fiction")
    assert genre_spellings("sci-fi & fantasy") == genre_spellings("Sci-Fi & Fantasy")


def test_folding_does_not_invent_a_match_for_a_label_outside_the_vocabulary() -> None:
    """The open-column invariant survives the fold, and this is the case that
    would catch a fold applied too early.

    An unmapped label must stay **byte-identical** to what the client sent --
    a third source's genre, or a TMDb label minted after this table was
    written, still filters exactly as `@>` always did. Folding it to lower
    case would silently stop matching the column it is checked against.
    """
    assert canonical_genres("Sword & Sandal") == ("Sword & Sandal",)
    assert canonical_genres("sword & sandal") == ("sword & sandal",)
    assert genre_spellings("SWORD & SANDAL") == ("SWORD & SANDAL",)


def test_no_two_labels_differ_only_by_case() -> None:
    """`_FOLDED` is keyed on `casefold()`, so two labels differing only in
    capitalisation would collapse and one would silently shadow the other.
    The vocabulary is small enough to assert this rather than hope for it."""
    labels = [*GENRE_ALIASES, *CANONICAL_GENRES]
    folded = [label.casefold() for label in labels]
    assert len(set(folded)) == len(labels), "two labels differ only by case"
