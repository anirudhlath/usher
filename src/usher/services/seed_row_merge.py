"""Merging a seed's three candidate lists into one row of cards."""

import uuid
from collections.abc import Mapping, Sequence
from fractions import Fraction

from usher.domain.seed_rows import SEED_ROW_CARDS, CardSource, SeedRowCard

RRF_K = 60


def merge_seed_row(
    *,
    seed_id: uuid.UUID,
    named: Sequence[uuid.UUID],
    recommended: Sequence[uuid.UUID],
    close: Sequence[uuid.UUID],
    votes: Mapping[uuid.UUID, int],
    limit: int = SEED_ROW_CARDS,
) -> tuple[SeedRowCard, ...]:
    """Odd positions take the next Named title, even ones the next Merged title.

    Merged is every Named or Recommended title by reciprocal rank fusion over all three
    lists, then votes, then id. A title only Close found is never a card. Each list is
    ranked from 1 after the seed and any repeat of a title are removed.
    """
    lists = {
        CardSource.NAMED: _ranked(named, seed_id),
        CardSource.RECOMMENDED: _ranked(recommended, seed_id),
        CardSource.CLOSE: _ranked(close, seed_id),
    }
    # Exact fractions, so two titles with the same terms in another order really tie.
    scores: dict[uuid.UUID, Fraction] = {}
    sources: dict[uuid.UUID, set[CardSource]] = {}
    for source, ranked in lists.items():
        for rank, title_id in enumerate(ranked, start=1):
            scores[title_id] = scores.get(title_id, Fraction(0)) + Fraction(1, RRF_K + rank)
            sources.setdefault(title_id, set()).add(source)

    candidates = dict.fromkeys([*lists[CardSource.NAMED], *lists[CardSource.RECOMMENDED]])
    merged = sorted(candidates, key=lambda t: (-scores[t], -votes.get(t, 0), t))

    chosen: list[uuid.UUID] = []
    taken: set[uuid.UUID] = set()
    while len(chosen) < limit:
        pick: uuid.UUID | None = None
        if len(chosen) % 2 == 0:
            pick = _next_unused(lists[CardSource.NAMED], taken)
        if pick is None:
            pick = _next_unused(merged, taken)
        if pick is None:
            break
        chosen.append(pick)
        taken.add(pick)
    return tuple(SeedRowCard(title_id=t, sources=frozenset(sources[t])) for t in chosen)


def _ranked(titles: Sequence[uuid.UUID], seed_id: uuid.UUID) -> list[uuid.UUID]:
    """`titles` in order without the seed and without a title's second appearance."""
    return list(dict.fromkeys(t for t in titles if t != seed_id))


def _next_unused(titles: Sequence[uuid.UUID], taken: set[uuid.UUID]) -> uuid.UUID | None:
    return next((t for t in titles if t not in taken), None)


__all__ = ["RRF_K", "merge_seed_row"]
