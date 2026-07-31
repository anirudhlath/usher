"""Enum-level behavior that individual model tests shouldn't have to
re-derive."""

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState, MatchMethod


def test_enrichment_rank_orders_the_ladder() -> None:
    assert (
        ENRICHMENT_RANK[EnrichmentState.SKELETON]
        < ENRICHMENT_RANK[EnrichmentState.STUB]
        < ENRICHMENT_RANK[EnrichmentState.ENRICHED]
    )


def test_enrichment_state_str_ordering_is_not_the_ladder() -> None:
    """Pin the footgun ADR-0008 records: StrEnum compares lexicographically
    ("enriched" < "skeleton"), not by ladder position. A "don't downgrade"
    guard written as `new_state > old_state` on the enum members themselves
    type-checks, runs, and silently computes the wrong answer.
    ENRICHMENT_RANK is what closes that gap."""
    assert not (EnrichmentState.ENRICHED > EnrichmentState.SKELETON)
    assert EnrichmentState.ENRICHED < EnrichmentState.SKELETON


def test_failed_is_not_a_tier() -> None:
    """FAILED was removed from the ladder by ADR-0008 — failure is tracked
    on Title.enrichment_error instead, orthogonal to the enrichment tier."""
    assert not hasattr(EnrichmentState, "FAILED")
    assert {s.value for s in EnrichmentState} == {"skeleton", "stub", "enriched"}


def test_match_method_names_every_tier_including_failure() -> None:
    """PRD 10's `usher.match.result` counter is labelled `method` and
    `confident`; a vocabulary missing `UNMATCHED` would make the review
    queue's depth invisible to the metric that is supposed to report it."""
    assert set(MatchMethod) == {
        MatchMethod.TMDB_ID,
        MatchMethod.IMDB_ID,
        MatchMethod.TVDB_ID,
        MatchMethod.NAME_YEAR,
        MatchMethod.PROVIDER_SEARCH,
        MatchMethod.CREATED_STUB,
        MatchMethod.UNMATCHED,
    }
    # Not `MatchMethod.TMDB_ID == "tmdb_id"`: true at runtime, but mypy
    # strict rejects it as a non-overlapping equality between
    # Literal[MatchMethod.TMDB_ID] and Literal['tmdb_id']. The f-string form
    # pins the same property and is what a metric label actually does with
    # the member.
    assert f"{MatchMethod.TMDB_ID}" == "tmdb_id"


def test_match_method_values_are_the_wire_identifiers() -> None:
    """`enums.py`'s own docstring: values are stable wire and storage
    identifiers. These reach a metric label, so a member whose value drifted
    from its spelling would split one counter series into two with no error
    anywhere."""
    assert {m.value for m in MatchMethod} == {
        "tmdb_id",
        "imdb_id",
        "tvdb_id",
        "name_year",
        "provider_search",
        "created_stub",
        "unmatched",
    }
