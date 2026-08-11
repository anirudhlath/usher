"""Behaviour every `ImageRepository` implementation must satisfy.

**The one thing this port exists to make impossible is id churn.** An image has
no provider integer id, so `(the one owner, provider, provider_path)` is its
natural key, and a re-derivation that does not recognise it mints a fresh
UUIDv7 per sighting — which invalidates every client's cached artwork reference
and makes
[ADR-0032](../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)'s
`Cache-Control: immutable` a lie the first time a title is re-derived. The
headline case below is the one that catches a delete-then-insert
implementation, and it asserts its own premise first, because *"the id did not
change"* is also what a second call that never ran produces.

**Every case names the wrong implementation it rules out.**

Subclass and provide `repository` and `seeder`. The seeder writes the one thing
this port cannot — a `titles` row for the foreign key to point at — and its
`ABC` shape is ADR-0001's argument applied to a test double: a `Protocol` would
let a subclass drift out of the suite silently.

**Every ordering case here turns on `is_primary`, and that is a consequence of
`m09c` carrying no `sort_order`.** The order is `(is_primary DESC, id)`, so
there is exactly one key a re-derivation can move and one tiebreak it cannot.
Cases that would have exercised a middle key are not written as if they pass;
`ImageRepository`'s docstring states the limit.
"""

import uuid
from abc import ABC, abstractmethod

from usher.domain.enums import ImageKind
from usher.domain.image import Image
from usher.ports.repository import ImageRepository


def image(title_id: uuid.UUID, path: str, **changes: object) -> Image:
    """One title-owned poster, with everything but the path defaulted.

    Title-owned because that is the only owner M9 writes: the group's boundary
    call puts episode stills and person headshots outside this milestone, and a
    contract suite that seeded them would be asserting behaviour no caller can
    reach through this port. The *key* covers all three owners; the *methods*
    cover the one with a writer.
    """
    fields: dict[str, object] = {
        "title_id": title_id,
        "kind": ImageKind.POSTER,
        "provider": "tmdb",
        "provider_path": path,
        "is_primary": False,
    }
    fields.update(changes)
    return Image.model_validate(fields)


class ImageSeeder(ABC):
    """A `titles` row, which is the only thing `ImageRepository` cannot write
    and every case needs."""

    @abstractmethod
    async def title(self) -> uuid.UUID:
        """A title, returning its id."""


class ImageRepositoryContract:
    async def test_a_second_replace_keeps_the_id_of_a_path_that_did_not_change(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """**The case this port exists for**, and the wrong implementation it
        kills is the obvious one: delete every image for the title, then insert
        the incoming set. That answers correctly on every read and mints a new
        id per pass, so a client's cached `/images/{id}` is invalidated on
        every `usher derive` and `Cache-Control: immutable` becomes a promise
        the catalog breaks nightly.

        **Two premises, and both are load-bearing.** The second derivation must
        actually change something, or an implementation that recognised the
        batch and returned early would pass — `is_primary` is the field moved
        because with no `sort_order` column it is the only ordering key a
        re-derivation can move, so it is the ordinary event rather than a
        contrived one. And the incoming row must carry a *different* id from
        the stored one, or "the id did not change" would be the caller's doing
        rather than the port's.
        """
        title_id = await seeder.title()
        first = image(title_id, "/an-invented-path.jpg", is_primary=False)
        await repository.replace_for_titles([title_id], [first])
        stored_first = (await repository.list_for_title(title_id))[0]

        second = image(title_id, "/an-invented-path.jpg", is_primary=True)
        assert second.is_primary != first.is_primary, (
            "the premise: the second derivation must change something, or an "
            "implementation that skipped the write would pass this case"
        )
        assert second.id != stored_first.id, (
            "the premise: the derivation mints a fresh UUIDv7 per sighting, so "
            "a stable id is the port's doing and not the caller's"
        )
        await repository.replace_for_titles([title_id], [second])

        stored_second = (await repository.list_for_title(title_id))[0]
        assert stored_second.id == stored_first.id
        assert stored_second.is_primary is True

    async def test_a_second_replace_refreshes_every_field_the_provider_moved(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The mirror of the case above, and the wrong implementation is
        `ON CONFLICT ... DO NOTHING` — or a `DO UPDATE` whose `SET` list has
        drifted short of the column list.

        An id that survives is worth nothing if the row it names is frozen at
        whatever the first derivation happened to see: the artwork would be
        stable *and* stale, which is the harder of the two failures to notice
        because every read succeeds. Every mutable column is moved at once and
        every one is asserted, so a `SET` list missing one name fails here
        rather than in whichever milestone first reads that column.

        `language` moves to `None`, deliberately: an assignment written as
        `COALESCE(excluded.language, images.language)` — the defensive-looking
        spelling — passes every other assertion in this case and makes a
        language a provider *removed* unremovable.
        """
        title_id = await seeder.title()
        await repository.replace_for_titles(
            [title_id],
            [
                image(
                    title_id,
                    "/an-invented-path.jpg",
                    kind=ImageKind.POSTER,
                    width=500,
                    height=750,
                    language="en",
                    is_primary=False,
                )
            ],
        )
        await repository.replace_for_titles(
            [title_id],
            [
                image(
                    title_id,
                    "/an-invented-path.jpg",
                    kind=ImageKind.BACKDROP,
                    width=1280,
                    height=720,
                    language=None,
                    is_primary=True,
                )
            ],
        )

        stored = (await repository.list_for_title(title_id))[0]
        assert stored.kind is ImageKind.BACKDROP
        assert (stored.width, stored.height) == (1280, 720)
        assert stored.language is None
        assert stored.is_primary is True

    async def test_an_image_the_provider_stopped_publishing_is_removed(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The wrong implementation this kills: a bare upsert with no delete.

        An upsert can express every change a provider makes to a title's
        artwork except the one that leaves a permanently wrong row — a poster
        withdrawn upstream. `list_for_title` would keep serving it forever and
        the proxy would keep fetching a path the CDN has stopped serving, so
        the symptom is a broken image on a screen with nothing anywhere
        reporting an error.

        The surviving image is asserted too, which is what separates this from
        an implementation that simply deletes everything.
        """
        title_id = await seeder.title()
        await repository.replace_for_titles(
            [title_id],
            [image(title_id, "/kept.jpg"), image(title_id, "/withdrawn.jpg")],
        )
        await repository.replace_for_titles([title_id], [image(title_id, "/kept.jpg")])

        assert [one.provider_path for one in await repository.list_for_title(title_id)] == [
            "/kept.jpg"
        ]

    async def test_a_title_whose_artwork_all_disappeared_is_emptied(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """**Why `title_ids` is passed separately from the rows**, which is
        `CreditRepository.replace_for_titles`' argument arriving at a third
        table: a title whose artwork all disappeared upstream contributes no
        rows at all, so a scope derived from `images` deletes nothing for it
        and leaves its stale artwork in place through every future derivation.
        It is the one row shape a re-derivation cannot repair.

        The second title is in the same call and keeps its image, so an
        implementation that "fixed" this by deleting the whole scope
        unconditionally fails here too.
        """
        emptied = await seeder.title()
        kept = await seeder.title()
        await repository.replace_for_titles(
            [emptied, kept], [image(emptied, "/gone.jpg"), image(kept, "/stays.jpg")]
        )

        await repository.replace_for_titles([emptied, kept], [image(kept, "/stays.jpg")])

        assert await repository.list_for_title(emptied) == []
        assert len(await repository.list_for_title(kept)) == 1

    async def test_a_replace_leaves_a_title_outside_its_scope_alone(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The wrong implementation this kills: a delete that is not scoped —
        one that clears `images` of everything the call did not name, which
        empties the whole catalog's artwork the first time the derivation runs
        over one page."""
        first = await seeder.title()
        second = await seeder.title()
        await repository.replace_for_titles([first], [image(first, "/first.jpg")])

        await repository.replace_for_titles([second], [image(second, "/second.jpg")])

        assert [one.provider_path for one in await repository.list_for_title(first)] == [
            "/first.jpg"
        ]

    async def test_the_same_path_under_two_providers_is_two_images(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The wrong implementation this kills: a key of `(title_id,
        provider_path)`, with `provider` dropped as redundant because there is
        only one `MetadataProvider` today.

        Two providers publishing `/abc.jpg` is not exotic — a path is a
        provider-local name, and `provider` is on the row precisely so a
        catalog holding two providers' artwork stays legible after either is
        turned off. Under the narrower key the second provider's image silently
        overwrites the first's and the row's `provider` column starts lying
        about where its path can be fetched from.
        """
        title_id = await seeder.title()
        await repository.replace_for_titles(
            [title_id],
            [
                image(title_id, "/shared-path.jpg", provider="tmdb"),
                image(title_id, "/shared-path.jpg", provider="an-invented-provider"),
            ],
        )

        stored = await repository.list_for_title(title_id)
        assert {one.provider for one in stored} == {"tmdb", "an-invented-provider"}

    async def test_two_titles_may_reference_one_path(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The other side of the key, and the reason it is not simply
        "stricter": the same artwork legitimately belongs to two titles — a
        film and its re-release, a series and its miniseries cut — so a key
        that collapsed them would silently give one title the other's poster
        id. Measured against the real constraint when `m09c` was written: two
        rows, which is right."""
        first = await seeder.title()
        second = await seeder.title()
        await repository.replace_for_titles(
            [first, second], [image(first, "/shared.jpg"), image(second, "/shared.jpg")]
        )

        assert len(await repository.list_for_title(first)) == 1
        assert len(await repository.list_for_title(second)) == 1

    async def test_a_duplicate_path_inside_one_batch_is_tolerated_and_the_last_wins(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """Required rather than defensive, and the real implementation says so
        loudly: without a `SELECT DISTINCT ON` over the conflict target,
        Postgres answers `CardinalityViolationError: ON CONFLICT DO UPDATE
        command cannot affect row a second time` and a whole derivation batch
        fails on a payload that merely listed one poster twice.

        Last-wins is asserted rather than left to whichever row survived: "one
        of them" is satisfied by an implementation that keeps an arbitrary one,
        and the caller's own ordering is the only tiebreak that means anything.
        """
        title_id = await seeder.title()
        written = await repository.replace_for_titles(
            [title_id],
            [
                image(title_id, "/twice.jpg", width=100),
                image(title_id, "/twice.jpg", width=900),
            ],
        )

        stored = await repository.list_for_title(title_id)
        assert [one.width for one in stored] == [900]
        assert written == 1

    async def test_list_for_title_puts_the_primary_first(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """`(is_primary DESC, id)`, with `id` as a tiebreak only.

        **The fixture makes id order disagree with the answer**, which is the
        whole reason this case can fail at all: every id here is a UUIDv7
        minted in construction order, so a fixture whose primary happened to be
        seeded first would let a bare `ORDER BY id` pass — the trap that cost
        M7 five untested orderings. Both premises are asserted, because a later
        edit that re-aligned the two orders would silently delete this case's
        teeth.
        """
        title_id = await seeder.title()
        ordinary = image(title_id, "/ordinary.jpg", is_primary=False)
        also_ordinary = image(title_id, "/also-ordinary.jpg", is_primary=False)
        primary = image(title_id, "/primary.jpg", is_primary=True)
        seeded = [ordinary, also_ordinary, primary]
        assert [one.id for one in seeded] == sorted(one.id for one in seeded), (
            "the premise: ids ascend in construction order, so id order is a "
            "real alternative answer this case has to rule out"
        )
        assert primary.id != min(one.id for one in seeded), (
            "the premise: the primary is not also the lowest id, so ORDER BY id "
            "cannot answer correctly by accident"
        )
        await repository.replace_for_titles([title_id], seeded)

        stored = await repository.list_for_title(title_id)
        assert [one.provider_path for one in stored] == [
            "/primary.jpg",
            "/ordinary.jpg",
            "/also-ordinary.jpg",
        ]

    async def test_list_for_title_is_scoped_to_its_title(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The wrong implementation this kills: a read with the filter
        forgotten, which returns the whole table in physical order — satisfying
        every membership assertion in this file and no positional one. A second
        title's artwork is seeded for exactly that reason."""
        wanted = await seeder.title()
        other = await seeder.title()
        await repository.replace_for_titles(
            [wanted, other], [image(wanted, "/wanted.jpg"), image(other, "/other.jpg")]
        )

        assert [one.provider_path for one in await repository.list_for_title(wanted)] == [
            "/wanted.jpg"
        ]

    async def test_primary_for_titles_answers_only_the_requested_kind(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The wrong implementation this kills: `kind` accepted and ignored.

        It has the property that makes this milestone dangerous — the answer is
        populated, correctly shaped and about the wrong artwork, so a row card
        paints a 16:9 backdrop into a 2:3 poster slot and nothing reports an
        error. The backdrop is seeded *first* and flagged, so the ignoring
        implementation answers with it rather than with nothing.
        """
        title_id = await seeder.title()
        await repository.replace_for_titles(
            [title_id],
            [
                image(title_id, "/backdrop.jpg", kind=ImageKind.BACKDROP, is_primary=True),
                image(title_id, "/poster.jpg", kind=ImageKind.POSTER, is_primary=True),
            ],
        )

        found = await repository.primary_for_titles([title_id], ImageKind.POSTER)
        assert found[title_id].provider_path == "/poster.jpg"

    async def test_primary_for_titles_prefers_the_flagged_image_over_the_first_one(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """`is_primary DESC` leading the order, and the wrong implementation is
        one that takes whatever row came back first.

        The flagged image is constructed *second*, so it carries the later id
        and "first inserted" names the other row.
        """
        title_id = await seeder.title()
        ordinary = image(title_id, "/ordinary.jpg", is_primary=False)
        flagged = image(title_id, "/flagged.jpg", is_primary=True)
        assert ordinary.id < flagged.id, (
            "the premise: the flagged image is not also the first by id, so "
            "an id-ordered read cannot answer correctly by accident"
        )
        await repository.replace_for_titles([title_id], [ordinary, flagged])

        found = await repository.primary_for_titles([title_id], ImageKind.POSTER)
        assert found[title_id].provider_path == "/flagged.jpg"

    async def test_primary_for_titles_falls_back_to_the_first_when_nothing_is_flagged(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """A provider that flags nothing is ordinary — TMDb publishes no
        "primary" bit at all, so `is_primary` is a judgement the derivation
        makes, and a derivation that declines to make it must not cost the
        title its artwork.

        The wrong implementation this kills is `WHERE is_primary`, which
        answers with an empty shelf on a title holding three perfectly good
        posters.
        """
        title_id = await seeder.title()
        earliest = image(title_id, "/earliest.jpg", is_primary=False)
        later = image(title_id, "/later.jpg", is_primary=False)
        assert earliest.id < later.id, "the premise: the expected answer is the lower id"
        await repository.replace_for_titles([title_id], [earliest, later])

        found = await repository.primary_for_titles([title_id], ImageKind.POSTER)
        assert found[title_id].provider_path == "/earliest.jpg"

    async def test_primary_for_titles_omits_a_title_it_has_nothing_for(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """Absent means "no artwork of this kind", never "not asked", so a
        caller iterates its own ids rather than reading a short answer as a
        full one. The wrong implementation this kills is one that pads the map
        with a placeholder, which a row card would render as a broken image
        rather than as no image at all."""
        with_artwork = await seeder.title()
        without = await seeder.title()
        await repository.replace_for_titles(
            [with_artwork, without], [image(with_artwork, "/only.jpg", is_primary=True)]
        )

        found = await repository.primary_for_titles([with_artwork, without], ImageKind.POSTER)
        assert set(found) == {with_artwork}

    async def test_primary_for_titles_answers_every_title_of_a_long_shelf(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The N+1 this port exists in this shape to prevent: a shelf is up to
        thirty cards and `GET /home` composes ten shelves, so a read per card
        is three hundred round trips a screen.

        Membership is what a shared contract case can assert; the *statement
        count* is asserted against the fake in `tests/unit/`, counted rather
        than timed — a timing assertion against an in-memory dict measures the
        dict.
        """
        titles = [await seeder.title() for _ in range(12)]
        await repository.replace_for_titles(
            titles,
            [image(one, f"/card-{index}.jpg", is_primary=True) for index, one in enumerate(titles)],
        )

        found = await repository.primary_for_titles(titles, ImageKind.POSTER)
        assert set(found) == set(titles)

    async def test_get_answers_the_stored_image_by_its_own_id(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The serve path's resolve: `GET /images/{id}` holds an id and nothing
        else, and needs the provider and the path to build a fetch URL. A
        second title's image is stored alongside, so a `get` that returns
        whatever it found first fails here."""
        title_id = await seeder.title()
        other = await seeder.title()
        await repository.replace_for_titles(
            [title_id, other],
            [image(title_id, "/wanted.jpg", width=500), image(other, "/other.jpg")],
        )
        stored = (await repository.list_for_title(title_id))[0]

        found = await repository.get(stored.id)
        assert found is not None
        assert (found.provider, found.provider_path, found.width) == ("tmdb", "/wanted.jpg", 500)

    async def test_get_answers_none_for_an_id_no_row_carries(
        self, repository: ImageRepository
    ) -> None:
        """`None`, never a raise: a client asking for an image the catalog
        re-derived away is a 404, and a port that raised would make the route's
        ordinary case an exception path."""
        assert await repository.get(uuid.uuid4()) is None

    async def test_replace_reports_the_number_of_rows_written(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """A number rather than a reassurance — `usher derive`'s report is the
        one thing that tells an operator a derivation reached this table at
        all. Counted after deduplication, so it is what was written and not
        what was handed in."""
        title_id = await seeder.title()
        written = await repository.replace_for_titles(
            [title_id], [image(title_id, "/one.jpg"), image(title_id, "/two.jpg")]
        )
        assert written == 2

    async def test_an_empty_call_is_a_no_op(self, repository: ImageRepository) -> None:
        """No titles and no rows is a batch the derivation legitimately
        assembles — a page of skeleton titles nobody has enriched — and it must
        not be an error or a delete of anything."""
        assert await repository.replace_for_titles([], []) == 0

    async def test_a_scope_with_no_rows_still_empties_its_titles(
        self, repository: ImageRepository, seeder: ImageSeeder
    ) -> None:
        """The early-return trap, stated separately from the case above because
        the two look identical and only one of them is a no-op: a guard reading
        `if not images: return 0` skips the delete, so a title whose artwork
        all disappeared upstream keeps it forever. `title_ids` non-empty with
        `images` empty is exactly that state, and it is the ordinary shape of a
        provider withdrawing a film's only poster."""
        title_id = await seeder.title()
        await repository.replace_for_titles([title_id], [image(title_id, "/gone.jpg")])

        assert await repository.replace_for_titles([title_id], []) == 0
        assert await repository.list_for_title(title_id) == []
