"""The metadata port's settled shape."""

import inspect
import uuid
from typing import Any, get_type_hints

import pytest

from tests.fakes.metadata_provider import FakeMetadataProvider
from usher.domain.enums import TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.image import Image
from usher.domain.title import Title
from usher.ports.ingest import ProviderRef
from usher.ports.metadata import (
    ChangedPage,
    DerivationResult,
    EnrichmentResult,
    MetadataCandidate,
    MetadataProvider,
)


def _title() -> Title:
    return Title(kind=TitleKind.SERIES, name="Fixture", sort_name="Fixture")


def test_enrichment_returns_the_aggregate_not_just_a_title() -> None:
    """Enrichment returns the aggregate, because the stage populates a hierarchy.

    `seasons`/`episodes` are here because they are stored;
    `people`/`credits`/`images`/`collection` are deliberately absent because nothing
    stores those and a field nothing writes is a placeholder.
    """
    assert set(EnrichmentResult.__dataclass_fields__) == {
        "title",
        "seasons",
        "episodes",
        "payload",
    }


def test_the_verbatim_payload_travels_with_the_result() -> None:
    """What makes deferring `Person`/`Credit`/`Collection`/`Image` honest rather than lossy.

    The response that would have produced them travels with the result on its way to
    `raw_payloads`, which is that table's stated purpose: a later stage re-derives them
    with no second network call.

    Asserted by round-tripping a payload carrying a key nothing reads yet, not by
    inspecting the annotation: `__dataclass_fields__["payload"].type is not None` is
    true of *every* annotated field on every dataclass and would pass against a result
    that dropped the payload entirely.
    """
    payload: dict[str, Any] = {"id": 90000550, "credits": {"cast": [{"id": 1, "name": "Someone"}]}}
    result = EnrichmentResult(title=_title(), seasons=(), episodes=(), payload=payload)
    assert result.payload["credits"]["cast"][0]["name"] == "Someone"


def test_an_enrichment_result_carries_the_hierarchy_a_series_needs() -> None:
    """Nearly everything in a television-heavy source is an episode.

    A result that could not carry seasons and episodes would leave the pipeline unable
    to enrich the bulk of what it holds.
    """
    title = _title()
    season = Season(title_id=title.id, season_number=1)
    episode = Episode(
        title_id=title.id, season_id=season.id, season_number=1, episode_number=1, name="Pilot"
    )
    result = EnrichmentResult(
        title=title, seasons=(season,), episodes=(episode,), payload={"id": 90001399}
    )
    assert result.seasons == (season,)
    assert result.episodes == (episode,)


def test_an_enrichment_result_is_frozen() -> None:
    """A mutable aggregate could be edited between `EnrichService` and `raw_payloads`.

    The cached payload would then stop being what the provider actually answered.
    """
    result = EnrichmentResult(title=_title(), seasons=(), episodes=(), payload={})
    with pytest.raises((AttributeError, TypeError)):
        result.title = _title()  # type: ignore[misc]  # verifying the runtime rejection


def test_fetch_takes_a_provider_ref_not_a_bare_integer() -> None:
    """`provider_id: int` baked in TMDb's id scheme.

    IMDb's `tt99000100` does not fit it, and PRD 01 lists additional metadata providers
    as an open extension seam.

    The annotation is compared to the *class*, not to the string `"ProviderRef"`: this
    module does not use `from __future__ import annotations`, so `inspect.signature`
    hands back the resolved object and a string comparison passes for no implementation
    at all.
    """
    signature = inspect.signature(MetadataProvider.fetch)
    assert list(signature.parameters) == ["self", "ref"]
    assert signature.parameters["ref"].annotation is ProviderRef


def test_fetch_no_longer_takes_a_separate_kind() -> None:
    """`ProviderRef` already carries the kind, and it carries it as `TitleKind | None`.

    `None` for a global namespace like IMDb's. A second `kind` argument would make "a
    TMDb ref with no kind" and "an IMDb ref with a kind" both spellable.
    """
    assert "kind" not in inspect.signature(MetadataProvider.fetch).parameters


def test_to_result_replaced_to_title() -> None:
    """The method that returned only a `Title` is gone rather than kept alongside.

    Two normalisation entry points, where one populates the hierarchy and the other
    silently does not, is the same defect restated as an API.
    """
    assert not hasattr(MetadataProvider, "to_title")
    assert hasattr(MetadataProvider, "to_result")


def test_changed_since_is_resumable() -> None:
    """`days: int` in and `list[int]` out cannot express a cursor.

    TMDb's changes feed is paginated and 14-day-capped, so a partial run had no way to
    pick up where it stopped.
    """
    signature = inspect.signature(MetadataProvider.changed_since)
    assert list(signature.parameters) == ["self", "since", "cursor"]
    assert set(ChangedPage.__dataclass_fields__) == {"refs", "next_cursor"}


def test_a_changed_page_carries_refs_not_bare_integers() -> None:
    """Same reason `fetch` takes one.

    A page of bare ids the caller then has to pair with a kind leaves the collision
    between TMDb's movie and series spaces waiting for a caller to make it.
    """
    hints = get_type_hints(ChangedPage)
    assert hints["refs"] == tuple[ProviderRef, ...]
    assert hints["next_cursor"] == (str | None)


def test_the_end_of_a_change_feed_is_a_null_cursor() -> None:
    page = ChangedPage(refs=(), next_cursor=None)
    assert page.next_cursor is None


def test_search_can_be_scoped_to_one_kind() -> None:
    """TMDb keys movies and series in separate spaces, with a search endpoint each.

    A caller that knows which one it wants -- the match stage always does, from
    `SourceItem.kind` -- would otherwise pay two upstream requests and then discard half
    the answers, on the only match tier in PRD 03 that spends rate-limited TMDb
    requests at all.

    Optional, so a provider with a single search space ignores it.
    """
    signature = inspect.signature(MetadataProvider.search)
    assert list(signature.parameters) == ["self", "name", "year", "kind"]
    assert signature.parameters["kind"].default is None


def test_a_search_candidate_still_speaks_the_canonical_vocabulary() -> None:
    """`MetadataCandidate` is unchanged, and deliberately so.

    Its `provider_id` plus `kind` plus the provider's own `name` is losslessly a
    `ProviderRef`, so a `search()` returning `list[dict[str, Any]]` -- which makes the
    match stage index into TMDb's movie/TV divergence -- stays ruled out.
    """
    candidate = MetadataCandidate(
        provider_id=90001399,
        name="A Synthetic Series",
        year=2011,
        kind=TitleKind.SERIES,
        popularity=1.0,
    )
    ref = ProviderRef(provider="tmdb", value=str(candidate.provider_id), kind=candidate.kind)
    assert ref == ProviderRef(provider="tmdb", value="90001399", kind=TitleKind.SERIES)


def test_to_result_takes_the_title_id_it_must_not_invent() -> None:
    """Identity is Usher's own UUIDv7.

    A provider that minted one would create a second canonical row for a title the
    catalog already holds, on every re-enrichment.
    """
    signature = inspect.signature(MetadataProvider.to_result)
    assert list(signature.parameters) == ["self", "payload", "title_id"]
    assert signature.parameters["title_id"].annotation is uuid.UUID


def test_a_derivation_carries_the_fourth_entity_and_a_provider_cannot_forget_it() -> None:
    """`raw_payloads` is kept so all four entities can be re-derived from it.

    **The field has no default, deliberately.** `DerivationResult`'s other three have
    none either, so a second `MetadataProvider` cannot construct one that silently
    carries no artwork -- the failure mode of a default would be a provider whose titles
    quietly have no posters, with every count in `usher derive`'s report still reading
    correctly.
    """
    hints = get_type_hints(DerivationResult)
    assert hints["images"] == tuple[Image, ...]

    with pytest.raises(TypeError, match="images"):
        DerivationResult(  # type: ignore[call-arg]  # the point of the case
            people=(), credits=(), collection=None
        )


def test_to_derivation_is_synchronous_and_pure_for_all_four_entities() -> None:
    """Derivation is synchronous and pure, and `images` is where that is least obvious.

    Artwork is the one of the four whose *bytes* really do need a request, which is `GET
    /images/{id}`'s job and not this one's.

    Asserted on the signature rather than on the prose: `async def` is how a provider
    that wanted to fetch would have to spell it.
    """
    assert not inspect.iscoroutinefunction(MetadataProvider.to_derivation)
    signature = inspect.signature(MetadataProvider.to_derivation)
    assert list(signature.parameters) == ["self", "payload", "title_id"]
    assert signature.return_annotation is DerivationResult


def test_a_complete_metadata_provider_implementation_instantiates() -> None:
    """The port is an ABC so the shape is checked at construction.

    An implementation missing a method fails there, not at the call site five layers
    into a walk.
    """
    assert isinstance(FakeMetadataProvider(), MetadataProvider)


def test_a_provider_that_still_implements_the_old_shape_is_incomplete() -> None:
    """The settled signatures are enforced by the ABC rather than by review.

    A provider written against the old ones no longer satisfies the port.
    """

    class Stale(MetadataProvider):
        @property
        def name(self) -> str:
            return "stale"

        async def search(  # type: ignore[override]  # deliberately the old signature
            self, name: str, year: int | None
        ) -> list[MetadataCandidate]:
            return []

        async def fetch(  # type: ignore[override]  # deliberately the old signature
            self, provider_id: int, kind: TitleKind
        ) -> dict[str, Any]:
            return {}

        def to_title(self, payload: dict[str, Any], title_id: uuid.UUID) -> Title:
            return _title()

        async def changed_since(  # type: ignore[override]  # deliberately the old signature
            self, days: int
        ) -> list[int]:
            return []

    with pytest.raises(TypeError, match="to_result"):
        Stale()  # type: ignore[abstract]  # verifying the runtime rejection ABC enforces


def test_there_are_no_remaining_provisional_markers() -> None:
    """A 🔶 that outlives what it promised to settle is worse than none at all.

    It reads as settled to anyone who checks the roadmap rather than the source.
    """
    import usher.ports.metadata as module

    source = inspect.getsource(module)
    assert "🔶" not in source
    assert "Settle in M4" not in source
