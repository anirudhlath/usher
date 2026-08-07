"""The curated shelf: what an LLM proposed last night, rendered tonight.

**This module hydrates; it never generates.** PRD 06 states it as a constraint
on the class -- *"`LLMRow.build()` only hydrates stored output. Generation
happens in a background job -- never in the request path."* `CurationService`
buys the completion, `curation_validate` decides what survives it, and
`curated_rows` holds the result; everything here turns one of those rows into
cards. A `Row` that could call an `LLMClient` would put a paid network round
trip inside `GET /home`, behind a 30 s cache, once per household per miss.

**The order is the product, and that is what makes this row different from the
other nine.** They all have an ordering and for all of them a wrong one is a
defect; here the ordering *is* the artefact -- it is the only judgement the
completion was bought for, and there is no oracle to recover it from, because
`curated_rows` is the one table in this project whose contents no re-run
reproduces (`domain/curation.py`). So `_title_ids` hands back
`card_title_ids` verbatim and `BaseRow.hydrate` is what preserves it. Nothing
here sorts, and nothing downstream may: `RowCard` deliberately carries no score
for a client to re-sort by (ADR-0006 puts the composition on the server).

**The wrong implementations this module's cases rule out:**

1. **Hydrates in the repository's order.** `TitleRepository.list_by_ids` is one
   `IN (...)` and promises no order at all, so a `build` that answered with what
   the read answered gives a correctly-populated shelf in the wrong sequence, on
   every generation, forever. `BaseRow`'s docstring calls this out for the whole
   family; it is sharpest here.
2. **Sorts** -- by id, by name, by popularity, by year. An alphabetised curated
   row is the one failure mode that looks exactly like a working one.
3. **Raises on a title that vanished.** `curated_rows.card_title_ids` is a
   `uuid[]` and PostgreSQL has no foreign key over array elements, so deleting a
   title leaves a dangling id in every curated row that named it, for up to one
   generation. A `KeyError` here is a 500 on a home screen because one film was
   merged away overnight. The card is dropped, the heading stays, and a shelf
   that loses *every* card builds empty -- a legal value, and the composer's to
   drop (ADR-0023).
4. **Truncates at the first missing title** instead of skipping it: a shelf that
   stops at the gap is populated, plausible and silently short.
5. **Mints its own slug.** `RowCache` keys on `(user_id, slug)`, so a constant
   would make five shelves one entry and the household would see whichever built
   first, five times. The stored slug is positional and zero-padded to the width
   of one generation, which is what makes the composer's `slug` tiebreak the
   model's own ordering rather than an alphabetisation of its prose.
6. **Invents a runtime** from `titles.runtime_minutes`. This row reads no watch
   state -- the pool is unwatched candidates -- so a runtime it did not read is
   one it does not know (ADR-0014), and a card carrying the catalog's figure
   invites every client to compute a fraction against a number that never came
   from the household's own copy.

**A curated slug is unique within one generation and is not a stable name across
generations**, because the padding width is a property of the generation: nine
rows mint `curated-1` and ten mint `curated-01`. That premise is stated once, in
`domain/curation.py`'s `slug` comment, beside the `RowCache` key it is about. It
does not constrain this class -- checked rather than assumed. `LLMRow` reads the
slug it was handed and compares it to nothing, so the only thing the instability
reaches is the cache, where the old width's entry is orphaned rather than
overwritten: a guaranteed miss, a rebuild, and a dead entry its own TTL
reclaims.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from usher.domain.curation import CuratedRow
from usher.domain.rows import DisplayHint, RowFamily
from usher.ports.rows import RowContext
from usher.services.rows.base import BaseRow

# **Five minutes, and PRD 06's "until regenerated" is the artefact's lifetime
# rather than this number.** Read as a TTL that phrase inverts: the stored row
# really is immutable until a generation replaces it, and the replacement is the
# only event that matters, because `RowCache` holds the whole built row under
# `(user_id, slug)` and a generation of the same width re-uses the same slugs.
# So a long TTL does not keep a fresh row fresh, it keeps *last night's* row on
# the screen.
#
# Nothing invalidates that entry. The cache is in-process in the API and the
# curation job runs under `usher work`, a different process; cross-process
# invalidation is M9's, alongside the cross-process `EventPublisher`
# (`services/rows/cache.py` argues both). So this number is the staleness bound,
# and `POST /admin/rows/regenerate` is what turns it into an operator watching a
# screen that has not changed. Five minutes is `RecentlyAddedProvider`'s, for a
# sibling reason: both rows' content moves on an event this process never sees.
_TTL = timedelta(minutes=5)


class LLMRow(BaseRow):
    """One stored `curated_rows` record, ready to render.

    Takes the whole `CuratedRow` rather than its four rendered fields. The
    stored row is the artefact and this is a view of it, so a constructor
    spelling `(slug, title, reason, card_title_ids)` would be four chances to
    fill the wrong slot from a ten-field model and still build something that
    renders -- `curated_row_repository_contract.py` makes the same argument
    about its own fixture. It also keeps `generation_id` and `model_name`
    reachable for anything that later wants to say which night a shelf is from.
    """

    def __init__(self, row: CuratedRow) -> None:
        self._row = row

    @property
    def slug(self) -> str:
        return self._row.slug

    @property
    def title(self) -> str:
        return self._row.title

    @property
    def reason(self) -> str | None:
        # Passed through, `None` included. `curation_validate` turns a blank
        # reason into `None` rather than `""` -- an empty string is a subtitle a
        # client renders as a blank line and cannot tell from a row that had
        # something to say and said nothing -- and this is the first row in the
        # project that can reach that arm, which `api/dto/home.py` records.
        return self._row.reason

    @property
    def family(self) -> RowFamily:
        return RowFamily.CURATED

    @property
    def display_hint(self) -> DisplayHint:
        # Portrait, like seven of the nine. `LANDSCAPE` is what the two resume
        # rows carry, where a still frame is the affordance for "pick up where
        # you left off"; a curated shelf is a set of titles a household has not
        # started, so the poster is the right card.
        return DisplayHint.PORTRAIT

    @property
    def ttl(self) -> timedelta:
        return _TTL

    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        """The model's own ordering, handed back untouched.

        No filter and no sort. A predicate here -- "only the ones still owned",
        "only the unwatched" -- would silently shorten a shelf whose length the
        validator already enforced (`min_cards`), and would do it on a signal
        the generation had when it chose. What legitimately shortens the shelf
        is a title that is *gone*, which `BaseRow.hydrate` handles by dropping
        the card.
        """
        return self._row.card_title_ids


__all__ = ["LLMRow"]
