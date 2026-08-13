"""In-memory `ImageRepository`.

Keyed on the natural key rather than on `Image.id`, which is the whole point:
`_by_key` is a dict whose key is `(title_id, episode_id, person_id, provider,
provider_path)`, so a second `replace_for_titles` finds the stored row and
keeps its id. A fake keyed on the id would be the delete-then-insert
implementation the contract exists to kill, wearing a dict.

**Where this is more forgiving than Postgres, on purpose.** Five places, each
of which the paired `tests/integration/test_image_repository.py` run is what
actually closes:

- **A Python tuple key is `NULLS NOT DISTINCT` for free**, and this is the
  divergence that matters most, because it hides the entire finding that shaped
  `m09c`. `(None, None, person, 'tmdb', '/x.jpg')` is an ordinary dict key and
  two of them collide; in SQL, `UNIQUE (title_id, provider, provider_path)`
  admits both, because Postgres defaults to `NULLS DISTINCT` and NULL never
  collides with NULL. So the careless DDL spelling is **invisible here** —
  every case in the shared contract passes against it — and
  `test_the_key_is_nulls_not_distinct_and_the_obvious_spelling_would_not_be`
  on the Postgres arm is the only thing that can see it. Named first because a
  reader would otherwise assume this fake is the stricter half.
- **No foreign keys**, so an image naming a `title_id` no row carries is stored
  here and is a `RepositoryConflict` there.
  `known_titles` exists only so the seeder has somewhere to put an id; nothing
  in this class reads it, which is itself the statement that no constraint is
  being modelled.
- **No CHECK bodies and no column widths.** Every CHECK on `images` is mirrored
  as a field bound on `Image`, so both implementations refuse a bad row at
  construction and neither exercises the SQL — except for the one class of
  value a *column* refuses and a field does not: `width = 2**31` is an ordinary
  Python `int` here and is refused client-side by asyncpg's own binary encoder
  there, as a bare `DBAPIError` (`_errors.py` holds the measurement).
- **Dict iteration plus a Python `sorted`, rather than an index and an
  `ORDER BY`.** The ordering cases pass on both arms and for different reasons:
  here the key function is the answer, there a deleted `ORDER BY` leaves heap
  order, which on a small fixture is frequently already sorted.
- **No transaction and no SAVEPOINT.** A `replace_for_titles` that raised
  part-way here has already mutated the dict; there, the caller's other pending
  work survives and the title keeps its previous artwork whole.

`calls` counts every method entry, and it is what
`test_a_whole_shelf_costs_one_statement` reads. It is a count of *calls*, not
of statements — the two coincide because every method here answers in one pass,
and the assertion it serves is about a loop the port forbids rather than about
SQL.
"""

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
