"""Tier 1 of the two-tier suggest: a btree prefix probe over names."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.ports.search import SearchHit, SuggestIndex

# `LIKE`'s three metacharacters, and the escape they are prefixed with.
_LIKE_ESCAPE = "\\"
_LIKE_SPECIALS = (_LIKE_ESCAPE, "%", "_")

# **Both tiers order their answers the same way, minus the key tier 1 does not have.**
# Tier 2 sorts `dist ASC, tmdb_popularity DESC NULLS LAST, tmdb_vote_count DESC NULLS
# LAST, id ASC`; every row here is an exact prefix match, so there is no distance to
# lead with and the remaining three are identical.
_PREFIX = """
WITH matched AS (
    SELECT t.id AS title_id
    FROM titles AS t
    WHERE lower(t.name) LIKE :pattern
    UNION
    SELECT n.title_id
    FROM title_search_names AS n
    WHERE lower(n.name) LIKE :pattern
)
SELECT m.title_id AS id
FROM matched AS m
JOIN titles AS t ON t.id = m.title_id
ORDER BY t.tmdb_popularity DESC NULLS LAST, t.tmdb_vote_count DESC NULLS LAST, m.title_id ASC
LIMIT :limit
"""


def _pattern(prefix: str) -> str:
    """`prefix` as a `LIKE` pattern anchored at the start of the name.

    **Escaped, because `%` and `_` are two keys on the keyboard of a box that
    runs a query per keystroke.** Unescaped, a typed `%` is `LIKE '%%'` -- the
    whole catalog collected, de-duplicated and sorted to answer a keystroke --
    and `_` matches every single-character name. Both are ordinary characters
    in a film title, so refusing them is not an option either.

    Escaping leaves the index usable: PostgreSQL extracts a prefix from a
    `LIKE` pattern by stopping at the first *unescaped* metacharacter, so an
    escaped one is part of the literal prefix and the range condition simply
    starts one character later.
    """
    for special in _LIKE_SPECIALS:
        prefix = prefix.replace(special, _LIKE_ESCAPE + special)
    return f"{prefix}%"


class PostgresPrefixSuggestIndex(SuggestIndex):
    """Prefix-only type-ahead over `titles.name` and `title_search_names.name`.

    **Writes nothing.**
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        # An empty box is the state of every page load and of every backspace to zero,
        # and `LIKE '%'` over 1.27M rows is a whole-catalog sort for a question nobody
        # asked.
        if not prefix.strip():
            return []
        rows = await self._session.execute(
            text(_PREFIX),
            {"pattern": _pattern(prefix.lower()), "limit": max(limit, 0)},
        )
        # **Every hit scores 1.0, and that is the honest number rather than a
        # placeholder.** `SearchHit.score` is a rank-shaped value for a caller to
        # render, and on this tier every row is an exact prefix match -- the distance
        # tier 2 varies its score with is zero for all of them.
        return [SearchHit(title_id=row.id, score=1.0) for row in rows]


__all__ = ["PostgresPrefixSuggestIndex"]
