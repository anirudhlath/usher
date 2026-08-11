"""Artwork references, and the one port whose whole job is to keep an id.

Implemented by `usher.db.repositories.image`'s `PostgresImageRepository`.

**Four methods and no more.** `replace_for_titles` is the derive-time write,
`primary_for_titles` is the shelf read that keeps `RowCard.artwork` from
costing a query per card, `list_for_title` is the detail read, and `get` is the
proxy's serve-path resolve. There is no `add`, no `delete`, and no
`replace_for_episodes` or `replace_for_people`: M9's two artwork consumers are
both title-shaped, a person's headshot belongs with `GET /people/{id}`, and
`SearchIndex`' settled argument applies unchanged — *"a port method whose only
test is its own test is a liability, and the failure mode of a rare path is
that it has rotted by the time somebody needs it."* The *key* covers all three
owner kinds (`m09c`); the *methods* cover the one with a writer.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.enums import ImageKind
from usher.domain.image import Image

__all__ = ["ImageRepository"]


class ImageRepository(ABC):
    """Persistence for `images` — PRD 02's `Image`, and the last of the four
    entities `raw_payloads` was kept for.

    **This port exists to make one failure impossible: id churn.** A `Person`
    re-points through `resolve_tmdb_ids` because TMDb gives a person an integer
    id. Artwork has none — a path is all a provider publishes — so
    `(the one owner, provider, provider_path)` is the natural key, and a write
    that does not recognise it mints a fresh UUIDv7 per sighting. Nothing
    breaks visibly: every read succeeds, every image renders. What breaks is
    that every client's cached `/images/{id}` is invalidated on every
    `usher derive`, and
    [ADR-0032](../../../../docs/prd/decisions/0032-the-image-proxy-clamps-to-a-ladder.md)'s
    `Cache-Control: immutable` becomes a lie the first time a title is
    re-derived.

    `m09c` is what makes that enforceable rather than hoped for:
    `uq_images_owner_provider_path`, spelled `UNIQUE NULLS NOT DISTINCT` over
    the whole owner triple. An implementation **may not** simulate it with a
    read-then-write — two concurrent derivations both read absent and both
    insert — which is why the constraint was a requested migration rather than
    a defensive `SELECT`.

    **The read order is `(is_primary DESC, id)`, and the missing middle key is
    a stated limit rather than an oversight.** Group C's preamble asked for a
    `sort_order` column; ADR-0032's request deliberately left it out
    (*"it belongs to whoever reads images rather than to the proxy"*) and
    `m09c` is authorised for the key alone. The consequence: `id` is
    *first-sighting* order, so a provider that re-ranks a title's posters can
    move exactly one thing in Usher's answer — which of them is primary.
    Restoring the rest is a column, a request and one line in each of the two
    statements below.

    Same session ownership as every other repository here: methods flush so
    conflicts surface immediately, none commits.
    """

    @abstractmethod
    async def replace_for_titles(
        self, title_ids: Sequence[uuid.UUID], images: Sequence[Image]
    ) -> int:
        """Make `title_ids`' stored artwork exactly `images`, keeping the id of
        every `(provider, provider_path)` that survived.

        **A scoped delete plus an upsert, and both halves are load-bearing.**
        The upsert is what keeps an id; the delete is what expresses the one
        change an upsert cannot — a poster withdrawn upstream. Without it
        `list_for_title` serves a path the CDN has stopped serving, forever,
        and the symptom is a broken image on a screen with nothing anywhere
        reporting an error.

        **`title_ids` is passed separately from the rows, and that is not
        redundancy.** `CreditRepository.replace_for_titles` already gives the
        argument in two sentences and it arrives here at a third table: a title
        whose artwork all disappeared upstream contributes no rows at all, so a
        scope derived from `images` deletes nothing for it and leaves its stale
        artwork in place through every future derivation. It is the one row
        shape a re-derivation cannot repair. Correspondingly, a call with
        `title_ids` non-empty and `images` empty is **not** a no-op, and an
        implementation guarding with `if not images: return 0` has reintroduced
        exactly that defect.

        Every image must be owned by a title named in `title_ids`. The port
        writes title-owned artwork only — see the module docstring.

        A batch may name one `(provider, provider_path)` twice; one derivation
        pass really does see a payload list a poster twice. An implementation
        deduplicates rather than assuming, and **the last such row wins**, so
        the caller's own ordering is the tiebreak. Tolerating it is not
        politeness: without it the real implementation answers
        `CardinalityViolationError: ON CONFLICT DO UPDATE command cannot affect
        row a second time` and the whole derivation batch fails.

        Returns the number of rows written after deduplication — a number
        rather than a reassurance, which is what makes `usher derive`'s report
        mean something.

        Idempotent by construction: PRD 08's redelivery rule, and the job queue
        *will* redeliver. Running it twice with the same arguments produces the
        same rows, the same ids and the same count.

        A `title_id` naming a row that does not exist, or a value a column
        cannot hold, raises `RepositoryConflict` rather than a raw storage
        error, and leaves the session usable for the caller's other pending
        work — the derivation commits a batch of images together with its job
        checkpoint.
        """

    @abstractmethod
    async def primary_for_titles(
        self, title_ids: Sequence[uuid.UUID], kind: ImageKind
    ) -> dict[uuid.UUID, Image]:
        """One image of `kind` per title, for a whole shelf, in one statement.

        **The N+1 this port exists in this shape to prevent.** A shelf is up to
        thirty cards and `GET /home` composes ten of them, so the obvious
        "read the title, then read its poster" shape is three hundred round
        trips a screen. The signature takes a sequence so a caller cannot
        express the other one.

        **"Primary" means the flagged image, and the first one when nothing is
        flagged.** TMDb publishes no primary bit — it is a judgement the
        derivation makes — so `WHERE is_primary` would answer with an empty
        shelf for a title holding three perfectly good posters, on nothing
        worse than a derivation that declined to choose. The order is the same
        `(is_primary DESC, id)` `list_for_title` uses, and this is its first
        row per title.

        **`kind` filters and may not be ignored.** A backdrop returned for a
        poster request is the failure that makes this milestone dangerous: the
        answer is populated, correctly shaped, and paints a 16:9 image into a
        2:3 slot with nothing reporting an error.

        A title with no image of that kind is **absent from the mapping**,
        never present with a placeholder — absent means "no artwork", not "not
        asked", and a placeholder is a broken image where a client wanted the
        chance to render none.
        """

    @abstractmethod
    async def list_for_title(self, title_id: uuid.UUID) -> list[Image]:
        """Everything one title's artwork holds, in `(is_primary DESC, id)`.

        `GET /titles/{id}`'s `images` key, and the surface every
        `replace_for_titles` case asserts through — a write port with no read
        can only assert on counts, and a count cannot tell a correct row from a
        wrong one.

        Unbounded, deliberately: a title's artwork is tens of rows because a
        provider publishes tens, not because anything here caps it, and a
        `limit` whose only caller passes the default is a parameter that
        documents a bound nothing enforces.
        """

    @abstractmethod
    async def get(self, image_id: uuid.UUID) -> Image | None:
        """One image by its own id — the proxy's serve-path resolve.

        `GET /images/{id}` holds an id and nothing else and needs `provider`
        and `provider_path` to build a fetch URL. `None` rather than a raise
        for an id no row carries: a client asking for artwork the catalog
        re-derived away is a 404, and a port that raised would make the route's
        ordinary case an exception path.
        """
