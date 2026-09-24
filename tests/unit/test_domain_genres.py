"""The canonical genre vocabulary.

No database, no network.
"""

from usher.domain.genres import (
    CANONICAL_GENRES,
    GENRE_ALIASES,
    canonical_genres,
    canonicalise_genres,
    genre_spellings,
)


def test_the_two_source_spellings_of_science_fiction_share_one_canonical_label() -> None:
    """`Sci-Fi` is IMDb's spelling and `Science Fiction` is TMDb's.

    Either `/browse` facet button on its own silently serves a viewer half the
    population.
    """
    assert canonical_genres("Sci-Fi") == ("Science Fiction",)
    assert canonical_genres("Science Fiction") == ("Science Fiction",)


def test_expanding_a_filter_is_symmetric_in_the_spelling_the_client_sent() -> None:
    """A bookmarked `?genre=Sci-Fi` and a facet `?genre=Science Fiction` are one query.

    Set equality of the whole expansion rather than membership: an implementation that
    expanded only the canonical spelling passes a membership check and still serves the
    legacy client half a concept.
    """
    assert set(genre_spellings("Sci-Fi")) == set(genre_spellings("Science Fiction"))
    assert set(genre_spellings("Sci-Fi")) == {"Sci-Fi", "Science Fiction", "Sci-Fi & Fantasy"}


def test_a_fused_tmdb_tv_label_decomposes_into_both_concepts_it_names() -> None:
    """TMDb's *television* vocabulary fuses concepts its movie vocabulary keeps apart.

    All three fused labels are in this catalog, and collapsing one onto a single
    canonical label would delete the other half of what it says.
    """
    assert canonical_genres("Sci-Fi & Fantasy") == ("Science Fiction", "Fantasy")
    assert canonical_genres("Action & Adventure") == ("Action", "Adventure")
    # `War & Politics` is the asymmetric one: there is no canonical `Politics`
    # for the second half to land in, so it names one concept and not two.
    assert canonical_genres("War & Politics") == ("War",)


def test_an_unknown_label_is_its_own_canonical_and_its_own_only_spelling() -> None:
    """The vocabulary is Usher-owned but the *column* is open.

    A third source's genre, or a TMDb genre minted after this table was written, must
    still filter rather than vanish from every answer.
    """
    assert canonical_genres("Sword & Sandal") == ("Sword & Sandal",)
    assert genre_spellings("Sword & Sandal") == ("Sword & Sandal",)


def test_canonicalising_a_title_s_labels_dedupes_and_keeps_first_seen_order() -> None:
    """The dedupe is what makes the backfill sweep idempotent.

    A title carrying `Sci-Fi & Fantasy` and `Sci-Fi` names Science Fiction once, not
    twice, so re-running the sweep over its own output is a no-op.
    """
    assert canonicalise_genres(("Sci-Fi & Fantasy", "Sci-Fi", "Drama")) == (
        "Science Fiction",
        "Fantasy",
        "Drama",
    )


def test_canonicalising_is_idempotent_over_the_whole_vocabulary() -> None:
    """The property `usher genres --backfill` rests on, over every label in both tables.

    A backfill has to be safe to interrupt and safe to re-run, and both reduce to
    `f(f(x)) == f(x)`. The way this breaks is a future alias whose target is itself an
    alias — a two-hop map, which `canonical_genres` does not perform — and one
    hand-picked example cannot see it.
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
    """The guard that keeps the two tables in step.

    An alias pointing at a label `CANONICAL_GENRES` does not hold is a facet button no
    filter can reach, and nothing else in the system would say so.
    """
    for source, targets in GENRE_ALIASES.items():
        assert targets, f"{source!r} maps to nothing"
        for target in targets:
            assert target in CANONICAL_GENRES, f"{source!r} -> {target!r} is not canonical"


def test_no_canonical_label_is_also_an_alias() -> None:
    """A label on both sides of the map is a two-hop resolution nothing here performs.

    `canonical_genres` reads the alias table exactly once.
    """
    assert not CANONICAL_GENRES & set(GENRE_ALIASES)


def test_a_hand_typed_label_resolves_regardless_of_case() -> None:
    """`?genre=` is a URL an operator edits by hand and a bookmark they keep.

    An exact lookup against the sources' own casing answers `sci-fi` with an empty page
    and no way to tell "no such genre" from "no titles".
    """
    assert canonical_genres("sci-fi") == canonical_genres("Sci-Fi")
    assert canonical_genres("SCIENCE FICTION") == ("Science Fiction",)
    assert genre_spellings("science fiction") == genre_spellings("Science Fiction")
    assert genre_spellings("sci-fi & fantasy") == genre_spellings("Sci-Fi & Fantasy")


def test_folding_does_not_invent_a_match_for_a_label_outside_the_vocabulary() -> None:
    """The open-column invariant survives the fold.

    An unmapped label must stay byte-identical to what the client sent; folding it to
    lower case would silently stop matching the column it is checked against.
    """
    assert canonical_genres("Sword & Sandal") == ("Sword & Sandal",)
    assert canonical_genres("sword & sandal") == ("sword & sandal",)
    assert genre_spellings("SWORD & SANDAL") == ("SWORD & SANDAL",)


def test_no_two_labels_differ_only_by_case() -> None:
    """`_FOLDED` is keyed on `casefold()`.

    Two labels differing only in capitalisation would collapse, and one would silently
    shadow the other.
    """
    labels = [*GENRE_ALIASES, *CANONICAL_GENRES]
    folded = [label.casefold() for label in labels]
    assert len(set(folded)) == len(labels), "two labels differ only by case"
