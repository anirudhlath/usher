"""The metadata port's settled shape.

Every 🔶 marker in `usher/ports/metadata.py` that named M4 has an assertion
here, and each is written so reverting the corresponding production line
fails it. ADR-0017 is the reasoning; this file is the enforcement.
"""

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
    """The 🔶: `to_title` returned a `Title` while the stage populates a
    hierarchy. `seasons`/`episodes` are here because M4 stores them;
    `people`/`credits`/`images`/`collection` are deliberately absent because
    nothing in M4 stores those and a field nothing writes is a placeholder."""
    assert set(EnrichmentResult.__dataclass_fields__) == {
        "title",
        "seasons",
        "episodes",
        "payload",
    }


def test_the_verbatim_payload_travels_with_the_result() -> None:
    """What makes deferring `Person`/`Credit`/`Collection`/`Image` honest
    rather than lossy: the response that would have produced them travels
    with the result on its way to `raw_payloads`, so M7 and M9 re-derive them
    with no second network call. PRD 02's whole stated purpose for that
    table.

    Asserted by round-tripping a payload carrying a key nothing in M4 reads,
    not by inspecting the annotation: `__dataclass_fields__["payload"].type
    is not None` (the shape an earlier draft of this test used) is true of
    *every* annotated field on every dataclass and would pass against a
    result that dropped the payload entirely.
    """
    payload: dict[str, Any] = {"id": 90000550, "credits": {"cast": [{"id": 1, "name": "Someone"}]}}
    result = EnrichmentResult(title=_title(), seasons=(), episodes=(), payload=payload)
    assert result.payload["credits"]["cast"][0]["name"] == "Someone"


def test_an_enrichment_result_carries_the_hierarchy_a_series_needs() -> None:
    """999,827 of the measured source's 1,126,674 items are episodes, so a
    result that could not carry seasons and episodes would leave the pipeline
    unable to enrich 89% of what it holds."""
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
    """A mutable aggregate handed to `EnrichService` and then written to
    `raw_payloads` could be edited between the two, so the cached payload
    would stop being what the provider actually answered."""
    result = EnrichmentResult(title=_title(), seasons=(), episodes=(), payload={})
    with pytest.raises((AttributeError, TypeError)):
        result.title = _title()  # type: ignore[misc]  # verifying the runtime rejection


def test_fetch_takes_a_provider_ref_not_a_bare_integer() -> None:
    """The 🔶: `provider_id: int` baked in TMDb's id scheme, which IMDb's
    `tt99000100` does not fit -- and PRD 01 lists additional metadata
    providers as an open extension seam.

    The annotation is compared to the *class*, not to the string
    `"ProviderRef"`: this module does not use `from __future__ import
    annotations`, so `inspect.signature` hands back the resolved object and a
    string comparison passes for no implementation at all.
    """
    signature = inspect.signature(MetadataProvider.fetch)
    assert list(signature.parameters) == ["self", "ref"]
    assert signature.parameters["ref"].annotation is ProviderRef


def test_fetch_no_longer_takes_a_separate_kind() -> None:
    """`ProviderRef` already carries the kind, and it carries it as
    `TitleKind | None` -- `None` for a global namespace like IMDb's. A second
    `kind` argument would make "a TMDb ref with no kind" and "an IMDb ref
    with a kind" both spellable, and ADR-0011 is what happens when the first
    one is."""
    assert "kind" not in inspect.signature(MetadataProvider.fetch).parameters


def test_to_result_replaced_to_title() -> None:
    """The method that returned only a `Title` is gone rather than kept
    alongside: two normalisation entry points where one populates the
    hierarchy and the other silently does not is the 🔶 restated as an API."""
    assert not hasattr(MetadataProvider, "to_title")
    assert hasattr(MetadataProvider, "to_result")


def test_changed_since_is_resumable() -> None:
    """The 🔶: `days: int` in, `list[int]` out cannot express a cursor
    through TMDb's paginated, 14-day-capped changes feed, so a partial run
    had no way to pick up where it stopped."""
    signature = inspect.signature(MetadataProvider.changed_since)
    assert list(signature.parameters) == ["self", "since", "cursor"]
    assert set(ChangedPage.__dataclass_fields__) == {"refs", "next_cursor"}


def test_a_changed_page_carries_refs_not_bare_integers() -> None:
    """Same reason `fetch` takes one: a page of ids the caller then has to
    pair with a kind is ADR-0011's failure waiting for a caller to make it.
    26,968 TMDb ids are live in both the movie and the series space."""
    hints = get_type_hints(ChangedPage)
    assert hints["refs"] == tuple[ProviderRef, ...]
    assert hints["next_cursor"] == (str | None)


def test_the_end_of_a_change_feed_is_a_null_cursor() -> None:
    page = ChangedPage(refs=(), next_cursor=None)
    assert page.next_cursor is None


def test_search_can_be_scoped_to_one_kind() -> None:
    """TMDb keys movies and series in separate spaces and searches them
    through separate endpoints. A caller that knows which one it wants --
    the match stage always does, from `SourceItem.kind` -- would otherwise
    pay two upstream requests and then discard half the answers, on the one
    tier PRD 03 already calls "a last resort" for rate-limit reasons.

    Optional, so a provider with a single search space ignores it.
    """
    signature = inspect.signature(MetadataProvider.search)
    assert list(signature.parameters) == ["self", "name", "year", "kind"]
    assert signature.parameters["kind"].default is None


def test_a_search_candidate_still_speaks_the_canonical_vocabulary() -> None:
    """`MetadataCandidate` is unchanged and deliberately so (ADR-0017): its
    `provider_id` plus `kind` plus the provider's own `name` is losslessly a
    `ProviderRef`, and the M1 bug it replaced -- `search()` returning
    `list[dict[str, Any]]`, which made the match stage index into TMDb's
    movie/TV divergence -- stays fixed either way."""
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
    """Identity is Usher's own UUIDv7 (ADR-0003). A provider that minted one
    would create a second canonical row for a title the catalog already
    holds, on every re-enrichment."""
    signature = inspect.signature(MetadataProvider.to_result)
    assert list(signature.parameters) == ["self", "payload", "title_id"]
    assert signature.parameters["title_id"].annotation is uuid.UUID


def test_a_derivation_carries_the_fourth_entity_and_a_provider_cannot_forget_it() -> None:
    """ADR-0016 kept `raw_payloads` so four entities could be re-derived from
    it; M7 cashed three and `images` is the fourth.

    **The field has no default, deliberately**, which is what this case is
    really about. `DerivationResult`'s other three have none either, so a
    second `MetadataProvider` cannot construct one that silently carries no
    artwork -- and the failure mode of a default would be a provider whose
    titles quietly have no posters, with every count in `usher derive`'s
    report still reading correctly.
    """
    hints = get_type_hints(DerivationResult)
    assert hints["images"] == tuple[Image, ...]

    with pytest.raises(TypeError, match="images"):
        DerivationResult(  # type: ignore[call-arg]  # the point of the case
            people=(), credits=(), collection=None
        )


def test_to_derivation_is_synchronous_and_pure_for_all_four_entities() -> None:
    """The clause the whole stage rests on, and `images` is the field where a
    reader would most expect it to be broken -- artwork is the one of the four
    whose *bytes* really do need a request, which is `GET /images/{id}`'s job
    and not this one's.

    Asserted on the signature rather than on the prose: `async def` is how a
    provider that wanted to fetch would have to spell it.
    """
    assert not inspect.iscoroutinefunction(MetadataProvider.to_derivation)
    signature = inspect.signature(MetadataProvider.to_derivation)
    assert list(signature.parameters) == ["self", "payload", "title_id"]
    assert signature.return_annotation is DerivationResult


def test_a_complete_metadata_provider_implementation_instantiates() -> None:
    """The ABC-shape check ADR-0001 exists for: an implementation missing a
    method fails at construction, not at the call site five layers into a
    walk."""
    assert isinstance(FakeMetadataProvider(), MetadataProvider)


def test_a_provider_that_still_implements_the_old_shape_is_incomplete() -> None:
    """The settling is enforced by the ABC rather than by review: a provider
    written against the pre-M4 signatures no longer satisfies the port."""

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
    """A 🔶 that says "settle in M4" and survives M4 is worse than one that
    names a later milestone -- it reads as settled to anyone who checks the
    roadmap rather than the source. All three of this module's markers named
    M4; none of them may survive it."""
    import usher.ports.metadata as module

    source = inspect.getsource(module)
    assert "🔶" not in source
    assert "Settle in M4" not in source
