"""What a composed home screen is made of, once built.

`domain/` imports nothing -- not `ports/`, which imports *it*. `SearchResult`
and `SimilarTitle` sit on this side of that line for the same reason these do:
they are what a client renders and what `usher home` prints, the last thing in
the pipeline rather than the first. PRD 06 says it independently -- *"`BuiltRow`
and `RowCard` are Pydantic DTOs ... the DTOs stay pure"*.

**`ScoredRow` is deliberately not here**, and the plan's own file structure put
it here. It carries the `Row` it scores, `Row` is an ABC in `usher.ports.rows`,
and `usher.domain` importing `usher.ports` breaks the `hexagonal layering`
contract -- verified by planting it and watching `lint-imports` report
`6 kept, 1 broken`. The alternative that keeps it in `domain/` is a
`row_slug: str` plus a `dict[str, Row]` on the composer: a lookup table, a
second source of truth, and a `KeyError` waiting for the first provider that
proposes two rows under one slug -- which `FranchiseProvider`, at one row per
franchise, is well placed to be.

**`artwork` is here now, and M7's boundary call 3 is what it was waiting for.**
PRD 06 describes `RowCard` as carrying *"artwork refs"*; M7 shipped no such
field, absent rather than null, because there was no `Image` table, no `images`
column and not even a `poster_path` on `titles` -- *"an always-null field is a
client-side branch that never takes its other arm, and the day M9 fills it
every client that shipped against the null already renders without it."* M9's
C2 built the table and the port, C3 fills it from `raw_payloads` with no second
network call, and C4/C5 serve the bytes; so the field arrives **populated**,
which is the condition the refusal named, and a client that shipped against its
absence is untouched because `extra="forbid"` guards additions rather than
readers.

**One image id, chosen server-side, and each of those three words is a
decision.** *One*, because a list puts the choice back on the client and
ADR-0006 puts composition on the server. An *id* rather than a URL or a path,
because a URL bakes the CDN base and the ladder rung into a screen a client
caches, and a path is provider vocabulary -- `GET /images/{id}` is the one
place either is decided (ADR-0032). *Server-side*, because the choice is keyed
on the **row's** `display_hint` (a poster for `portrait`/`square`, a backdrop
for `landscape`/`wide`) and a card cannot see its row's hint; the mapping lives
once, on `services/rows/base.py:ARTWORK_FOR_HINT`.

**`None` is a true fact and not an ADR-0014 stand-in**, which is why it is
absent from the enumeration below. A site on that list is a field where a
*falsy* value would be read as a measurement -- a zero runtime, a zero cosine.
There is no UUID that means "no artwork", so nothing here is standing in for
anything: `None` says the catalog holds no poster or backdrop for this title,
which is the ordinary state of a title nothing has derived yet, and a client's
branch on it is a real branch with a real other arm.

**ADR-0014 -- absence is not zero -- and the enumeration of its sites, because
nothing in this repository enumerated them and the ordinals were being
incremented by guesswork.** Counted over `src/` at M7:

1. `SourceWatchState.play_count`/`last_played_at` from a listing walk --
   `ports/source.py:126`, `adapters/emby/mapping.py:474`,
   `services/watch_sync.py:445`, `db/repositories/watch_state.py:121`
2. the push `UserDataChanged` payload -- `adapters/emby/mapping.py:512`,
   `ports/source.py:174`
3. `EnrichService._changes` -- `services/enrich.py:331`
4. `SearchService._popularity_term` -- `services/search.py:476`, which calls
   itself *"a fourth place"*
5. `SimilarityService._jaccard` -- `services/similar.py:266`
6. the degenerate-embedding `NULL`, which applies the rule without citing the
   ADR -- `services/index.py`, `db/models/search.py`, `ports/search.py`
7. **`RowCard.runtime_seconds`, here.** A source may not report a runtime, and
   `db/repositories/watch_state.py:133` `COALESCE`s it on merge for exactly
   that reason. Zero is not "no runtime" -- it is a divisor that renders every
   partially-watched title as finished.
8. `GenomeRepository.get_pair` returning `None` for a title with no genome row,
   and for two rows from different genome releases -- `ports/repository.py`.
9. **`NeighborCandidate.tags` -- `ports/repository.py`, `services/similar.py`.**
   The genome cosine of a *pair*, `None` when either side has no vector. The
   sharpest site on this list: every genome component is positive, so the true
   cosine of any real pair is bounded above zero (measured floor **0.2556**
   over 268,157,000 pairs), which makes `0.0` a value **real data cannot
   produce** rather than merely an unevidenced reading.

**A site was removed rather than added, which had not happened before.**
`RowContext.taste` was site 8 in this list at Group A. No provider ever read
it, and on the request path it was structurally `None` -- so M7's Task 35 group
deleted the field, and the ordinals below it moved up. That is the reason this
enumeration exists: the ordinals were being incremented by guesswork, and a
list that can only grow is a list that lies the first time something is
deleted. **Count against this list; never against a number read out of a
plan.**
"""

import uuid
from datetime import timedelta
from enum import StrEnum

from pydantic import Field

from usher.domain.base import DomainModel
from usher.domain.enums import EnrichmentState, TitleKind


class RowFamily(StrEnum):
    """PRD 06's row *family*, and the key the composer's diversity constraint
    is stated in: *"no three consecutive similarity rows; cap per family"*. A
    row that could not name its family would make that constraint
    inexpressible, which is why this is a typed vocabulary rather than a
    convention over slugs -- `because-you-watched-<seed>` is per-seed, so a
    slug-keyed rule couples the composer to the catalog.

    **Three members, and the third arrived with its emitter rather than ahead
    of it.** PRD 06's family table lists `SourceRow`, `SimilarityRow` and
    `LLMRow`; M7's boundary call 2 gave the whole
    `LLMRow`/`CuratedProvider`/`curated_rows` family to M8, and `CURATED` was
    deliberately **not** pre-declared -- a cap on a family with no members is a
    branch nothing can reach, so the first thing M8 would discover is whether
    that branch was ever right. It cost one line in the diff that added
    `services/rows/curated.py:LLMRow`, which is the only thing that emits it.

    **What it turned out to make reachable, because that is the question the
    deferral was protecting.** Not the per-family cap: the two cases that
    exercise it -- `test_services_home.py::test_no_family_exceeds_its_cap_even_
    when_it_proposes_the_top_scores` and `::test_a_proposal_the_cap_declined_is_
    selected_zero_rather_than_absent` -- have each proposed **eight
    `SIMILARITY`** rows since M7, and the cap has never cared how many families
    exist. It is `HomeService`'s `_MAX_ROWS`. With two families the longest
    screen the composer could return was **nine** rows -- one pinned plus four
    per family -- and the *registry* could only reach **eight** of those,
    because `BecauseYouWatchedProvider` is the only `SIMILARITY` emitter and
    its `_MAX_SEEDS` is 3; both are under the ceiling of ten, so it truncated
    nothing at any input. With three families, thirteen candidates get past the
    cap and it truncates three of them. That "one pinned" term is a property of
    the *registry* and not of the composer -- `_select` sets every pinned
    candidate aside before the cap with no bound of its own -- and
    `test_rows_invariants.py::test_continue_watching_is_the_only_provider_that_
    pins_and_it_pins_one_row` is what holds it. `services/home.py` carries the
    same note in its **module docstring**, and `test_services_home.py::test_the_
    default_row_ceiling_is_reachable_now_that_a_third_family_exists` is where
    the branch is pinned.

    Named `RowFamily` and not `RowKind`, and there is exactly one of it. The
    milestone plan used both names for this one concept -- `RowKind` in the
    task body, `RowFamily` in the cross-group handoff that Group I's composer
    is written against. PRD 06's own word is "family", the constraint that
    reads it is phrased "per family", and `RowCard.kind` is already a
    `TitleKind` in this module, so `kind` twice would be two unrelated
    vocabularies one field apart.
    """

    SOURCE = "source"
    SIMILARITY = "similarity"
    CURATED = "curated"


class DisplayHint(StrEnum):
    """ADR-0006's only concrete client vocabulary, and its whole of it:
    *"Rows carry a display **hint** (`portrait | landscape | wide | square`)
    but never a layout."*

    Closed on purpose. The way this goes wrong is a fifth member -- `HERO`, or
    `GRID_3_COLUMN` -- which is a layout wearing a hint's name and which the
    server has no business specifying. A hint says what shape a card *is*; a
    layout says where a client should put it.
    """

    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"
    WIDE = "wide"
    SQUARE = "square"


class RowCard(DomainModel):
    """One title on one shelf, hydrated and ready to render.

    Field names deliberately match `SearchResult`'s where they mean the same
    thing (`title_id`, `kind`, `name`, `year`, `owned`): two DTOs meaning the
    same thing under different names is how a client acquires two renderers
    for one concept. `owned` inherits `SearchResult`'s justification verbatim
    -- PRD 05 requires unowned results to be *"clearly marked"*, and a client
    that had to ask a second question to render the badge would either ask it
    per card or not render it.

    **Not carried: `popularity` and `score`.** A card is not ranked within its
    row by anything a client can see; the row's order *is* the answer, and a
    visible per-card score invites a client to re-sort -- which is precisely
    the composition ADR-0006 put on the server. **Not carried: `rating`.**
    `watch_states` has no rating column at all, so a rating on a card is a
    field with no source. **Carried since M9: `artwork`** -- one image id,
    chosen against the row's hint; see the module docstring for why it is an
    id, why there is one of it, and why its `None` is not an ADR-0014 site.

    The progress pair is two facts rather than one fraction. `runtime_seconds`
    is `int | None` because `WatchState.runtime_seconds` is, and a fraction of
    an unknown total is a number that merely *looks* computed:
    `position_seconds=1800, runtime_seconds=None` says "half an hour in, we do
    not know of what", which is true, where `progress=1.0` would say
    "finished", which is not.
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None = None
    enrichment_state: EnrichmentState
    owned: bool = False
    # Zero is a *true* value here and not an ADR-0014 stand-in: a household
    # that has not started a title is genuinely nought seconds into it. The
    # absence that must not become zero is the runtime below.
    position_seconds: int = Field(default=0, ge=0)
    # `ge=0` rather than `gt=0`, matching `WatchState.runtime_seconds`
    # exactly. The refusal that matters is `None`, not the boundary: a
    # hydration that has no runtime must pass `None` and never `0`.
    runtime_seconds: int | None = Field(default=None, ge=0)
    # Rides along cheaply, because the alternative is a client inferring
    # "watched" from `position_seconds >= runtime_seconds` -- wrong for
    # exactly the titles whose runtime is `None`, which are the ones that
    # most need the badge.
    played: bool = False
    # **Two nullable fields for the two rows that are about a chapter rather
    # than about a title**, added by Group G/H because `NextUpProvider`'s own
    # headline case asserts on the label and `ContinueWatchingProvider` has to
    # be able to resume an episode file.
    #
    # `title_id` stays the **series**, and that is the decision. Every other
    # field on this card -- `kind`, `name`, `year`, `owned`,
    # `enrichment_state` -- describes the series, and a `title_id` that
    # sometimes meant an episode would be a second vocabulary in the field
    # every other provider's cards agree on. So the chapter rides alongside
    # rather than replacing it.
    #
    # `episode_id` is what makes the card *playable*: without it a Next Up
    # card can only navigate to the series page, which is one more click than
    # the row exists to remove. `episode_label` is composed here rather than
    # left as two integers, so the zero-padding is decided once on the server
    # instead of by each client -- ADR-0006's argument ("the server composes")
    # applied to a string. `None` on every card of the other seven rows, which
    # is why both default and why neither is a branch a client has to take
    # twice.
    episode_id: uuid.UUID | None = None
    episode_label: str | None = None
    # **M9's field, and the one the module docstring is about.** An
    # `images.id`, resolvable through `GET /images/{id}` and nothing else --
    # never a path, never a URL, never a list. `None` means the catalog holds
    # no image of the kind this row's hint asks for, which is the ordinary
    # state of a title nothing has derived and is *not* an ADR-0014 site: a
    # UUID has no zero for an absence to be mistaken for.
    #
    # Defaulted, because every one of the ten providers builds its cards
    # through `BaseRow.hydrate` and a required field here would make a card
    # unconstructible outside it -- and because a household whose catalog has
    # never been derived is a household whose every card takes this arm.
    artwork: uuid.UUID | None = None


class BuiltRow(DomainModel):
    """One shelf, built: what a client renders in the order it is given.

    **Constructible with no cards, on purpose.** An empty row and an absent
    row are different states. Were `cards` to carry `min_length=1`,
    `Row.build()` would have to return `BuiltRow | None`, and then "this row
    built and had nothing to show" and "this row was never proposed" collapse
    into one `None` -- a quiet household and a dead provider respectively,
    which Group I's metrics have to tell apart. `Row.empty()` is a real method
    returning a real value only because of this.

    `cards` is a tuple rather than a list for two reasons: `DomainModel`'s
    docstring notes that a model with a `list`/`dict` field is unhashable even
    when frozen, and a cached `BuiltRow` handed to two concurrent requests
    must not be mutable by either.
    """

    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    # PRD 06: "the `reason` field is already written to be spoken aloud, not
    # just displayed" -- Alfred reads it out. That is a real constraint on M7's
    # nine providers: "Because you watched Dune" is speakable and
    # "cosine>0.82 seed=a3f9" is not. `None` when a row needs no explanation,
    # and M8's `LLMRow` is the first thing in `src/` to return it -- it hands
    # the stored `reason` through, `None` included.
    reason: str | None = None
    family: RowFamily
    # On the row, never on the card: a hint describes the shelf, and a card
    # carrying one lets a single row disagree with itself about its own shape,
    # which is a per-item layout instruction arriving by a second route.
    display_hint: DisplayHint
    # On the value rather than on the `Row` class where PRD 06's sketch puts
    # it, and required rather than defaulted. A cache stores a *built row*,
    # not the object that built it; a TTL on the class means the cache must
    # hold a reference back to its producer to know when to drop the value,
    # and the two can then disagree after a deploy -- a row written under a
    # 60 s class TTL being judged by a 300 s one. ADR-0020's argument for
    # fingerprints, applied to a shorter-lived derivative. A *default* would
    # be a number nobody chose that every row silently inherits.
    ttl: timedelta
    cards: tuple[RowCard, ...] = ()


__all__ = ["BuiltRow", "DisplayHint", "RowCard", "RowFamily"]
