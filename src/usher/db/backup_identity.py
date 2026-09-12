"""What a backup carries instead of a title id, and what restore does when
it cannot find one.

K1's `backup_manifest` decided *which tables* an artifact carries. Five
columns in the set it calls precious name a title or an episode **by id**,
and not one of those ids survives a bootstrap boundary --
`db/repositories/bulk.py:611` mints `new_id()` for every row of every batch
on the way into the staging table, and `:670` resolves the collision with
`ON CONFLICT (imdb_id) WHERE imdb_id IS NOT NULL DO UPDATE`. So *within* one
database a re-import is idempotent and a title keeps the id it was first
given; *across* two databases built from the same `title.basics.tsv.gz`,
every title gets a different id, because the `new_id()` calls are
independent. There is no seed, no derivation from `imdb_id`, and ADR-0003 is
what makes it so on purpose.

## The five columns, and the one that decides the design

Read off `pg_constraint` on the live database:

- `watch_states.title_id` -> `titles`, `ON DELETE RESTRICT` (ADR-0010): a
  wrong id fails the insert, and the restore is refused.
- `watch_states.episode_id` -> `episodes`, `RESTRICT`: the same.
- `media_items.title_id` and `.episode_id` -> `titles`/`episodes`,
  `ON DELETE SET NULL`: the delete rule differs and the insert still fails.
- `search_queries.clicked_title_id` -> `titles`, `SET NULL`: the same.
- `curated_rows.card_title_ids` is a **`uuid[]` with no foreign key at
  all**, so **nothing fails**: the shelf renders with dead ids and the
  database cannot tell.

That last entry is the one that decides it. `m08a` took the array
deliberately and `02-data-model.md:825` states the trade in its own heading
-- *"the missing foreign key is the price"*; `db/repositories/curation.py`
explains why `unnest` of parallel arrays cannot express a per-element
reference. So `curated_rows` is the one precious-looking table where a naive
carry is **silent**, and it is also the one PRD 08 already puts in the
rebuildable column and PRD 02 already calls *"rebuildable and not
restorable"*. **Ruling: `curated_rows` is never carried by backup or
restore**, and `backup_manifest`'s entry for it is where that is recorded --
not here, because a table's classification has one home.

## The design: restore never creates a title id, it only resolves one

Every carried reference travels as a natural key and is re-resolved against
the target at restore time. `TitleReference` and `EpisodeReference` are the
shapes (they live on the ports, because
`pyproject.toml`'s third import contract forbids `usher.ports` importing
`usher.db` and a port method has to be typed in terms of something), and
`RESOLUTION_ORDER` is the ladder both arms spell.

- **A title** is carried as `imdb_id`, falling back to `(kind, tmdb_id)` --
  ADR-0011's namespacing is why the kind is part of it -- falling back to
  its own UUID. Coverage on the live catalog, read 2026-08-21 from
  `usher-postgres-1`: **1,272,888 titles; 72 with no `imdb_id`; 980,176 with
  no `tmdb_id`; 6 with neither.**
- **An episode** is carried as its title's key plus `(season_number,
  episode_number)`. `uq_episodes_title_season_episode` is a real unique
  constraint, so this is an identity and not a heuristic.
- **A user** is carried as `name`. `uq_users_name` is a real unique
  constraint and -- unlike what this task's brief recorded -- the ORM
  declares it too: `UserRow.name` is `unique=True`, which
  `usher.db.base.NAMING_CONVENTION`'s `uq_%(table_name)s_%(column_0_N_name)s`
  renders as exactly that name. There is no resolver here because there is
  no user repository port to put one on; K4 reads `users` directly and the
  key it reads by is recorded in `backup_manifest`'s own entry.
- **A source** is carried by its own UUID, and that is correct rather than
  an exception: a source id is minted when an operator adds the source and
  travels *inside* the backup together with the `sources` row it names, so
  `source_credentials.source_id`, `media_items.source_id` and
  `sync_runs.source_id` are internally consistent within one artifact.

## The one place a raw id is accepted, and it is a check rather than a trust

For a title the artifact carries the raw UUID and restore accepts it **if
and only if the target already holds a title with that exact id**. That is
the same-database case -- disaster recovery into the database the backup
came from, which is the ordinary path -- expressed as a lookup rather than
as a mode. It unifies the two cases: there is one code path, and *"restore
into the same database"* is simply the case where every lookup resolves.

**The rung is load-bearing rather than defensive, and the drift is the
evidence.** ADR-0003 makes a title with no provider id a first-class citizen
on purpose; on 2026-08-13 the live catalog held **13** titles with no
`imdb_id` and **0** with neither, and on 2026-08-21 it held **72** and
**6**. The population moves, so the fallback is exercised by real rows.

**Rejected: a fast path that carries ids when a catalog fingerprint
matches.** A hash over `(imdb_id, id)` pairs would tell the two cases apart
cheaply and it buys nothing measurable -- the precious set is small by
construction (`08-operations.md:618`, *"one row per generation per household
per night"*; 8 non-empty precious rows on this deployment) -- while adding a
second code path exercised only in the case the drill does not cover. One
path, always resolve.

**Rejected: remapping `curated_rows.card_title_ids` through this resolver.**
It would work mechanically and it would be restoring a *rendering* rather
than a *judgement*: ADR-0028 says nothing downstream may re-sort a curated
row, a card whose title did not resolve would have to be dropped from the
middle of an ordering the completion was bought for, and the result is a
shelf the model never produced. One completion regenerates it.

## Why `usher.db` and not `usher.services`

`backup_manifest`'s argument, unchanged: this is knowledge about *this
schema*. The tables the rules below are keyed on are table names, and a
module under `usher.services` holding one would put the database's
vocabulary in the layer that is supposed to be ignorant of it. What is new
here is that this module also *calls* two ports -- which is the same
direction `usher.db` already depends in, and is why the resolvers take a
repository rather than a session.
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

#: The rungs a title reference is resolved on, in order, first hit wins.
#: Declared here rather than left implicit in two implementations because
#: the *order* is the design: `imdb_id` is a global identity, `(kind,
#: tmdb_id)` is one only once ADR-0011's namespacing is applied, and the raw
#: id is a check on the target rather than a key at all. Both the Postgres
#: arm and the fake spell this ladder, and the shared contract suite is what
#: keeps them agreeing.
RESOLUTION_ORDER: Final[tuple[str, ...]] = ("imdb_id", "kind+tmdb_id", "id")


class UnresolvedRule(StrEnum):
    """What restore does with a reference the target does not hold."""

    REFUSE = "refuse"
    """Refuse that row, name it, count it. The operator has to see it."""

    NULL = "null"
    """Write the column `NULL` and count it. The column already permits it."""


#: Per table, because the answer differs per table on purpose and is not a
#: function of anything K1 already records: K4 reads one field rather than
#: re-deriving the argument at each call site.
#:
#: `watch_states`: a watch state whose title is missing is a real loss and
#: the operator must see it. It is also recoverable -- enrich the title,
#: restore again -- which is what makes refusing the *helpful* answer rather
#: than merely the strict one. `media_items`: same, and the link is the
#: operator's judgement (K1's `PARTIAL` entry is what carries the two
#: columns). `search_queries.clicked_title_id`: the FK is already
#: `ON DELETE SET NULL`, so `NULL` is a state the column and every reader
#: already handle, and the analytic value is the query text and the outcome
#: rather than the id.
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
