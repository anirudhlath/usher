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

**And the wrong `CuratedProvider`s, which is the other half of this module and
the tenth member of the registry (M8 task 15):**

7. **Reads the table rather than the household.** `list_for_user(ctx.user.id)`
   is the whole of the scope, and the `user_id` predicate one layer down is the
   one this subsystem's Postgres read actually got wrong -- it survived all
   fourteen of its cases, because a `generation_id` is minted per generation
   and every fixture gave each household a fresh one.
8. **Proposes the whole generation.** `curation_validate` deliberately caps
   nothing, so the `0-5 rows` bound is PRD 06's, is a product bound, and lives
   here as `MAX_CURATED_ROWS`.
9. **Takes the wrong five** -- the last five, or the five that sort first by
   heading. The ordering is the only judgement the completion was bought for,
   so the survivors are the model's first five and nothing here sorts.
10. **Scores them apart.** A per-row decrement is a second spelling of an order
    the positional slug already carries; see `CURATED_SCORE`.
11. **Generates.** A provider holding an `LLMClient` puts a paid network round
    trip inside `GET /home`, and it would *work* --
    `test_no_provider_reaches_a_port_the_context_does_not_carry` cannot see it,
    because `usher.ports.llm` is under `usher.ports` and passes that scan
    whole. Asserted on this module's imports and its own source text.

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


class _Family:
    """One generation's cards, read once for whichever shelf builds first.

    **This is the only thing in `services/rows/` that knows two rows can share
    a read, and it exists because only this provider mints two rows from one
    read.** `propose` returns up to `MAX_CURATED_ROWS` shelves out of a single
    `list_for_user`, so every card id in the family is in hand before anything
    builds; the composer then builds `HomeService._MAX_PER_FAMILY` of them, and
    each `BaseRow.build` was issuing its own catalog read and its own ownership
    read -- **eight statements for the ~22 distinct ids one generation names**,
    inside one request, on a screen whose whole budget is 400 ms. M9's C6 adds
    a third read of the same shape (`primary_for_titles`), so the unshared
    spelling would now be **twelve**; it is three.

    **Nothing is read here until a shelf asks.** Constructed in `propose` and
    populated by the first `build`, which is what keeps a shelf the per-family
    cap discards free: hydrating at propose time would read for rows nobody
    sees, which is the one-phase design ADR-0023 rejected arriving inside a
    single provider.

    **The union is over what was proposed, not over what is built**, so the
    first shelf to build pays for at most one cut shelf's ids as well -- `IN
    (...)` over ~27 ids instead of ~22, against seven statements saved. The
    first builder cannot know which of its siblings the composer will reach,
    and a memo filled per builder is the eight statements again.

    **A cached row never touches this.** `HomeService._build` returns
    `RowCache.get_row`'s hit before calling `build`, so the memo is simply not
    consulted; and a second composition re-proposes, minting new rows over a
    new `_Family`. There is no path by which this outlives the `propose` that
    made it, which is what keeps it off the frozen context and out of the row
    cache's lifetime.
    """

    __slots__ = ("_artwork", "_known", "_owned", "_title_ids")

    def __init__(self, title_ids: Sequence[uuid.UUID]) -> None:
        # `dict.fromkeys` rather than `set`: two shelves naming one film is
        # ordinary, so the dedup is not optional -- and this spelling keeps the
        # model's own ordering in the `IN (...)` list, where `set` would
        # substitute its hash table's. Nothing downstream reads that order
        # (`hydrate` looks each id up), which is exactly why the cheap
        # order-preserving spelling is the one to use: the day something does,
        # it will be the generation's order rather than an arbitrary one.
        self._title_ids = list(dict.fromkeys(title_ids))
        self._known: dict[uuid.UUID, Title] | None = None
        self._owned: set[uuid.UUID] | None = None
        # **Keyed by `ImageKind`, not a bare slot**, and that is not
        # speculative generality: `_known` and `_owned` answer questions with
        # one answer per family, and this one has an answer per *hint*. Every
        # `LLMRow` is `PORTRAIT` today, so the dict holds one entry and the
        # read is `4 -> 1` exactly as the other two are -- but a shelf whose
        # hint moved would otherwise be served the poster memo under a
        # backdrop's name, which is the one artwork defect that renders
        # perfectly.
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
    renders -- `curated_row_repository_contract.py` makes the same argument
    about its own fixture. It also keeps `generation_id` and `model_name`
    reachable for anything that later wants to say which night a shelf is from.

    **`family` is the generation's shared hydration, and it is optional
    because a shelf on its own is still a shelf.** `propose` hands one
    `_Family` to every row it returns; a row built without one gets a `_Family`
    over its own ids, which is exactly the two statements `BaseRow` would have
    issued. The invariant the sharing rests on is structural rather than
    checked: `propose` is the only site that passes one, and it builds it from
    the union of the very rows it passes it to, so a shelf's own ids are always
    inside it.
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
        # `_ownership` because `FranchiseRow._owned` is an attribute and a
        # method of that name is shadowed by it; `base.py` records the
        # measurement.
        return await self._family.owned(ctx)

    async def _artwork(
        self, ctx: RowContext, title_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, Image]:
        # The third read of the same shape, and the third `4 -> 1`: four
        # shelves out of one `list_for_user` were about to issue four
        # `primary_for_titles` for one set of ~22 ids. The kind is still this
        # shelf's own -- `ARTWORK_FOR_HINT[self.display_hint]`, not a constant
        # -- so the memo is asked the question this row would have asked, and
        # the family answers it once.
        return await self._family.artwork(ctx, ARTWORK_FOR_HINT[self.display_hint])


# **0.85, flat, and the two things it has to be are different kinds of fact.**
#
# *Strictly below 1.0* is `test_no_provider_but_continue_watching_can_reach_the_
# top_score`, a registry invariant: PRD 06 gives `ContinueWatchingProvider`
# *"1 row, always ranked first"*, that guarantee is `ScoredRow.pinned`, and the
# score ladder is kept in agreement with the pin so the composer's sort is not
# quietly fighting it. Nothing here is pinned; a second pinned provider would
# be two rows claiming one position, which is a guarantee that becomes a tie.
#
# *Where in the band* is a product judgement, and this is it. The band the
# comparable-scale invariant leaves open is [0.3, 1.0); the band the ladder case
# under it actually enforces is **(0.80, 0.90) exclusive**, measured 2026-08-07
# by planting each endpoint -- `0.805` and `0.899` pass all 2,759 unit cases,
# `0.80` and `0.90` fail `test_a_curated_shelf_outranks_every_discovery_row_and_
# neither_row_about_intent` on its own line. So the judgement this comment
# argues is which number inside a hundredth-wide window, and the window is not
# the one an earlier draft of this sentence named. The two rows
# above are about **intent** -- Continue Watching is a title the household is
# in the middle of and Next Up (0.90) is the next episode of one they are
# watching -- and a shelf a model proposed overnight must never outrank
# something somebody is literally watching. Every other provider's ceiling is a
# **discovery** claim computed from one signal: one seed's neighbour list
# (0.80), one library event (0.75), one genre's lift (0.70), one recurring face
# (0.65), the calendar (0.60), one collection (0.55), one crossing of the
# two-year line (0.35). This one reads the household's whole recent history
# against a 200-title pool, and it is the only row on the screen that cost
# money -- so it sits above all seven and below both intent rows.
#
# **Being outranked here is not "shown later", it is "not shown".** The screen
# is ten rows and a rich household proposes more than ten; a curated score
# under `BecauseYouWatchedProvider`'s 0.80 would fill the screen before the
# shelves this milestone bought were reached, every night, on exactly the
# households the completion is most worth buying for. That is spend with no
# screen to show for it, which is the failure PRD 10's dashboard 5 exists to
# make visible and which a constant is cheaper than.
#
# **Flat, and that is a decision rather than an omission.** The composer ranks
# on `(-score, slug)`, and a curated slug is positional and zero-padded to the
# width of its generation (`domain/curation.py`), so the *tie* is already the
# model's own ordering, stated exactly once.
# `BecauseYouWatchedProvider._SEED_STEP` exists for the opposite reason: its
# slugs carry a seed id, so its tie alphabetises. A decrement here would be a
# second spelling of an order the slug already carries, and two spellings of
# one order are two things that can disagree.
CURATED_SCORE = 0.85

# **PRD 06's `CuratedProvider | 0-5 rows`, and the cap is this provider's
# because it is a product bound rather than a safety one.**
# `services.curation_validate` deliberately caps nothing -- every card in a
# hundredth row is still a title the household could watch, so nothing about
# the *stored* generation is wrong at any length -- and the amendment that
# settled that names this constant as where the bound belongs.
#
# `curation_prompt.MAX_ROWS` asks the model for at most five and is a
# different kind of number: a request, which a model may ignore. This is what a
# household is shown when it does. The two are allowed to differ, in one
# direction only, and `test_the_shelf_budget_is_never_smaller_than_what_the_
# prompt_asks_for` is the guard on that direction -- **the direction that needs
# a code change**. The direction that happens at *runtime*, with no code change
# at all, is a model ignoring `MIN_ROWS`/`MAX_ROWS`, which `curation_validate`
# deliberately does not cap; `propose` counts what it drops for that reason.
#
# `HomeService._MAX_PER_FAMILY` is 4 and `CURATED` is a family, so at most four
# of these five ever reach a screen. That is not a reason to make this four:
# the family cap is about crowding *between* families on one screen and moves
# with the composer, and PRD 06's budget is about what this provider is
# entitled to propose. `BecauseYouWatchedProvider` proposes three under the
# same cap for the same reason.
MAX_CURATED_ROWS = 5


class CuratedProvider(RowProvider):
    """0-5 rows: whatever last night's generation left in `curated_rows`.

    **The tenth provider, and the only one whose signal no re-run
    reproduces.** The other nine ask the catalog or the household a question
    with a predicate and get the same answer twice; this one reads an artefact
    that was bought, validated and stored hours ago by a different process.
    That is the whole of the difference, and it is why `propose` is one port
    call with no arithmetic in it: there is nothing here to recompute and
    nothing to decide that the generation did not already decide.

    **It has no constructor argument at all, unlike `row_providers`' one
    deployment fact and unlike the eight siblings that take a tuning `limit`.**
    Whether an LLM is configured is not visible here and must not be: a
    deployment with `USHER_LLM_ENABLED=false` has an empty `curated_rows` and
    therefore no curated shelves, which is the same answer this provider gives
    a household whose first generation has not run yet -- fewer rows, not worse
    rows (ADR-0022's phrase, one subsystem over). A flag would make those two
    states different code paths with one observable outcome.

    **And no `limit` either, which is where this sentence and the code
    disagreed for one commit.** It shipped as
    `def __init__(self, *, limit: int = MAX_CURATED_ROWS)` three lines under a
    docstring saying there was no constructor argument -- the *restated fact*
    failure `row_providers`' own docstring spends a paragraph retiring, in a
    new module, at three lines' distance. Nothing in `src/` or `tests/` ever
    passed it. Deleted rather than the sentence, because the eight siblings'
    `limit` is a **tuning dial** over card counts and candidate pools while
    this number is PRD 06's `0-5 rows` **product bound**, argued as such by
    `MAX_CURATED_ROWS`' own comment -- and a per-instance override of a product
    bound is a sixth curated shelf nobody decided to paint. One spelling, on
    the constant, where the argument for it already lives.
    `test_this_provider_takes_no_constructor_argument` is what makes the
    paragraph checkable rather than merely restated.
    """

    @property
    def slug_prefix(self) -> str:
        return SLUG_PREFIX

    async def propose(self, ctx: RowContext) -> Sequence[ScoredRow]:
        """This household's newest generation, cut to the budget.

        **`ctx.user.id`, and the scope is the whole of the correctness here.**
        `PostgresCuratedRowRepository.list_for_user` had its `user_id`
        predicate deleted in a sweep and passed all fourteen of its cases,
        because every fixture minted a fresh `generation_id` per household and
        the two predicates were then equally selective. One layer up the same
        mistake crosses a *screen* rather than a count: another household's
        headings, their reasons, and a shelf of films this one has already
        watched, rendered as a personal recommendation.

        **The slice is `[:MAX_CURATED_ROWS]`, taken from the read's own
        order.** `list_for_user` orders by `position`, which is the model's
        ordering and the only judgement the completion was bought for -- so the
        shelves that survive the budget are its first five and not the five that
        sort first by heading, by id or by anything else. Nothing here sorts.

        **What the slice throws away is counted, and it is the one drop on this
        screen that `ProviderReport` structurally cannot see.** That report
        splits `proposed`/`selected`/`built` precisely because PRD 06's "drops
        any that build empty" is otherwise invisible, so the composer's
        family-cap drop of curated row #5 reads as `proposed 5, selected 4`. But
        `proposed` is the **post**-cut number: a nine-shelf generation prints
        `curated 5` and the four bought-and-stored shelves it discarded appear
        nowhere. `MAX_CURATED_ROWS`' comment calls that "spend with no screen to
        show for it, which is exactly what PRD 10's dashboard 5 exists to make
        visible", and a comment naming a gap is not an instrument.

        So the count goes on the ambient span -- `usher.home.curated.discarded`
        on `home.compose`, PRD 10's span attribute for it -- rather than on a
        third metric: PRD 10 argues `usher.curation.rows`/`.dropped` are the
        milestone's only two, and "how many did *this* composition discard" is
        the same shape as "how many rows did *this* generation produce", which
        that document already puts on a span. **Recorded every time, zeros
        included**, for the reason `usher.curation.dropped` exports every reason
        every time: a value absent from the export is indistinguishable from a
        value nobody records. Outside a span the set is a no-op, which is what
        `get_current_span` returning `INVALID_SPAN` means.

        **A slug is unique inside the generation this read answered and is not
        a name across two.** The padding width is a property of the generation,
        so nine rows mint `curated-1` and ten mint `curated-01`; this provider
        reads what it was handed and compares it to nothing, which is what
        keeps that instability confined to `RowCache`, where the old width's
        entry is orphaned rather than overwritten. The one copy of that
        argument is `domain/curation.py`'s `slug` comment.
        """
        stored = await ctx.curated.list_for_user(ctx.user.id)
        kept = stored[:MAX_CURATED_ROWS]
        trace.get_current_span().set_attribute(
            "usher.home.curated.discarded", len(stored) - len(kept)
        )
        # **One hydration for the family, built here and read at `build`
        # time.** Every card id in the generation arrived in the read above, so
        # the shelves that survive the composer's cap can share two statements
        # instead of paying two each -- and because `_Family` reads nothing
        # until asked, a shelf the cap discards still costs exactly nothing.
        # Over the ids of `kept` rather than of `stored`: a shelf that is not
        # proposed cannot be built, and widening the `IN (...)` on its behalf
        # would be paying for the discard this method just counted.
        family = _Family([title_id for row in kept for title_id in row.card_title_ids])
        return [ScoredRow(row=LLMRow(row, family=family), score=CURATED_SCORE) for row in kept]


__all__ = ["CURATED_SCORE", "MAX_CURATED_ROWS", "CuratedProvider", "LLMRow"]
