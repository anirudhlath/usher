"""Enum-level behavior that individual model tests shouldn't have to
re-derive."""

from usher.domain.enums import ENRICHMENT_RANK, EnrichmentState


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
