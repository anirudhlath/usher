"""Emby's library units: what each key names, and a library's episodes in chunks."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from usher.domain.sync import WalkStage

_BOUND = re.compile(r"([0-9]+)(?:@(-?[0-9]+))?")

#: The unit holding what the account is watching. It is no library's, so
#: `parse_unit_key` refuses it.
SEED_KEY = "seed"


def _bound(offset: int, key: int | None) -> str:
    return str(offset) if key is None else f"{offset}@{key}"


def _parse_bound(text: str) -> tuple[int, int | None] | None:
    found = _BOUND.fullmatch(text)
    if found is None:
        return None
    offset, key = found.groups()
    return int(offset), None if key is None else int(key)


@dataclass(frozen=True, slots=True)
class LibraryUnit:
    """One unit of a library: its titles, or a `StartIndex` range of its episodes.

    `upper` is exclusive, and `None` runs to the end of the library. A keyed chunk also
    carries the `DateCreated` key of the item at each bound when it was planned.
    """

    stage: WalkStage
    view_id: str
    lower: int = 0
    upper: int | None = None
    lower_key: int | None = None
    upper_key: int | None = None

    @property
    def key(self) -> str:
        if self.stage is WalkStage.TITLES:
            return f"titles:{self.view_id}"
        upper = "" if self.upper is None else _bound(self.upper, self.upper_key)
        return f"episodes:{self.view_id}:{_bound(self.lower, self.lower_key)}:{upper}"

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


def keyed_chunks(chunks: Sequence[LibraryUnit], keys: Sequence[int | None]) -> list[LibraryUnit]:
    """`chunks` bounded by the key at each boundary, one per boundary.

    A boundary with no key ends the chunks there: the chunk before it runs to the end.
    """
    if len(keys) != len(chunks) - 1:
        raise ValueError(f"{len(chunks)} chunks have {len(chunks) - 1} boundaries, not {len(keys)}")
    keyed: list[LibraryUnit] = []
    lower_key: int | None = None
    for chunk, upper_key in zip(chunks, [*keys, None], strict=True):
        if upper_key is None:
            keyed.append(replace(chunk, upper=None, lower_key=lower_key))
            break
        keyed.append(replace(chunk, lower_key=lower_key, upper_key=upper_key))
        lower_key = upper_key
    return keyed


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
    view_id, lower_text, upper_text = parts
    lower = _parse_bound(lower_text)
    upper = _parse_bound(upper_text) if upper_text else None
    if not view_id or lower is None or (upper_text and upper is None):
        return None
    if upper is not None and upper[0] <= lower[0]:
        return None
    return LibraryUnit(
        WalkStage.EPISODES,
        view_id,
        lower[0],
        None if upper is None else upper[0],
        lower_key=lower[1],
        upper_key=None if upper is None else upper[1],
    )
