"""`BaseRow` -- the shared hydration every row does the same way, and the one
place a title id becomes a card.

**Here rather than on `Row`, and `ports/rows.py` argues it at length.** PRD 06
puts `hydrate()` and `empty()` on the ABC; `hydrate` needs a `TitleRepository`,
a `MediaItemRepository` and a `WatchStateRepository` to turn ids into cards, and
a concrete method on a port is a port with a dependency. `ports/` has zero
concrete behaviour today and `Row` is not the place to spend that precedent.

**The two properties that make this file worth having, both of them positional
and both invisible to a membership assertion.**

1. **`hydrate` returns cards in the order it was given ids.** `list_by_ids`
   answers in whatever order the store found rows -- physical order against
   Postgres, insertion order against the fake -- and the *row's order is the
   answer* for every provider here. A hydration that returned the repository's
   order would give a correctly-populated shelf in the wrong sequence, forever,
   on every row at once. `assert title_id in {...}` cannot see it.
2. **A title that vanished between the read and the hydrate is dropped, never
   raised.** `SimilarityService.neighbors_of`'s precedent: a `KeyError` here is
   a 500 on a home screen because one film went away between two statements of
   one request. If every card drops, the row builds *empty* -- which is a legal
   value and a different state from a row that was never proposed
   (`HomeService` has to tell them apart; ADR-0023).

**`Progress` is passed in rather than read here, and that is the N+1 this file
exists to not commit.** Every provider already holds the watch state its own
read returned -- `ContinueWatchingProvider` has `WatchState`s,
`RediscoverProvider` has `RecentWatch`es -- so a `get_for_title` per card would
be one round trip per card to re-read what the caller is holding. A provider
with nothing to say about progress passes nothing and the cards carry the
honest zero-and-`None`.

**Artwork is the one thing `hydrate` reads *for* the provider rather than from
it**, and the asymmetry is the point. Progress and chapters are things the
caller already has in hand; artwork is a thing nobody has, and the only input
the decision needs is `self.display_hint`, which every row already declares.
Pushing it out to ten providers would be ten copies of one mapping and ten
chances for a shelf to paint a backdrop into a 2:3 slot; leaving it here makes
it `+1` statement per shelf, once, with `LLMRow`'s override collapsing the
curated family's four to one exactly as it already does for the other two
reads. `ARTWORK_FOR_HINT` is the mapping and `_artwork` is the hook -- the
latter named after grepping this directory, for the reason `_ownership`'s own
docstring records.
"""

import uuid
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from usher.domain.enums import ImageKind
from usher.domain.episode import Episode
from usher.domain.image import Image
from usher.domain.rows import BuiltRow, DisplayHint, RowCard
from usher.domain.title import Title
from usher.ports.rows import Row, RowContext

#: Which kind of artwork a shelf's cards are painted with, keyed on the shelf's
#: own `display_hint`. **Total over `DisplayHint` rather than over the hints the
#: registry happens to emit** -- `wide` and `square` have no emitter in
#: `services/rows/` today, so a mapping written from the ten providers would be
#: complete-looking and would `KeyError` inside `hydrate` on the first row that
#: used one, which is a 500 on a home screen.
#:
#: The two-way split is the whole of the vocabulary a card needs: a poster is
#: 2:3 and a backdrop is 16:9, and ADR-0006's four hints are two aspect ratios
#: wearing four names. `ImageKind.LOGO`, `STILL` and `PROFILE` are deliberately
#: unreachable from here -- a card paints a poster or a backdrop, an episode
#: still belongs to a chapter view and a profile to `GET /people/{id}`.
ARTWORK_FOR_HINT: Mapping[DisplayHint, ImageKind] = MappingProxyType(
    {
        DisplayHint.PORTRAIT: ImageKind.POSTER,
        DisplayHint.SQUARE: ImageKind.POSTER,
        DisplayHint.LANDSCAPE: ImageKind.BACKDROP,
        DisplayHint.WIDE: ImageKind.BACKDROP,
    }
)


@dataclass(frozen=True, slots=True)
class Progress:
    """What a provider already knows about a household's place in a title.

    Three fields rather than a `WatchState`, because two of the four providers
    that use it never read a `WatchState` at all -- `RediscoverProvider` has a
    `RecentWatch`, which carries no position -- and a DTO that could only be
    built from a row nobody read would push those providers into an N+1 to fill
    a field they know the answer to.

    The defaults are the honest ones for a title nobody has opened: zero
    seconds in (a *true* value, not an ADR-0014 stand-in -- a household that has
    not started a title is genuinely nought seconds into it) and **no** runtime,
    because a runtime this provider did not read is a runtime it does not know.
    """

    position_seconds: int = 0
    runtime_seconds: int | None = None
    played: bool = False


@dataclass(frozen=True, slots=True)
class Chapter:
    """Which episode a card is about, for the two rows that are about one.

    Both fields ride *alongside* `RowCard.title_id`, which stays the series --
    see `RowCard`'s own comment for why. `label` is composed on the server so
    the zero-padding is decided once rather than by each client, which is
    ADR-0006's "the server composes" applied to a string.
    """

    episode_id: uuid.UUID
    label: str


def label(episode: Episode) -> str:
    """`S02E05`, zero-padded to two digits and widening past it.

    Two digits because that is what every client renders and one is what makes
    `S1E5` sort before `S1E10` as text; wider for a series that earns it,
    because truncating would make two different episodes render identically.
    """
    return f"S{episode.season_number:02d}E{episode.episode_number:02d}"


_NOTHING_KNOWN = Progress()


class BaseRow(Row):
    """The shared half of every row in `services/rows/`.

    Subclasses supply the six properties and a `_title_ids(ctx)` returning the
    ids to show, in the order to show them. `build` does the rest.
    """

    @abstractmethod
    async def _title_ids(self, ctx: RowContext) -> Sequence[uuid.UUID]:
        """The shelf's titles, **in the order they are to be rendered**.

        The row's order *is* the answer -- `RowCard` deliberately carries no
        score, so a client has nothing to re-sort by (ADR-0006 puts the
        composition on the server). A provider that returned an unordered set
        here would produce a correct row nobody could tell from a wrong one.
        """

    async def _progress(self, ctx: RowContext) -> Mapping[uuid.UUID, Progress]:
        """What this row knows about the household's place in its own titles.

        Empty by default: a row about the library rather than about the person
        knows nothing about progress and must not pretend to. Overridden by the
        two rows that have already read it.
        """
        return {}

    async def _chapters(self, ctx: RowContext) -> Mapping[uuid.UUID, Chapter]:
        """Which episode each card is about, for the rows that are about one.

        Empty by default, which is the answer for eight of the ten providers:
        a card with no chapter carries `episode_id=None` and
        `episode_label=None` rather than a placeholder, so a client's branch is
        "is this an episode card" and never "is this label meaningful".
        """
        return {}

    async def build(self, ctx: RowContext) -> BuiltRow:
        title_ids = list(await self._title_ids(ctx))
        return BuiltRow(
            slug=self.slug,
            title=self.title,
            reason=self.reason,
            family=self.family,
            display_hint=self.display_hint,
            ttl=self.ttl,
            cards=await self.hydrate(
                ctx, title_ids, await self._progress(ctx), await self._chapters(ctx)
            ),
        )

    async def hydrate(
        self,
        ctx: RowContext,
        title_ids: Sequence[uuid.UUID],
        progress: Mapping[uuid.UUID, Progress] | None = None,
        chapters: Mapping[uuid.UUID, Chapter] | None = None,
    ) -> tuple[RowCard, ...]:
        """Turn ids into cards, **in the order given**, dropping what is gone.

        Four port calls whatever the row's length, never one per card: the
        catalog read, the ownership read, the artwork read, and whatever the
        caller already did. It was three until M9's C6 filled `RowCard.artwork`
        -- `+1 per shelf`, and one for the whole curated family through
        `LLMRow`'s override, which is the same `4 -> 1` the shared catalog and
        ownership reads already buy.

        The early return is load-bearing for the same reason it always was and
        for one more: the composer drops rows that build empty (ADR-0023), so a
        read taken before this guard is one statement per *dropped* shelf on
        every screen.
        """
        if not title_ids:
            return ()
        known = await self._known(ctx, title_ids)
        owned = await self._ownership(ctx, title_ids)
        artwork = await self._artwork(ctx, title_ids)
        seen: set[uuid.UUID] = set()
        cards: list[RowCard] = []
        for title_id in title_ids:
            title = known.get(title_id)
            # Dropped, not raised, and not substituted with a placeholder: a
            # card naming a title the catalog no longer holds is a card a
            # client cannot open.
            if title is None or title_id in seen:
                continue
            seen.add(title_id)
            place = (progress or {}).get(title_id, _NOTHING_KNOWN)
            chapter = (chapters or {}).get(title_id)
            image = artwork.get(title_id)
            cards.append(
                RowCard(
                    title_id=title.id,
                    kind=title.kind,
                    name=title.name,
                    year=title.year,
                    enrichment_state=title.enrichment_state,
                    owned=title_id in owned,
                    position_seconds=place.position_seconds,
                    runtime_seconds=place.runtime_seconds,
                    played=place.played,
                    episode_id=None if chapter is None else chapter.episode_id,
                    episode_label=None if chapter is None else chapter.label,
                    # A title with no image of this row's kind carries `None`
                    # rather than being dropped: absent artwork is an ordinary
                    # state and a card without a poster is still a card a
                    # client can open. The **id** and not the row -- see
                    # `RowCard.artwork` for why a card carries neither the path
                    # nor a URL.
                    artwork=None if image is None else image.id,
                )
            )
        return tuple(cards)

    async def _known(
        self, ctx: RowContext, title_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, Title]:
        """This shelf's titles, by id. One statement, whatever the length.

        **Overridable, and `LLMRow` is the one row that overrides it.** Four
        curated shelves come out of a single `list_for_user`, so the family's
        card ids are all in hand before any of them builds and four separate
        `IN (...)`s is four round trips for one set. A row whose ids arrive one
        shelf at a time -- every other provider here -- has nothing to share
        and inherits this.

        The answer may legitimately be a *superset* of `title_ids`: `hydrate`
        looks each id up rather than iterating what came back, so a shared read
        over a family is indistinguishable from a private read over one shelf.
        What it may never be is a subset, which is why the seam is a method on
        the row rather than a mutable field on the context (`RowContext` is
        frozen precisely so `propose` cannot leave state for `build`).
        """
        rows = await ctx.titles.list_by_ids(list(title_ids))
        return {title.id: title for title in rows}

    async def _ownership(self, ctx: RowContext, title_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of them this household has a copy of. One statement, always.

        `owned_title_ids` rather than `list_for_title` per card: one statement
        for the whole shelf, and its own bound (`episode_id IS NULL`, no
        availability filter) is decided once on that port rather than ten times
        here. Overridable on `_known`'s exact terms, and by the same one row --
        the two reads are a pair, and sharing one without the other would halve
        a saving while doubling the number of places a family's ids are
        assembled.

        **`_ownership` and not `_owned`, which is not a style preference.**
        `FranchiseRow` already carries `self._owned`, a tuple of the collection
        members it was proposed with, so a base-class *method* of that name is
        shadowed by a subclass *attribute* -- and the failure is
        `TypeError: 'tuple' object is not callable` from inside `hydrate`, on
        one provider out of ten, at render time. Measured: naming it `_owned`
        failed 12 cases across three files. A shared hook's name has to be free
        in every subclass, and `grep` over `services/rows/` is the check.
        """
        return await ctx.media_items.owned_title_ids(list(title_ids))

    async def _artwork(
        self, ctx: RowContext, title_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, Image]:
        """One image per title, of the kind **this shelf's hint** asks for.

        `primary_for_titles` rather than `list_for_title` per card: a shelf is
        up to thirty cards and `GET /home` composes ten of them, so the
        per-card shape is three hundred round trips a screen -- which is why
        that port takes a sequence and a caller cannot express the other one.

        **The kind is read off `self.display_hint` and not off the card**, and
        that is ADR-0006's *"the server composes"* applied to a second field. A
        hint is a property of the shelf, so one row cannot disagree with itself
        about its own shape; the poster/backdrop decision is therefore answered
        once per shelf, by the shelf, and a client is never asked to re-decide
        a question the composer already answered. `ARTWORK_FOR_HINT` is the
        mapping, total over `DisplayHint` rather than over the hints the
        registry happens to emit.

        **Overridable on `_known`'s exact terms, and by the same one row.**
        `LLMRow` shares one read across the whole curated family, which is
        `4 -> 1` on the home path; the answer may be a *superset* of
        `title_ids` because `hydrate` looks each id up rather than iterating
        what came back.

        **`_artwork` and not `_images`, `_art` or `_poster`**, and the name was
        chosen by grepping `services/rows/` before it was written rather than
        after. `FranchiseRow` carries `self._owned`, a tuple of its
        collection's members, and naming the ownership hook `_owned` shadowed
        it -- a subclass *attribute* shadows a base-class *method*, so
        `hydrate` raised `TypeError: 'tuple' object is not callable` on one
        provider in ten, at render time, and the failure is invisible in the
        class that declares the method. Measured: 12 failures across three
        files, all from one name. `grep` over this directory is the check, and
        it is cheaper than the twelve cases.

        A title absent from the answer is a title with no image of that kind,
        which is what the port promises -- absent means "no artwork", never
        "not asked", and `hydrate` turns it into `artwork=None` rather than
        into a dropped card.
        """
        return await ctx.images.primary_for_titles(
            list(title_ids), ARTWORK_FOR_HINT[self.display_hint]
        )

    def empty(self) -> BuiltRow:
        """This row with no cards.

        A real method returning a real value, and the reason `BuiltRow` is
        constructible with `cards=()`: an empty row and an absent row are
        different states. Were `build` to return `BuiltRow | None`, "this row
        built and had nothing to show" and "this row was never proposed"
        would collapse into one `None` -- a quiet household and a dead
        provider, which Group I's metrics have to tell apart.
        """
        return BuiltRow(
            slug=self.slug,
            title=self.title,
            reason=self.reason,
            family=self.family,
            display_hint=self.display_hint,
            ttl=self.ttl,
            cards=(),
        )


__all__ = ["ARTWORK_FOR_HINT", "BaseRow", "Chapter", "Progress", "label"]
