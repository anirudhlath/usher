"""Behaviour every `RawPayloadStore` implementation must satisfy."""

import uuid
from typing import Any

from usher.ports.repository import CachedPayload, RawPayloadStore

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
        """TMDb's `append_to_response` payloads are deeply nested and carry JSON nulls.

        A store that flattened or dropped them would make re-derivation from the cache
        impossible, and would do it invisibly.
        """
        await store.put("tmdb", "movie", "90000550", PAYLOAD)
        found = await store.get("tmdb", "movie", "90000550")
        assert found is not None
        assert found[0]["belongs_to_collection"] is None
        assert found[0]["genres"] == [{"id": 18, "name": "Drama"}]

    async def test_get_returns_none_for_an_unknown_reference(self, store: RawPayloadStore) -> None:
        assert await store.get("tmdb", "movie", "999999") is None

    async def test_the_key_is_the_whole_triple(self, store: RawPayloadStore) -> None:
        """One TMDb id can be a movie *and* a series, and IMDb ids look nothing like TMDb's.

        A store keyed on the reference alone hands a series' payload to a movie's
        enrichment.
        """
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
        """A refresh moves `fetched_at`, which is the column's whole purpose.

        `RawPayloadRow`'s `server_default` covers the INSERT arm only, so an upsert
        that leaves `fetched_at` out of its `DO UPDATE SET` reports a stale cache date
        for a payload fetched this morning, and the compliance panel reads a breach
        that is not real or hides one that is.
        """
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
        """The minimum is what the cache ceiling is judged against.

        `max` here would report perfect compliance right up to the moment it is audited.
        """
        await store.put("tmdb", "movie", "1", {"v": 1})
        await store.put("tmdb", "movie", "2", {"v": 2})
        first = await store.get("tmdb", "movie", "1")
        assert first is not None
        assert await store.oldest_fetched_at("tmdb") == first[1]

    async def test_oldest_fetched_at_is_scoped_by_provider(self, store: RawPayloadStore) -> None:
        """Each provider has its own caching term.

        One number over all of them answers no provider's question.
        """
        await store.put("tmdb", "movie", "1", {"v": 1})
        assert await store.oldest_fetched_at("tvdb") is None

    async def test_iterate_visits_every_cached_payload_exactly_once_across_pages(
        self, store: RawPayloadStore
    ) -> None:
        """Rules out the pager that *loses rows across a page boundary*."""
        for index in range(7):
            await store.put("tmdb", "movie", f"9000055{index}", {"n": index})

        seen: list[uuid.UUID] = []
        after: uuid.UUID | None = None
        calls = 0
        while calls < 12:
            calls += 1
            page = await store.iterate("tmdb", limit=2, after=after)
            if not page:
                break
            assert len(page) <= 2, "a page must respect its limit"
            seen.extend(row.id for row in page)
            after = page[-1].id
        assert calls < 12, (
            "the cursor is not advancing -- seven rows at a page of two is five calls"
        )

        assert len(seen) == 7, "every row exactly once -- not six, and not eight"
        assert len(set(seen)) == 7

    async def test_iterate_terminates_rather_than_returning_the_same_page_forever(
        self, store: RawPayloadStore
    ) -> None:
        """Rules out the pager that *repeats the same page forever*.

        An `iterate` that ignores `after` entirely -- or that applies it to a column
        it does not order by -- returns page one on every call, and nothing raises:
        `DeriveService`'s loop never exits and the only symptom is a busy worker. The
        bound is the assertion -- two rows at a page of one drains in at most three
        calls, and more than that is a cursor that is not advancing.
        """
        await store.put("tmdb", "movie", "90000550", {"n": 0})
        await store.put("tmdb", "movie", "90000551", {"n": 1})

        after: uuid.UUID | None = None
        calls = 0
        while calls < 10:
            calls += 1
            page = await store.iterate("tmdb", limit=1, after=after)
            if not page:
                break
            after = page[-1].id
        assert calls == 3, "two pages of one, then the empty page that says drained"

    async def test_iterate_stays_scoped_to_one_provider_on_every_page_not_only_the_first(
        self, store: RawPayloadStore
    ) -> None:
        """A misplaced parenthesis scopes only the first page to the provider.

        Invisible to a cursor case whose rows all match the filter anyway.
        """
        await store.put("tmdb", "movie", "90000550", {"provider": "tmdb"})
        await store.put("tmdb", "movie", "90000551", {"provider": "tmdb"})
        # The tconst is in the reserved `tt99`/`nm99` band, not in TMDb's >= 90,000,000
        # one -- the two synthetic-data rules are different and
        # `test_every_imdb_id_is_in_the_reserved_synthetic_band` enforces this one.
        await store.put("imdb", "global", "tt99000550", {"provider": "imdb"})

        seen: list[CachedPayload] = []
        after: uuid.UUID | None = None
        calls = 0
        while calls < 8:
            calls += 1
            page = await store.iterate("tmdb", limit=1, after=after)
            if not page:
                break
            seen.extend(page)
            after = page[-1].id
        assert calls < 8, "the cursor is not advancing -- two rows at a page of one is three calls"

        assert len(seen) == 2
        assert all(row.payload["provider"] == "tmdb" for row in seen)

    async def test_iterate_carries_the_kind_that_distinguishes_two_id_spaces(
        self, store: RawPayloadStore
    ) -> None:
        """The row carries `kind`, without which the caller cannot rejoin correctly.

        `raw_payloads` has no `title_id`, so the caller joins back on `(provider,
        kind, reference)`, and TMDb keys movies and series in separate id spaces that
        overlap. A row shape returning `reference` without `kind` -- the obvious
        `list[tuple[uuid, dict]]` -- makes it *unspellable* for the caller to get this
        right, and the failure is a series' cast attached to a film with the same
        integer.
        """
        await store.put("tmdb", "movie", "90000550", {"space": "movie"})
        await store.put("tmdb", "series", "90000550", {"space": "series"})

        rows = await store.iterate("tmdb", limit=10)

        assert {(row.kind, row.reference) for row in rows} == {
            ("movie", "90000550"),
            ("series", "90000550"),
        }
        assert {row.kind: row.payload["space"] for row in rows} == {
            "movie": "movie",
            "series": "series",
        }

    async def test_a_refreshed_payload_does_not_move_in_the_cursors_order(
        self, store: RawPayloadStore
    ) -> None:
        """A refreshed payload keeps its id, so it does not move in the cursor's order.

        `_PUT`'s `DO UPDATE SET` names `payload` and `fetched_at` and **not** `id`, so
        the freshly minted `new_id()` is discarded on the conflict arm. Replacing the
        whole stored tuple, id and all -- the natural dict-backed fake -- re-mints a
        UUIDv7 that sorts the row to the *end* of the walk, so an enrichment landing
        mid-derivation makes the walk revisit one row and skip another until the next
        full pass.
        """
        await store.put("tmdb", "movie", "90000550", {"v": 1})
        before = (await store.iterate("tmdb", limit=10))[0]

        await store.put("tmdb", "movie", "90000550", {"v": 2})
        after = (await store.iterate("tmdb", limit=10))[0]

        assert after.id == before.id, "a refresh replaces the payload, never the identity"
        assert after.payload == {"v": 2}
        assert after.fetched_at > before.fetched_at

    async def test_iterate_orders_by_the_primary_key_and_not_by_fetched_at(
        self, store: RawPayloadStore
    ) -> None:
        """Rules out `ORDER BY fetched_at`, which the rest of this suite lets through."""
        await store.put("tmdb", "movie", "90000550", {"v": 1})
        await store.put("tmdb", "movie", "90000551", {"v": 1})
        # The refresh is what separates the two orderings: this row keeps the
        # smaller id and takes the newer timestamp.
        await store.put("tmdb", "movie", "90000550", {"v": 2})

        seen: list[str] = []
        after: uuid.UUID | None = None
        calls = 0
        while calls < 8:
            calls += 1
            page = await store.iterate("tmdb", limit=1, after=after)
            if not page:
                break
            seen.extend(row.reference for row in page)
            after = page[-1].id
        assert calls < 8, "the cursor is not advancing"

        assert sorted(seen) == ["90000550", "90000551"], (
            "a refreshed row must keep its place in the walk -- ordering on "
            "fetched_at strands the row with the smaller id"
        )

    async def test_iterate_is_empty_for_a_provider_with_nothing_cached(
        self, store: RawPayloadStore
    ) -> None:
        """Every command has to work against an empty database, `usher derive` included.

        An implementation that raised or returned `None` here would make the empty
        deployment the loud case.
        """
        assert await store.iterate("tmdb", limit=10) == []
