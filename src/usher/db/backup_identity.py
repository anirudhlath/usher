"""What a backup carries instead of a title id, and what restore does when it cannot
find one.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from usher.domain.episode import Episode
from usher.domain.title import Title
from usher.ports.repository import (
    EpisodeReference,
    EpisodeRepository,
    TitleReference,
    TitleRepository,
)

__all__ = [
    "RESOLUTION_ORDER",
    "UNRESOLVED_RULE",
    "Unresolved",
    "UnresolvedRule",
    "episode_reference",
    "keys_tried",
    "resolve_episodes",
    "resolve_titles",
    "title_reference",
]

# : The rungs a title reference is resolved on, in order, first hit wins.
RESOLUTION_ORDER: Final[tuple[str, ...]] = ("imdb_id", "kind+tmdb_id", "id")


class UnresolvedRule(StrEnum):
    """What restore does with a reference the target does not hold."""

    REFUSE = "refuse"
    """Refuse that row, name it, count it. The operator has to see it."""

    NULL = "null"
    """Write the column `NULL` and count it. The column already permits it."""


# : Per table, because the answer differs per table on purpose and is not a : function
# of anything K1 already records: K4 reads one field rather than : re-deriving the
# argument at each call site.
UNRESOLVED_RULE: Final[MappingProxyType[str, UnresolvedRule]] = MappingProxyType(
    {
        "watch_states": UnresolvedRule.REFUSE,
        "media_items": UnresolvedRule.REFUSE,
        "search_queries": UnresolvedRule.NULL,
    }
)


@dataclass(frozen=True, slots=True)
class Unresolved:
    """A reference the target does not hold, carrying what was looked for.

    **Not `None`.** The five columns this layer feeds are four foreign keys
    and one nullable one, so `None` is a value a caller can write -- and a
    caller that wrote it into `watch_states.title_id` would get a foreign-key
    error out of Postgres instead of *"this watch state's title is not in the
    target"*. A distinct type is what makes the two impossible to confuse and
    what gives K4's report something to name and count.
    """

    reference: TitleReference | EpisodeReference
    keys_tried: tuple[str, ...]


def keys_tried(reference: TitleReference | EpisodeReference) -> tuple[str, ...]:
    """The rungs this reference offers, rendered, in resolution order.

    Rendered rather than structured because the one consumer is an
    operator's line: *"3 watch states refused: tt99009999, ..."*. A rung the
    reference cannot offer is omitted rather than rendered as `None` -- a
    report saying `tmdb_id=None` sends somebody looking for a title that was
    never asked about.
    """
    if isinstance(reference, EpisodeReference):
        return tuple(
            f"{one} S{reference.season_number:02d}E{reference.episode_number:02d}"
            for one in keys_tried(reference.title)
        )
    rungs = []
    if reference.imdb_id is not None:
        rungs.append(f"imdb_id={reference.imdb_id}")
    if reference.tmdb_id is not None:
        rungs.append(f"{reference.kind.value}+tmdb_id={reference.tmdb_id}")
    rungs.append(f"id={reference.id}")
    return tuple(rungs)


def title_reference(title: Title) -> TitleReference:
    """What an artifact writes down for a title.

    Every key the row has, not the strongest one: the target may be a
    catalog at a different bootstrap phase, so a title carrying an `imdb_id`
    here can be a row carrying only a `tmdb_id` there -- 980,176 of the live
    catalog's titles have no `tmdb_id` and 72 have no `imdb_id`, and a
    partially-crosswalked target is the ordinary state of a fresh install.
    Dropping the rungs at *write* time would decide the resolution before
    the target is known.
    """
    return TitleReference(
        kind=title.kind,
        id=title.id,
        imdb_id=title.imdb_id,
        tmdb_id=title.tmdb_id,
    )


def episode_reference(episode: Episode, series: TitleReference) -> EpisodeReference:
    """What an artifact writes down for an episode.

    Takes the series' reference rather than its `Title`, because the caller
    building a page of episodes already holds one per series and rebuilding
    it per episode would be one `title_reference` call per row on a library
    where 999,827 of 1,126,674 items are episodes.
    """
    return EpisodeReference(
        title=series,
        season_number=episode.season_number,
        episode_number=episode.episode_number,
    )


async def resolve_titles(
    repository: TitleRepository, references: Sequence[TitleReference]
) -> dict[TitleReference, uuid.UUID | Unresolved]:
    """Every reference, answered: an id the target holds, or an `Unresolved`.

    **Every** reference, including one that repeats -- two watch states for
    one title is the ordinary shape of a household -- because K4 iterates
    this mapping and a reference absent from it is a row written with no
    target at all. The repository is asked once per distinct reference.
    """
    return await _answer(repository.resolve_natural_keys, references)


async def resolve_episodes(
    repository: EpisodeRepository, references: Sequence[EpisodeReference]
) -> dict[EpisodeReference, uuid.UUID | Unresolved]:
    """`resolve_titles` for episodes; see its docstring."""
    return await _answer(repository.resolve_natural_keys, references)


async def _answer[ReferenceT: (TitleReference, EpisodeReference)](
    resolve: Callable[[Sequence[ReferenceT]], Awaitable[dict[ReferenceT, uuid.UUID]]],
    references: Sequence[ReferenceT],
) -> dict[ReferenceT, uuid.UUID | Unresolved]:
    """The one place an absence becomes a refusal, shared by both arms so
    they cannot drift on what "not found" means.

    An empty batch asks nothing: `resolve_natural_keys([])` is a round trip
    to learn nothing, the same guard `list_by_ids` and `resolve_tmdb_ids`
    carry one port up.

    `references` is passed whole because the port already deduplicates its
    own probe list, and the comprehension collapses the repeats again on the
    way out -- so a household naming one title from every watch state costs
    one probe here and none extra.

    Membership rather than falsiness, for the reason `_Family.owned` states
    one subsystem over: the resolver omits what it could not find, and a
    `uuid.UUID` being truthy is a fact about a type this module does not own.
    """
    if not references:
        return {}
    found = await resolve(references)
    return {
        reference: found[reference]
        if reference in found
        else Unresolved(reference, keys_tried(reference))
        for reference in references
    }
