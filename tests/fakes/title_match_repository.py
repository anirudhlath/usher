"""In-memory `TitleMatchRepository`."""

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
        """Seeded rows first, then whatever `FakeTitleRepository` holds.

        one table, read through two ports.

        Order decides this fake's first-one-wins tie-break, which describes a state
        `ix_titles_tmdb_id_kind` makes unreachable in Postgres anyway.
        """
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
    """A source is free to report `ProviderIds.Tmdb: "unknown"`.

    That is a matching failure, not a pipeline failure.
    """
    try:
        return int(value)
    except ValueError:
        return None
