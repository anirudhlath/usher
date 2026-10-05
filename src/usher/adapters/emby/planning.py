"""Emby's library units: what each key names, and a library's episodes in chunks."""

import re
from dataclasses import dataclass

from usher.domain.sync import WalkStage

_OFFSET = re.compile(r"[0-9]+")

#: The unit holding what the account is watching. It is no library's, so
#: `parse_unit_key` refuses it.
SEED_KEY = "seed"


@dataclass(frozen=True, slots=True)
class LibraryUnit:
    """One unit of a library: its titles, or a `StartIndex` range of its episodes.

    `upper` is exclusive, and `None` runs to the end of the library.
    """

    stage: WalkStage
    view_id: str
    lower: int = 0
    upper: int | None = None

    @property
    def key(self) -> str:
        if self.stage is WalkStage.TITLES:
            return f"titles:{self.view_id}"
        return f"episodes:{self.view_id}:{self.lower}:{'' if self.upper is None else self.upper}"

    @property
    def item_types(self) -> str:
        """The `IncludeItemTypes` this unit lists."""
        return "Movie,Series" if self.stage is WalkStage.TITLES else "Episode"

    def label(self, library: str) -> str:
        """How logs and the CLI name this unit."""
        if self.stage is WalkStage.TITLES:
            return f"titles in {library}"
        if self.upper is not None:
            return f"episodes in {library}, {self.lower:,} to {self.upper:,}"
        if self.lower:
            return f"episodes in {library}, from {self.lower:,}"
        return f"episodes in {library}"


def episode_chunks(view_id: str, held: int, unit_max_items: int) -> list[LibraryUnit]:
    """A library's episodes in chunks of `unit_max_items`, as many as it holds items for.

    The last chunk has no upper bound.
    """
    count = max(1, (held + unit_max_items - 1) // unit_max_items)
    return [
        LibraryUnit(
            WalkStage.EPISODES,
            view_id,
            index * unit_max_items,
            None if index == count - 1 else (index + 1) * unit_max_items,
        )
        for index in range(count)
    ]


def parse_unit_key(key: str) -> LibraryUnit | None:
    """The library unit `key` names, or `None` if it names none."""
    kind, _, rest = key.partition(":")
    if kind == "titles":
        return LibraryUnit(WalkStage.TITLES, rest) if rest else None
    if kind != "episodes":
        return None
    parts = rest.rsplit(":", 2)
    if len(parts) != 3:
        return None
    view_id, lower, upper = parts
    if not view_id or not _OFFSET.fullmatch(lower) or (upper and not _OFFSET.fullmatch(upper)):
        return None
    bounded = int(upper) if upper else None
    if bounded is not None and bounded <= int(lower):
        return None
    return LibraryUnit(WalkStage.EPISODES, view_id, int(lower), bounded)
