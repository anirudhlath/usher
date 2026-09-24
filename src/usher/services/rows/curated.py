"""The curated shelf: what an LLM proposed last night, rendered tonight."""

import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta

from opentelemetry import trace

from usher.domain.curation import SLUG_PREFIX, CuratedRow
from usher.domain.enums import ImageKind
from usher.domain.image import Image
from usher.domain.rows import DisplayHint, RowFamily
from usher.domain.title import Title
from usher.ports.rows import RowContext, RowProvider, ScoredRow
from usher.services.rows.base import ARTWORK_FOR_HINT, BaseRow

# Five minutes. PRD 06: "The curated TTL bounds staleness, not the artefact's
# lifetime" -- the stored row really is immutable until a generation replaces it,
# and the replacement is the only event that matters, because `RowCache` holds
# the whole built row under `(user_id, slug)` and a generation of the same width
# re-uses the same slugs.
_TTL = timedelta(minutes=5)


class _Family:
    """One generation's cards, read once for whichever shelf builds first."""

    __slots__ = ("_artwork", "_known", "_owned", "_title_ids")

    def __init__(self, title_ids: Sequence[uuid.UUID]) -> None:
        # `dict.fromkeys` rather than `set`: two shelves naming one film is ordinary, so
        # the dedup is not optional -- and this spelling keeps the model's own ordering
        # in the `IN (...)` list, where `set` would substitute its hash table's.
        self._title_ids = list(dict.fromkeys(title_ids))
        self._known: dict[uuid.UUID, Title] | None = None
        self._owned: set[uuid.UUID] | None = None
        # Keyed by `ImageKind`, not a bare slot, and that is not speculative
        # generality: `_known` and `_owned` answer questions with one answer per
        # family, and this one has an answer per *hint*.
        self._artwork: dict[ImageKind, dict[uuid.UUID, Image]] = {}

    async def known(self, ctx: RowContext) -> dict[uuid.UUID, Title]:
        if self._known is None:
            rows = await ctx.titles.list_by_ids(self._title_ids)
            self._known = {title.id: title for title in rows}
        return self._known

    async def owned(self, ctx: RowContext) -> set[uuid.UUID]:
        # `is None` and not falsiness on both of these: a generation whose
        # every title was merged away reads back `{}` and a household that owns
        # none of them reads back `set()`, and both are answers rather than
        # misses. Falsiness here would re-read once per shelf for exactly the
        # households the reads are least useful to.
        if self._owned is None:
            self._owned = await ctx.media_items.owned_title_ids(self._title_ids)
        return self._owned

    async def artwork(self, ctx: RowContext, kind: ImageKind) -> dict[uuid.UUID, Image]:
        # `not in` rather than falsiness, for `owned`'s reason one table over:
        # a generation none of whose titles has a poster reads back `{}`, which
        # is an answer rather than a miss, and falsiness would re-read once per
        # shelf for exactly the households the read is least useful to.
        if kind not in self._artwork:
            self._artwork[kind] = await ctx.images.primary_for_titles(self._title_ids, kind)
        return self._artwork[kind]


class LLMRow(BaseRow):
    """One stored `curated_rows` record, ready to render.

    Takes the whole `CuratedRow` rather than its four rendered fields. The
    stored row is the artefact and this is a view of it, so a constructor
    spelling `(slug, title, reason, card_title_ids)` would be four chances to
    fill the wrong slot from a ten-field model and still build something that
    renders. It also keeps `generation_id` and `model_name` reachable for
    anything that later wants to say which night a shelf is from.

    `family` is the generation's shared hydration, and it is optional because a
    shelf on its own is still a shelf. A row built without one gets a `_Family`
    over its own ids, which is exactly the statements `BaseRow` would have
    issued. The invariant the sharing rests on is structural rather than checked:
    `propose` is the only site that passes one, and it builds it from the union
    of the very rows it passes it to, so a shelf's own ids are always inside it.
    """

    def __init__(self, row: CuratedRow, *, family: _Family | None = None) -> None:
        self._row = row
        self._family = _Family(row.card_title_ids) if family is None else family

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

    async def _known(
        self, ctx: RowContext, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Title]:
        # `title_ids` is deliberately ignored: the family's map is a superset
        # of this shelf's ids by construction (see `__init__`), and `hydrate`
        # looks each id up rather than iterating what came back, so the extra
        # entries are unreachable from this row. Narrowing it here would cost a
        # dict comprehension per shelf to hide nothing.
        return await self._family.known(ctx)

    async def _ownership(self, ctx: RowContext, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        # The same superset, for the same reason -- `hydrate` asks
        # `title_id in owned` about this shelf's ids and no others. Named
        # `_ownership` because `FranchiseRow._owned` is an attribute and a method
        # of that name is shadowed by it; `base.py` records why.
        return await self._family.owned(ctx)

    async def _artwork(
        self, ctx: RowContext, title_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, Image]:
        # The third read of the same shape: four shelves out of one `list_for_user`
        # were about to issue four `primary_for_titles` for one set of ids.
        return await self._family.artwork(ctx, ARTWORK_FOR_HINT[self.display_hint])


# Flat, and strictly below 1.0: PRD 06 gives `ContinueWatchingProvider` *"1 row,
# always ranked first"*, that guarantee is `ScoredRow.pinned`, and the score
# ladder is kept in agreement with the pin so the composer's sort is not quietly
# fighting it.
CURATED_SCORE = 0.85

# PRD 06's `CuratedProvider | 0-5 rows`, and the cap is this provider's because
# it is a product bound rather than a safety one. `services.curation_validate`
# deliberately caps nothing -- every card in a hundredth row is still a title the
# household could watch -- so the bound belongs here.
MAX_CURATED_ROWS = 5


class CuratedProvider(RowProvider):
    """0-5 rows: whatever last night's generation left in `curated_rows`."""

    @property
    def slug_prefix(self) -> str:
        return SLUG_PREFIX

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        """This household's newest generation, cut to the budget."""
        stored = await ctx.curated.list_for_user(ctx.user.id)
        kept = stored[:MAX_CURATED_ROWS]
        trace.get_current_span().set_attribute(
            "usher.home.curated.discarded", len(stored) - len(kept)
        )
        # One hydration for the family, built here and read at `build` time. Every
        # card id in the generation arrived in the read above, so the shelves that
        # survive the composer's cap share the statements instead of paying each --
        # and `_Family` reads nothing until asked, so a discarded shelf costs
        # nothing.
        family = _Family([title_id for row in kept for title_id in row.card_title_ids])
        return [ScoredRow(row=LLMRow(row, family=family), score=CURATED_SCORE) for row in kept]


__all__ = ["CURATED_SCORE", "MAX_CURATED_ROWS", "CuratedProvider", "LLMRow"]
