"""`FakeMetadataProvider`'s recommendations seam.

The fake answers what a test seeds, so what is pinned here is the seam's own contract:
order kept, a count a test can read, `fail_with` honoured, and the same refusal split
`TmdbMetadataProvider` makes between an entity with no recommendations and none at all.
"""

import pytest

from tests.fakes.metadata_provider import FakeMetadataProvider
from usher.domain.enums import TitleKind
from usher.ports.errors import PortDataMalformed, PortUnavailable
from usher.ports.ingest import ProviderRef
from usher.ports.metadata import MetadataCandidate

_SEED = ProviderRef(provider="tmdb", value="90000001", kind=TitleKind.MOVIE)
# A ref the fake holds a payload for out of the box, so the entity exists.
_KNOWN = ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.MOVIE)
_UNKNOWN = ProviderRef(provider="tmdb", value="90000999", kind=TitleKind.MOVIE)


def _candidate(provider_id: int, name: str) -> MetadataCandidate:
    return MetadataCandidate(
        provider_id=provider_id, name=name, year=2001, kind=TitleKind.MOVIE, popularity=1.0
    )


async def test_seeded_recommendations_come_back_in_the_seeded_order() -> None:
    """A 3-cycle of ids and names, so neither sort order reproduces the seeded one."""
    seeded = [_candidate(90020003, "B"), _candidate(90020001, "C"), _candidate(90020002, "A")]
    assert [one.provider_id for one in seeded] != sorted(one.provider_id for one in seeded)
    assert [one.name for one in seeded] != sorted(one.name for one in seeded)
    fake = FakeMetadataProvider()
    fake.seed_recommendations(_SEED, seeded)
    assert await fake.recommendations(_SEED) == seeded


async def test_recommendations_are_seeded_per_ref() -> None:
    fake = FakeMetadataProvider()
    other = ProviderRef(provider="tmdb", value="90000003", kind=TitleKind.SERIES)
    fake.seed_recommendations(_SEED, [_candidate(90020001, "One")])
    fake.seed_recommendations(other, [_candidate(90020002, "Two")])
    assert [one.name for one in await fake.recommendations(_SEED)] == ["One"]
    assert [one.name for one in await fake.recommendations(other)] == ["Two"]


async def test_a_caller_cannot_change_what_was_seeded_through_what_it_got_back() -> None:
    fake = FakeMetadataProvider()
    fake.seed_recommendations(_SEED, [_candidate(90020001, "One")])
    (await fake.recommendations(_SEED)).clear()
    assert len(await fake.recommendations(_SEED)) == 1


async def test_calls_are_counted_and_reset_calls_clears_the_count() -> None:
    fake = FakeMetadataProvider()
    fake.seed_recommendations(_SEED, [_candidate(90020001, "One")])
    assert fake.recommendation_calls == 0
    await fake.recommendations(_SEED)
    await fake.recommendations(_SEED)
    assert fake.recommendation_calls == 2
    fake.reset_calls()
    assert fake.recommendation_calls == 0


async def test_a_known_entity_with_nothing_seeded_has_no_recommendations() -> None:
    """TMDb answers 200 with an empty page for an entity nobody recommends from."""
    assert await FakeMetadataProvider().recommendations(_KNOWN) == []


async def test_an_entity_the_provider_holds_nothing_for_is_malformed() -> None:
    """The refusal `fetch` makes, which TMDb makes as a 404, and it is still a counted call."""
    fake = FakeMetadataProvider()
    with pytest.raises(PortDataMalformed):
        await fake.recommendations(_UNKNOWN)
    assert fake.recommendation_calls == 1


async def test_an_empty_seed_is_an_entity_with_no_recommendations() -> None:
    """Seeding `[]` is how a test says the entity exists and nobody is recommended from it."""
    fake = FakeMetadataProvider()
    fake.seed_recommendations(_UNKNOWN, [])
    assert await fake.recommendations(_UNKNOWN) == []


async def test_fail_with_applies_to_recommendations_until_recover() -> None:
    """A failing provider raises before it counts, as `fetch` does."""
    fake = FakeMetadataProvider()
    fake.seed_recommendations(_SEED, [_candidate(90020001, "One")])
    fake.fail_with(PortUnavailable("down"))
    with pytest.raises(PortUnavailable):
        await fake.recommendations(_SEED)
    assert fake.recommendation_calls == 0
    fake.recover()
    assert len(await fake.recommendations(_SEED)) == 1
    assert fake.recommendation_calls == 1
