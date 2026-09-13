"""Artwork references, and the one port whose whole job is to keep an id."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from usher.domain.enums import ImageKind
from usher.domain.image import Image

__all__ = ["ImageRepository"]


class ImageRepository(ABC):
    """Persistence for `images`.

    PRD 02's `Image`, and the last of the four entities `raw_payloads` was kept for.
    """

    @abstractmethod
    async def replace_for_titles(
        self, title_ids: Sequence[uuid.UUID], images: Sequence[Image]
    ) -> int:
        """Make `title_ids`' stored artwork exactly `images`.

        keeping the id of every `(provider, provider_path)` that survived.
        """

    @abstractmethod
    async def primary_for_titles(
        self, title_ids: Sequence[uuid.UUID], kind: ImageKind
    ) -> dict[uuid.UUID, Image]:
        """One image of `kind` per title, for a whole shelf, in one statement."""

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
