"""`seed_rows` -- one stored row per seed title, replaced whole."""

import uuid
from collections.abc import Sequence

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import refusals_as_conflict
from usher.domain.seed_rows import CardSource, SeedRow, SeedRowCard, SeedRowOutcome
from usher.ports.repository import SeedRowRepository

#: What separates one source name from the next inside a card's entry.
_SOURCE_SEPARATOR = "+"

_LIST_ROWS = """
SELECT seed_title_id, card_title_ids, card_sources, built_at, built_recipe,
       attempted_at, attempt_outcome, attempt_recipe
FROM seed_rows
WHERE seed_title_id = ANY(CAST(:seed_title_ids AS uuid[]))
"""

# **A record that equals the stored one is not written.** A bare `DO UPDATE` rewrites
# the row version for no change, and the scheduler repeats an attempt it has already
# recorded. The guard names every column, so a column added to the table and not here
# is a change the guard throws away -- which `test_a_change_to_any_one_field_is_stored`
# makes loud, one column at a time.
_SAVE_ROW = text(
    """
INSERT INTO seed_rows (seed_title_id, card_title_ids, card_sources, built_at, built_recipe,
                       attempted_at, attempt_outcome, attempt_recipe)
VALUES (CAST(:seed_title_id AS uuid), CAST(:card_title_ids AS uuid[]),
        CAST(:card_sources AS text[]), :built_at, :built_recipe, :attempted_at,
        :attempt_outcome, :attempt_recipe)
ON CONFLICT (seed_title_id) DO UPDATE SET
    card_title_ids = excluded.card_title_ids,
    card_sources = excluded.card_sources,
    built_at = excluded.built_at,
    built_recipe = excluded.built_recipe,
    attempted_at = excluded.attempted_at,
    attempt_outcome = excluded.attempt_outcome,
    attempt_recipe = excluded.attempt_recipe
WHERE (seed_rows.card_title_ids, seed_rows.card_sources, seed_rows.built_at,
       seed_rows.built_recipe, seed_rows.attempted_at, seed_rows.attempt_outcome,
       seed_rows.attempt_recipe)
  IS DISTINCT FROM
      (excluded.card_title_ids, excluded.card_sources, excluded.built_at,
       excluded.built_recipe, excluded.attempted_at, excluded.attempt_outcome,
       excluded.attempt_recipe)
"""
)


class PostgresSeedRowRepository(SeedRowRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for(self, seed_title_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, SeedRow]:
        if not seed_title_ids:
            return {}
        with self._session.no_autoflush:
            result = await self._session.execute(
                text(_LIST_ROWS), {"seed_title_ids": list(seed_title_ids)}
            )
        rows = [_to_domain(found) for found in result.mappings().all()]
        return {row.seed_title_id: row for row in rows}

    async def save(self, row: SeedRow) -> None:
        # **What this table can refuse:** a `seed_title_id` naming no title
        # (`fk_seed_rows_seed_title_id_titles`), and a record the four CHECKs reject.
        # `SeedRow` refuses all of those first; the table is for a writer that
        # bypasses it.
        async with refusals_as_conflict(
            self._session, "a seed row violates the table's own bounds"
        ):
            await self._session.execute(
                _SAVE_ROW,
                {
                    "seed_title_id": row.seed_title_id,
                    "card_title_ids": [card.title_id for card in row.cards],
                    "card_sources": [_encode_sources(card) for card in row.cards],
                    "built_at": row.built_at,
                    "built_recipe": row.built_recipe,
                    "attempted_at": row.attempted_at,
                    "attempt_outcome": row.attempt_outcome.value,
                    "attempt_recipe": row.attempt_recipe,
                },
            )


def _encode_sources(card: SeedRowCard) -> str:
    """The card's source names, sorted and joined: `close+named`.

    Sorted because a set has no order of its own, and the stored form has to be one a
    reader can compare without knowing which order a writer iterated in.
    """
    return _SOURCE_SEPARATOR.join(sorted(source.value for source in card.sources))


def _decode_sources(stored: str) -> frozenset[CardSource]:
    return frozenset(CardSource(name) for name in stored.split(_SOURCE_SEPARATOR))


def _to_domain(row: RowMapping) -> SeedRow:
    return SeedRow(
        seed_title_id=row["seed_title_id"],
        cards=tuple(
            SeedRowCard(title_id=title_id, sources=_decode_sources(sources))
            for title_id, sources in zip(row["card_title_ids"], row["card_sources"], strict=True)
        ),
        built_at=row["built_at"],
        built_recipe=row["built_recipe"],
        attempted_at=row["attempted_at"],
        attempt_outcome=SeedRowOutcome(row["attempt_outcome"]),
        attempt_recipe=row["attempt_recipe"],
    )
