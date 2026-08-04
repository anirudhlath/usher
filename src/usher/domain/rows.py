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

**There is no artwork field, and that is boundary call 3 rather than an
oversight.** PRD 06 describes `RowCard` as carrying *"artwork refs"*. There is
no `Image` table, no `images` column and not even a `poster_path` on `titles`;
M9 owns all three. The choice was between an always-null field and no field,
and M5 settled the identical question one route over for `GET /titles/{id}`'s
absent `images` key: *"an empty list would be indistinguishable from a film
with no cast."* A `RowCard` with `"artwork": null` on every card of every row
is a client-side branch that never takes its other arm, and the day M9 fills
it every client that shipped against the null already renders without it.
`extra="forbid"` is what makes that a runtime refusal rather than a naming
convention.

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
8. **`RowContext.taste`, in `ports/rows.py`.** A deployment with no embedder
   has no centroid at all (ADR-0022), and every reader drops the signal rather
   than substituting a zero vector.

The next site is Group F's `GenomeRepository` returning `None` for a title with
no genome row; count it against this list at implementation time rather than
incrementing a number read out of a plan.
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

    **Two members in M7, not PRD 06's three.** Its family table lists
    `SourceRow`, `SimilarityRow` and `LLMRow`; boundary call 2 gives the whole
    `LLMRow`/`CuratedProvider`/`curated_rows` family to M8. `CURATED` is
    deliberately not pre-declared: a cap on a family with no members is a
    branch nothing can reach, so the first thing M8 would discover is whether
    that branch was ever right. The member costs one line in the diff that
    adds the provider emitting it.

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
    field with no source. **Not carried: artwork** -- see the module docstring.

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
    # just displayed" -- Alfred reads it out. That is a real constraint on the
    # nine providers: "Because you watched Dune" is speakable and
    # "cosine>0.82 seed=a3f9" is not. `None` when a row needs no explanation.
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
