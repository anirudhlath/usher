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
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from usher.domain.enums import TitleKind
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


class FakeTitleMatchRepository(TitleMatchRepository):
    def __init__(self) -> None:
        self._rows: list[_Row] = []

    async def given_title(
        self,
        *,
        kind: TitleKind,
        name: str,
        year: int | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
    ) -> uuid.UUID:
        row = _Row(
            id=new_id(),
            kind=kind,
            name=name,
            year=year,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            tvdb_id=tvdb_id,
        )
        self._rows.append(row)
        return row.id

    async def match_by_provider_ids(
        self, refs: Sequence[ProviderRef]
    ) -> dict[ProviderRef, uuid.UUID]:
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
                        (r for r in self._rows if r.tmdb_id == number and r.kind is ref.kind), None
                    )
                case "imdb":
                    # `tt` ids are one global namespace, so `ref.kind` is
                    # redundant here and deliberately not filtered on.
                    found = next((r for r in self._rows if r.imdb_id == ref.value), None)
                case "tvdb":
                    number = _as_int(ref.value)
                    if number is None:
                        continue
                    found = next((r for r in self._rows if r.tvdb_id == number), None)
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
        resolved: dict[NameYearProbe, uuid.UUID] = {}
        for probe in dict.fromkeys(probes):
            # A bare name is not an identity claim at 1,271,138 titles.
            if probe.year is None:
                continue
            candidates = [
                row
                for row in self._rows
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


def _as_int(value: str) -> int | None:
    """A source is free to report `ProviderIds.Tmdb: "unknown"`. That is a
    matching failure, not a pipeline failure."""
    try:
        return int(value)
    except ValueError:
        return None
