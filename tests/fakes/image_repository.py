"""In-memory `ImageRepository`."""

import uuid
from collections.abc import Sequence

from usher.domain.enums import ImageKind
from usher.domain.image import Image
from usher.ports.repository import ImageRepository

#: `(title_id, episode_id, person_id, provider, provider_path)` — the natural
#: key `uq_images_owner_provider_path` enforces, as a tuple.
_Key = tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, str, str]


def _key(one: Image) -> _Key:
    return (one.title_id, one.episode_id, one.person_id, one.provider, one.provider_path)


def _read_order(one: Image) -> tuple[bool, uuid.UUID]:
    """`(is_primary DESC, id)`. `not is_primary` because Python sorts ascending
    and `False < True`, so the flagged image leads."""
    return (not one.is_primary, one.id)


class FakeImageRepository(ImageRepository):
    def __init__(self) -> None:
        self._by_key: dict[_Key, Image] = {}
        #: Written by the seeder, read by nothing. See the module docstring:
        #: this fake enforces no foreign key, and an empty affordance is a
        #: clearer statement of that than a check that would pass anyway.
        self.known_titles: set[uuid.UUID] = set()
        self.calls = 0

    def reset_calls(self) -> None:
        self.calls = 0

    async def replace_for_titles(
        self, title_ids: Sequence[uuid.UUID], images: Sequence[Image]
    ) -> int:
        self.calls += 1
        scope = set(title_ids)

        # Last-wins deduplication, which the real implementation needs as a
        # `SELECT DISTINCT ON` to avoid `CardinalityViolationError` and which
        # here is just a dict being written twice. Insertion order is the
        # caller's order, so the last assignment is the last row.
        deduped: dict[_Key, Image] = {}
        for one in images:
            deduped[_key(one)] = one

        # **The delete's scope is `title_ids`, never the incoming rows.** A
        # title in scope contributing nothing has its artwork emptied; a title
        # outside scope is untouched even if a row names it -- which mirrors
        # the SQL exactly, and is why the port says the caller owns that
        # correspondence (`CreditRepository.replace_for_titles`' precedent).
        for stored_key, stored in list(self._by_key.items()):
            if stored.title_id in scope and stored_key not in deduped:
                del self._by_key[stored_key]

        for key, incoming in deduped.items():
            existing = self._by_key.get(key)
            # **The id of the stored row wins, and every other field is
            # assigned.** This is `ON CONFLICT ... DO UPDATE` returning the id
            # the row was first inserted with. `.evolve()` rather than
            # `model_copy(update=)`, so the re-pointed row is re-validated.
            self._by_key[key] = incoming if existing is None else incoming.evolve(id=existing.id)

        return len(deduped)

    async def primary_for_titles(
        self, title_ids: Sequence[uuid.UUID], kind: ImageKind
    ) -> dict[uuid.UUID, Image]:
        self.calls += 1
        wanted = set(title_ids)
        # One pass over everything stored rather than a pass per title, so the
        # `calls` count this fake reports is one whatever the shelf's length --
        # which is the property `test_a_whole_shelf_costs_one_statement`
        # asserts and the loop the port forbids.
        best: dict[uuid.UUID, Image] = {}
        for one in self._by_key.values():
            if one.title_id not in wanted or one.kind is not kind:
                continue
            standing = best.get(one.title_id)
            # `is not None` and a comparison, not `min()` over a filtered list:
            # the fallback when nothing is flagged is the *first* in read
            # order, so the flagged/unflagged decision and the id tiebreak are
            # one comparison rather than two branches.
            if standing is None or _read_order(one) < _read_order(standing):
                best[one.title_id] = one
        return best

    async def list_for_title(self, title_id: uuid.UUID) -> list[Image]:
        self.calls += 1
        return sorted(
            (one for one in self._by_key.values() if one.title_id == title_id), key=_read_order
        )

    async def get(self, image_id: uuid.UUID) -> Image | None:
        self.calls += 1
        # A scan rather than a second index. The real implementation reads
        # `pk_images`; keeping one dict here means the two views cannot drift,
        # and a fake holding tens of rows has nothing to gain from the index.
        return next((one for one in self._by_key.values() if one.id == image_id), None)
