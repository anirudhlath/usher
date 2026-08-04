"""The composer, its fingerprint, and its refusal.

Every title here is invented -- `test_no_dataset_row_is_committed_anywhere`
scans this file.

**The cross-check that holds `compose_document` to `_FINGERPRINT_SQL` is not
in this file and cannot be.** The predicate's assembly is evaluated by
Postgres over a `titles` row, so the case that catches the two drifting apart
lives in `tests/integration/test_search_repository.py`. Everything here is
the half a pure function can answer on its own.
"""

import hashlib
from typing import Any

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.services.search import compose_document


def _title(**rest: Any) -> Title:
    """A synthetic enriched movie, with `rest` overriding any field."""
    fields: dict[str, Any] = {
        "kind": TitleKind.MOVIE,
        "name": "The Quiet Vacuum",
        "sort_name": "quiet vacuum, the",
        "year": 2019,
        "enrichment_state": EnrichmentState.ENRICHED,
    }
    fields.update(rest)
    return Title(**fields)


def test_the_fingerprint_is_the_md5_of_the_text_that_gets_embedded() -> None:
    """The milestone's central mechanism, asserted against the *string*
    rather than against itself.

    Fails: a fingerprint over anything but `document.text` -- `title.name`, a
    `model_dump()`, the text before a `strip()`, the parts before they were
    joined. All are stable and deterministic, and every one silently
    decouples "the vector is current" from "the text is current", so the
    stale predicate stops converging with nothing raising.

    Spelled as an independent `md5` of `document.text`, never as
    `compose(x).fingerprint == compose(x).fingerprint`, which any pure
    function satisfies.
    """
    document = compose_document(_title(overview="A caretaker inventories a house."))

    assert (
        document.fingerprint
        == hashlib.md5(document.text.encode("utf-8"), usedforsecurity=False).hexdigest()
    )


def test_two_titles_differing_only_in_overview_get_different_fingerprints() -> None:
    """The half that makes re-enrichment re-index. An implementation
    fingerprinting only identity fields passes the case above and fails this
    one -- and in production it means a title enriched from a skeleton keeps
    the skeleton's vector for good.
    """
    assert (
        compose_document(_title()).fingerprint
        != compose_document(_title(overview="Two sisters share one inherited grudge.")).fingerprint
    )


def test_a_whitespace_only_document_is_refused_and_still_carries_a_fingerprint() -> None:
    """The degenerate-document trap and the second trap it creates, together.

    Measured: every whitespace-only input embeds to the identical vector, cos
    = 1.0000 exactly, so a catalog of them is an unbounded cluster at the top
    of every similarity result -- and no assertion about norms, dimensions or
    determinism can see it.

    Two wrong implementations, the second worse. One embeds it anyway and
    ships the cluster. One *refuses by returning `None`*, leaving the caller
    nothing to write, so the title matches the stale predicate forever:
    re-claimed every pass, counted every scrape, permanently. This repository
    has shipped that shape once, one lane over.

    `name=" "` is reachable -- `Title.name` is `Field(min_length=1)`.
    """
    document = compose_document(_title(name=" ", sort_name=" ", year=None))

    assert document.is_degenerate is True
    assert document.fingerprint  # a refusal is writable, so it can stop matching
    assert not document.text.strip()


def test_a_refused_title_gets_a_new_fingerprint_the_moment_it_has_content() -> None:
    """Convergence, asserted rather than hoped for. An implementation
    fingerprinting every refusal to one constant (`md5("")`, a literal)
    satisfies the case above, writes a row, and then never re-claims the
    title however much enrichment gives it.
    """
    refused = compose_document(_title(name=" ", sort_name=" ", year=None))
    repaired = compose_document(_title(overview="A caretaker counts the rooms."))

    assert repaired.is_degenerate is False
    assert repaired.fingerprint != refused.fingerprint


def test_a_name_only_skeleton_is_not_degenerate() -> None:
    """The threshold is about *empty*, not *thin*, and this is what stops it
    drifting. Measured: unrelated name-only documents sit at 0.5867 pairwise
    and a skeleton retrieves its own enriched form at 0.7638 against a 0.4751
    cross-title mean.

    Fails: a minimum word count or minimum length -- the obvious
    "improvement" the first time someone reads the refusal. It also fails the
    laziest spelling of the unconditional assembly, `len(text) < 6`, since a
    name-only document is one word and five separators. In production either
    makes every thin title permanently absent from semantic results while the
    gauge reads zero stale.
    """
    document = compose_document(_title(name="Ledgerhand", sort_name="ledgerhand", year=None))

    assert document.is_degenerate is False


def test_the_document_is_deterministic_and_ordered_by_the_provider() -> None:
    """`genres` and `keywords` are tuples in a provider's order, not ours. An
    implementation iterating a `set` produces a different string -- and a
    different fingerprint -- per process, which is `PYTHONHASHSEED` making the
    backfill never drain. Same family as the `hash()` trap the fake embedder
    documents. Four elements, not two: a two-element `set` round-trips.
    """
    keywords = ("house", "ledger", "attic", "inventory")

    first = compose_document(_title(keywords=keywords))

    assert first.text == compose_document(_title(keywords=keywords)).text
    assert first.text.index("house") < first.text.index("attic")


def test_the_assembly_is_positional_so_a_missing_field_is_an_empty_segment() -> None:
    """**The property that makes the Python composer and `_FINGERPRINT_SQL`
    the same function.** The predicate is spelled with `coalesce(..., '')` on
    every nullable field and no conditionals at all, so it emits seven
    segments for every title in the catalog -- seven since M7 filled weight
    class B, and the seventh is `credit_names`, which is empty for the great
    majority of them and is an **empty segment** rather than an absent one.

    Fails: the obvious composer, which appends a section only when the field
    is populated. That one reads better, embeds slightly cleaner text, and
    produces a fingerprint the SQL predicate can never reproduce -- so every
    enriched title matches the stale predicate forever, the backfill re-claims
    the whole tier every pass, and nothing raises. The integration cross-check
    is what proves the agreement; this case is what names the reason, in the
    file a reader of the composer opens first.

    Asserted on the separator count rather than on the literal string, so it
    still has teeth if a field's *content* changes.
    """
    full = compose_document(
        _title(
            original_name="Das Stille Vakuum",
            overview="A caretaker inventories a house.",
            tagline="Nothing is missing.",
            genres=("drama", "mystery"),
            keywords=("house", "ledger"),
        )
    )
    sparse = compose_document(_title(name="Ledgerhand", sort_name="ledgerhand", year=None))

    assert full.text.count("\n") == 6
    assert sparse.text.count("\n") == 6
    assert sparse.text == "Ledgerhand\n\n\n\n\n\n"

    # The credits segment specifically, because it is the one that arrived as
    # a *conditional* append and the count above cannot tell which of the
    # seven is missing. An empty `credits` and an absent one must be the same
    # string.
    assert (
        compose_document(
            _title(name="Ledgerhand", sort_name="ledgerhand", year=None), credits=()
        ).text
        == sparse.text
    )


def test_the_year_is_not_in_the_document_because_the_predicate_has_no_year() -> None:
    """A deliberate omission, and the only one that costs anything.

    A release year is genuinely useful text to embed, and it is left out
    because `usher.db.repositories.search._FINGERPRINT_SQL` -- which the
    backfill's cursor, the stale gauge and the enqueue-closure test all
    evaluate -- assembles six columns and `year` is not among them. Adding it
    to one side only is failure mode (a) of the whole scheme: the fingerprint
    stops being a statement about the vector and the predicate stops
    converging.

    So this case is a tripwire on a *future* edit rather than a claim that
    the omission is ideal. Adding `year` means adding it to both, in one
    commit, and re-embedding the enriched tier -- which the fingerprint
    scheme does by itself, in 25 s to 2 minutes.
    """
    assert "2019" not in compose_document(_title(year=2019)).text
    assert compose_document(_title(year=2019)).text == compose_document(_title(year=1999)).text


def test_array_fields_join_on_a_single_space_as_usher_array_text_does() -> None:
    """`usher_array_text(text[])` is `array_to_string($1, ' ')`, and it is the
    same wrapper the generated column uses -- one definition of "an array as
    text" in this schema rather than two.

    Fails: `", ".join(...)`, which is the natural Python spelling and which
    produces a fingerprint the SQL predicate cannot reproduce for any title
    carrying two genres. On the measured catalog that is most of the enriched
    tier, so the backfill would re-claim it forever.
    """
    document = compose_document(_title(genres=("science fiction", "thriller")))

    assert "science fiction thriller" in document.text
    assert "science fiction, thriller" not in document.text


def test_the_credits_segment_sits_at_position_three_and_not_at_the_end() -> None:
    """`test_credits_are_accepted_and_are_empty_in_m6` was deleted here, and
    this replaced it.

    That case existed *"to make visible"* the obligation that M7 move both
    spellings in one commit or neither, and the thing it was making visible
    has now happened -- so leaving it asserting `credits == ()` would pin the
    milestone shut. What survives it is the ordering, which the tripwire never
    covered.

    **Position three, matching the generated column's concatenation order**,
    so all three spellings of this document read in the same sequence. The
    wrong implementation is the M6 shim's `text + _SECTION + _ITEM.join(...)`
    at the end: it produces a *different string* from `_FINGERPRINT_SQL`'s for
    every credited title, which is a fingerprint the predicate cannot
    reproduce and therefore a backfill that never drains -- and both
    spellings contain the same words, so an assertion on membership passes
    against it.
    """
    document = compose_document(
        _title(original_name="Das Stille Vakuum", overview="A caretaker inventories a house."),
        credits=("Marlow Vance", "Iris Kemp"),
    )
    segments = document.text.split("\n")

    assert len(segments) == 7
    assert segments[2] == "Marlow Vance Iris Kemp", (
        "position three, joined by usher_array_text's separator"
    )
    assert segments[3] == "A caretaker inventories a house.", "the overview follows it"


def test_a_credit_moves_the_fingerprint() -> None:
    """ADR-0020's scheme, at the field M7 added. A title that gains a cast
    gains a different document, so it is re-embedded exactly once -- and a
    composer that accepted `credits` and ignored them would leave weight class
    B populated in the tsvector while the vector was computed without it, with
    nothing to say so."""
    assert (
        compose_document(_title(), credits=("Marlow Vance",)).fingerprint
        != compose_document(_title()).fingerprint
    )
