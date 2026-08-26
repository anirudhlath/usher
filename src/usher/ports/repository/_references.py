"""The two shapes a backup artifact names a row by, shared by two ports.

Private and shared for `_results`' reason, arriving at a *parameter* type
rather than a return one. `EpisodeReference` embeds a `TitleReference`
because an episode is named by its series' key plus two numbers, so homing
the title's shape in `title.py` and importing it back into `episode.py`
resolves perfectly well today and makes the episode port drag the title port
into every consumer -- which is the cycle
`test_no_aggregate_module_imports_another_aggregate_module` exists to
prevent, and the first thing it caught after `BulkWriteResult`.

**Why these are ports types at all**, given that
`usher.db.backup_identity` is the module that argues for them and builds
them: `pyproject.toml`'s third import contract (*"db is driven, not
driving"*) forbids `usher.ports` reaching `usher.db`, and
`resolve_natural_keys` has to be typed in terms of something.
`BrowseCursorPosition` and `EpisodeCursorPosition` are the precedent --
typed values a caller builds above and hands down, rather than an opaque
token the port would have to interpret. See ADR-0045.
"""

import uuid
from dataclasses import dataclass

from usher.domain.enums import TitleKind

__all__ = [
    "EpisodeReference",
    "TitleReference",
]


@dataclass(frozen=True, slots=True)
class TitleReference:
    """A title named the way a backup artifact names one: by natural key,
    with its own id as the last rung rather than the first.

    **Here rather than in `usher.db.backup_identity`, which is the module
    that argues for it.** That module builds these and applies the ladder,
    and this port cannot import it: `pyproject.toml`'s third import contract
    (*"db is driven, not driving"*) forbids `usher.ports` reaching
    `usher.db`, and `resolve_natural_keys` has to be typed in terms of
    something. `BrowseCursorPosition` and `EpisodeCursorPosition` are the
    precedent -- typed values a caller builds above and hands down, rather
    than an opaque token the port would have to interpret.

    **`kind` is required, and that is
    [ADR-0011](../../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md)
    spelled where a type checker holds it.** TMDb's movie and series id
    spaces overlap on 26,968 ids -- 47.3% of every series id Wikidata knows,
    measured 2026-07-30 -- so `tmdb_id` alone is not an identity, and
    `ix_titles_tmdb_id_kind` is composite for that reason. A `kind` with a
    default would resolve a series' `tmdb_id` onto whichever film shares the
    integer, and every reference carried against it would land on the wrong
    show. There is no such default: a `tmdb_id` without a `kind` is a
    `TypeError` at construction.

    **`imdb_id` is compared exactly, never folded.** `replace_aliases` folds
    under SQL `lower()` because it compares a *name* somebody typed; a
    provider id is not that, IMDb issues one spelling, and `Title.imdb_id`
    carries `pattern=r"^tt\\d{7,8}$"`. `TT99000011` is a different key from
    `tt99000011` and resolves to nothing.

    `id` is carried whatever the provider ids say, and it is the rung that
    makes disaster recovery into the database the backup came from an
    ordinary lookup rather than a second mode -- accepted if and only if the
    target already holds a title with that exact id. See ADR-0045.
    """

    kind: TitleKind
    id: uuid.UUID
    imdb_id: str | None = None
    tmdb_id: int | None = None


@dataclass(frozen=True, slots=True)
class EpisodeReference:
    """An episode named the way a backup artifact names one: its series'
    natural key, plus the two numbers.

    **This is an identity rather than a heuristic**, and
    `uq_episodes_title_season_episode` (`db/models/episode.py:113`) is why --
    a real unique constraint over `(title_id, season_number,
    episode_number)`, so the triple names at most one row. Without it this
    would be a guess dressed as a key, and the guess is not idle: every
    series has an S01E01, and 32,409 of them makes a resolution that lost
    the series scope a certainty rather than a risk.

    **Both numbers are `int` rather than `int | None`**, for the reason
    `EpisodeCursorPosition` states one class down: `episodes.season_number`
    and `episodes.episode_number` are `nullable=False`, so the unkeyed group
    is provably empty and this annotation is that fact spelled where a type
    checker can hold it.

    **No raw episode id, and that is not an omission.** `TitleReference`
    carries one because a title with neither provider id is a first-class
    citizen (ADR-0003) and has nothing else to be named by; an episode
    always has both numbers, so the natural key is total and the
    same-database case is already covered by the series' own raw-id rung.
    See ADR-0045.
    """

    title: TitleReference
    season_number: int
    episode_number: int
