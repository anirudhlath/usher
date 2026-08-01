"""Behaviour every `RawPayloadStore` implementation must satisfy.

PRD 02's `raw_payloads`, narrowed by ADR-0016 to provider responses only.
Two properties carry real weight: the key is the whole triple, and
`fetched_at` moves when the payload does -- the second because it is the only
answer this system has to TMDb's <=6-month caching term, and a stale
timestamp on fresh data is a compliance answer that is wrong and silent.

Subclass and provide `store`.
"""

from typing import Any

from usher.ports.repository import RawPayloadStore

PAYLOAD: dict[str, Any] = {
    "id": 90000550,
    "title": "Fight Club",
    "genres": [{"id": 18, "name": "Drama"}],
    "belongs_to_collection": None,
    "vote_average": 8.4,
}


class RawPayloadStoreContract:
    async def test_a_payload_round_trips(self, store: RawPayloadStore) -> None:
        await store.put("tmdb", "movie", "90000550", PAYLOAD)
        found = await store.get("tmdb", "movie", "90000550")
        assert found is not None
        payload, _ = found
        assert payload == PAYLOAD, "stored verbatim -- the whole point is no second network call"

    async def test_a_payload_survives_nesting_and_nulls(self, store: RawPayloadStore) -> None:
        """TMDb's `append_to_response` payloads are deeply nested and carry
        JSON nulls. A store that flattened or dropped them would make the
        re-derivation M7 and M9 depend on impossible, and would do it
        invisibly until those milestones."""
        await store.put("tmdb", "movie", "90000550", PAYLOAD)
        found = await store.get("tmdb", "movie", "90000550")
        assert found is not None
        assert found[0]["belongs_to_collection"] is None
        assert found[0]["genres"] == [{"id": 18, "name": "Drama"}]

    async def test_get_returns_none_for_an_unknown_reference(self, store: RawPayloadStore) -> None:
        assert await store.get("tmdb", "movie", "999999") is None

    async def test_the_key_is_the_whole_triple(self, store: RawPayloadStore) -> None:
        """TMDb id 90000550 is a movie *and* a series (26,968 such collisions,
        measured), and IMDb ids look nothing like TMDb's. A store keyed on the
        reference alone hands a series' payload to a movie's enrichment."""
        await store.put("tmdb", "movie", "90000550", {"kind": "movie"})
        await store.put("tmdb", "series", "90000550", {"kind": "series"})
        await store.put("imdb", "movie", "90000550", {"kind": "imdb"})
        for kind_or_provider, expected in (
            (("tmdb", "movie"), "movie"),
            (("tmdb", "series"), "series"),
            (("imdb", "movie"), "imdb"),
        ):
            found = await store.get(kind_or_provider[0], kind_or_provider[1], "90000550")
            assert found is not None and found[0]["kind"] == expected

    async def test_a_second_put_replaces_the_payload(self, store: RawPayloadStore) -> None:
        await store.put("tmdb", "movie", "90000550", {"vote_average": 8.4})
        await store.put("tmdb", "movie", "90000550", {"vote_average": 8.5})
        found = await store.get("tmdb", "movie", "90000550")
        assert found is not None
        assert found[0] == {"vote_average": 8.5}

    async def test_a_refresh_moves_fetched_at(self, store: RawPayloadStore) -> None:
        """The column's whole purpose. `RawPayloadRow`'s `server_default`
        covers the INSERT arm only, so an upsert that leaves `fetched_at` out
        of its `DO UPDATE SET` reports a six-month-old cache date for a payload
        fetched this morning -- and PRD 10's dashboard-5 panel then shows a
        compliance breach that is not real, or hides one that is."""
        await store.put("tmdb", "movie", "90000550", {"v": 1})
        first = await store.get("tmdb", "movie", "90000550")
        await store.put("tmdb", "movie", "90000550", {"v": 2})
        second = await store.get("tmdb", "movie", "90000550")
        assert first is not None and second is not None
        assert second[1] > first[1], "a refreshed payload carries a refreshed timestamp"

    async def test_oldest_fetched_at_is_none_for_a_provider_with_nothing_cached(
        self, store: RawPayloadStore
    ) -> None:
        assert await store.oldest_fetched_at("tmdb") is None

    async def test_oldest_fetched_at_is_the_minimum(self, store: RawPayloadStore) -> None:
        """It asks for the oldest, and the oldest is what the 6-month ceiling
        is measured against. `max` here would report perfect compliance right
        up to the moment it is audited."""
        await store.put("tmdb", "movie", "1", {"v": 1})
        await store.put("tmdb", "movie", "2", {"v": 2})
        first = await store.get("tmdb", "movie", "1")
        assert first is not None
        assert await store.oldest_fetched_at("tmdb") == first[1]

    async def test_oldest_fetched_at_is_scoped_by_provider(self, store: RawPayloadStore) -> None:
        """Each provider has its own caching term. One number over all of them
        answers no provider's question."""
        await store.put("tmdb", "movie", "1", {"v": 1})
        assert await store.oldest_fetched_at("tvdb") is None
