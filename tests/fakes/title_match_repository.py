"""In-memory `TitleMatchRepository`.

**Where this is more forgiving than Postgres, on purpose.** Five places, each
of which the paired `tests/integration/test_title_match_repository.py` run is
what actually closes:

- **It matches on `name.lower()` in Python, so it agrees with `lower(name)`
  by construction.** The real one only agrees if the query names the same
  expression the `ix_titles_name_lower_year` index is built on -- and an
  implementation that lowercases the *probe* and compares against the raw
  column returns identical rows while seq-scanning 1,271,138 of them per
  probe. **Nothing in this file can catch that.** Only an assertion on the
  plan can, and that is what the integration run adds.
- **No unique index on the provider columns**, so two titles here can carry
  the same `(tmdb_id, kind)` and the first one wins silently.
  `ix_titles_tmdb_id_kind` makes that state unreachable in Postgres, so this
  fake's tie-break rule describes a situation the real store cannot be in.
- **No integer column, so a non-numeric TMDb value is a Python `int()` that
  raises where Postgres would raise `invalid input syntax for type
  integer`.** Both are avoided the same way -- filter before binding -- but
  they are different exceptions arriving at different layers.
- **No `year` column type**, so nothing here distinguishes a `NULL` year from
  a zero. Postgres's `BETWEEN` over a `NULL` yields `NULL`, which is what
  makes `test_a_title_with_no_year_is_never_matched_by_name_alone` free
  there and an explicit `is not None` here.
- **No session and no autoflush**, so nothing here can surface some other
  caller's pending, invalid state as this read's error.

**One divergence has been closed rather than documented**, because it made a
*correct* service fail rather than a wrong one pass. `titles` is one table,
and `TitleRepository.add` flushes -- so a stub the match stage just created
is visible to the very next `TitleMatchRepository` read. Two independent
dicts made it invisible forever, and `IngestService`'s second walk of a
series it had itself stubbed then missed the ladder, re-created the stub,
conflicted on `ix_titles_tvdb_id`, and had nothing left to look the winner up
with. Passing a `FakeTitleRepository` to the constructor makes the two read
the same rows. Leaving it out is still useful and still meaningful: it models
a read that missed a write another worker had already committed, which is
exactly the race `MatchService`'s conflict handler exists for and the only
way to produce one deterministically.

`calls` and `reset_calls()` are test-double affordances rather than port
methods: `MatchService`'s scale case asserts that a page of 500 items costs
a bounded number of *round trips*, and nothing about the answers this fake
returns can express that. A real query counter against Postgres would need
an event listener on the engine; the property being pinned is the service's,
not the store's, so it is counted here.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.ids import new_id
from usher.ports.ingest import NameYearProbe, ProviderRef
from usher.ports.repository import TitleMatchRepository


@dataclass(frozen=True, slots=True)
class _Row:
    id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None
    tvdb_id: int | None
    enrichment_state: EnrichmentState


class FakeTitleMatchRepository(TitleMatchRepository):
    def __init__(self, titles: FakeTitleRepository | None = None) -> None:
        self._rows: list[_Row] = []
        self._titles = titles
        self.calls = 0

    def _all_rows(self) -> list[_Row]:
        """Seeded rows first, then whatever `FakeTitleRepository` holds --
        one table, read through two ports. Order decides this fake's
        first-one-wins tie-break, which describes a state
        `ix_titles_tmdb_id_kind` makes unreachable in Postgres anyway."""
        if self._titles is None:
            return self._rows
        return self._rows + [
            _Row(
                id=title.id,
                kind=title.kind,
                name=title.name,
                year=title.year,
                tmdb_id=title.tmdb_id,
                imdb_id=title.imdb_id,
                tvdb_id=title.tvdb_id,
                enrichment_state=title.enrichment_state,
            )
            for title in self._titles.stored()
        ]

    def reset_calls(self) -> None:
        self.calls = 0

    async def given_title(
        self,
        *,
        kind: TitleKind,
        name: str,
        year: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
        title_id: uuid.UUID | None = None,
        enrichment_state: EnrichmentState = EnrichmentState.SKELETON,
    ) -> uuid.UUID:
        # `title_id` lets a case seed this store and `FakeTitleRepository`
        # with the *same* id -- which is one row in Postgres and two here.
        # Only a test modelling a race between them needs it.
        row = _Row(
            id=title_id or new_id(),
            kind=kind,
            name=name,
            year=year,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            tvdb_id=tvdb_id,
            enrichment_state=enrichment_state,
        )
        self._rows.append(row)
        return row.id

    async def match_by_provider_ids(
        self, refs: Sequence[ProviderRef]
    ) -> dict[ProviderRef, uuid.UUID]:
        self.calls += 1
        rows = self._all_rows()
        resolved: dict[ProviderRef, uuid.UUID] = {}
        # `dict.fromkeys` rather than `set`: deduplicates while keeping the
        # caller's order, so a failure reads in the order the batch was given.
        for ref in dict.fromkeys(refs):
            match ref.provider:
                case "tmdb":
                    # ADR-0011: TMDb's two id spaces overlap on 26,968 ids, so
                    # a ref with no kind names nothing rather than one of two.
                    number = _as_int(ref.value)
                    if number is None or ref.kind is None:
                        continue
                    found = next(
                        (r for r in rows if r.tmdb_id == number and r.kind is ref.kind), None
                    )
                case "imdb":
                    # `tt` ids are one global namespace, so `ref.kind` is
                    # redundant here and deliberately not filtered on.
                    found = next((r for r in rows if r.imdb_id == ref.value), None)
                case "tvdb":
                    number = _as_int(ref.value)
                    if number is None:
                        continue
                    found = next((r for r in rows if r.tvdb_id == number), None)
                case _:
                    # A provider this catalog has no column for. "None that I
                    # can tell" is the honest answer; raising would fail a
                    # batch of 5,000 items over one source's stray scraper.
                    continue
            if found is not None:
                resolved[ref] = found.id
        return resolved

    async def match_by_name_year(
        self, probes: Sequence[NameYearProbe]
    ) -> dict[NameYearProbe, uuid.UUID]:
        self.calls += 1
        rows = self._all_rows()
        resolved: dict[NameYearProbe, uuid.UUID] = {}
        for probe in dict.fromkeys(probes):
            # A bare name is not an identity claim at 1,271,138 titles.
            if probe.year is None:
                continue
            candidates = [
                row
                for row in rows
                if row.kind is probe.kind
                and row.name.lower() == probe.name.lower()
                and row.year is not None
                and abs(row.year - probe.year) <= 1
            ]
            # Exactly one, or nothing. PRD 03 stage 5: no *confident* match
            # means the review queue, not a coin flip between two remakes.
            if len(candidates) == 1:
                resolved[probe] = candidates[0].id
        return resolved

    async def enrichment_states(
        self, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, EnrichmentState]:
        self.calls += 1
        wanted = set(title_ids)
        # An absent key means "no such title"; the caller iterates its own ids.
        return {row.id: row.enrichment_state for row in self._all_rows() if row.id in wanted}


def _as_int(value: str) -> int | None:
    """A source is free to report `ProviderIds.Tmdb: "unknown"`. That is a
    matching failure, not a pipeline failure."""
    try:
        return int(value)
    except ValueError:
        return None
