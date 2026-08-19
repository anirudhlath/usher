"""Port for external metadata providers.

The three provisional markers this module carried through M1-M3 are settled
here; [ADR-0017](../../../docs/prd/decisions/0017-the-metadata-port-is-an-aggregate-and-a-cursor.md)
is the reasoning and this docstring is the summary:

1. `to_title() -> Title` became `to_result() -> EnrichmentResult`, an
   aggregate carrying the title, its season/episode hierarchy, and the
   provider's verbatim payload.
2. `fetch(provider_id: int, kind)` became `fetch(ref: ProviderRef)`, which
   already fits IMDb's `tt99000100` alongside TMDb's `90000550`/`movie`.
3. `changed_since(days: int) -> list[int]` became
   `changed_since(since, cursor) -> ChangedPage`, a resumable page.

One thing that was *not* marked also changed: `search` gained an optional
`kind`. The reasoning is in the ADR, and the short form is that TMDb
searches its two id spaces through two endpoints, so a caller that knows
which one it wants (the match stage always does) otherwise pays two upstream
requests on the tier PRD 03 already calls a last resort.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import AwareDatetime

from usher.domain.collection import Collection
from usher.domain.enums import TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.image import Image
from usher.domain.people import Credit, Person
from usher.domain.title import Title
from usher.ports.ingest import ProviderRef


@dataclass(frozen=True)
class MetadataCandidate:
    """One search result from a `MetadataProvider`, normalised enough that
    the match stage (PRD 03 Stage 2) never indexes into a provider's own
    JSON keys — e.g. TMDb's movie/TV divergence (`title`/`name`,
    `release_date`/`first_air_date`) stops here, not one layer up in M4.

    `provider_id` stays an `int` while `fetch` takes a `ProviderRef`, and
    that asymmetry is deliberate rather than an oversight the settling
    missed. A candidate is *the provider's own search result*, and
    `provider_id` + `kind` + the provider's `name` is losslessly a
    `ProviderRef` — `MatchService` builds one at the single point a candidate
    crosses into the matcher. The moment a provider whose search results are
    not integer-keyed exists, this field becomes a `ProviderRef` and `kind`
    folds into it; nothing else moves. ADR-0017.
    """

    provider_id: int
    name: str
    year: int | None
    kind: TitleKind
    popularity: float


@dataclass(frozen=True)
class EnrichmentResult:
    """Everything one provider fetch yields for one canonical title.

    `seasons`/`episodes` are empty for a movie and populated for a series.
    They are here because M4 persists them -- 999,827 of the source library's
    1,126,674 items are episodes, so a result that could not carry the
    hierarchy would leave the pipeline unable to enrich 89% of what it holds.

    **`Person`, `Credit`, `Collection` and `Image` are deliberately not
    fields**, and that is not an oversight the way the marker this settles
    was. Nothing in M4 stores them: `Person`/`Credit` are first read by M7's
    "more from this director" join, `Collection` by M7's franchise
    completeness, `Image` by M9's image proxy. A field nothing writes is a
    placeholder, and adding one now would mean either a table nobody queries
    or a `tuple()` that reads as "this provider has no cast".

    `payload` is what makes that deferral honest rather than lossy: the
    provider's full response, verbatim, on its way to `raw_payloads`. Each of
    those milestones re-derives its own entities from the cached payload with
    **no second network call**, which is exactly what PRD 02 says that table
    is for. Adding `people: tuple[Person, ...]` here later is an added field,
    not a signature change.

    Frozen but **not hashable in practice**: `payload` is a `dict` and
    `Title.field_provenance` is one too, so the generated `__hash__` raises
    at call time. Nothing keys on a result; `Title`'s own docstring records
    the same property for the same reason.

    `seasons` and `episodes` have no defaults. A provider that has not
    thought about the hierarchy should have to write `seasons=()` rather than
    silently produce a series with none — which is what enriching 89% of this
    library looks like when it goes wrong.
    """

    title: Title
    seasons: tuple[Season, ...]
    episodes: tuple[Episode, ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class DerivationResult:
    """Everything one *cached* payload yields about people, franchises and
    artwork.

    **A sibling of `EnrichmentResult`, not a field on it**, and the
    invitation to make it a field is right there in that class's own
    docstring ("adding `people: tuple[Person, ...]` here later is an added
    field, not a signature change"). Taking it would be wrong for a reason
    that document could not have known: `EnrichmentResult` is produced on the
    **enrichment** path, which runs once per title per fetch, while a
    derivation runs over the whole cache independently of enrichment. Putting
    people on `EnrichmentResult` means either `EnrichService` writes them --
    which makes `enrich` a second, slower, credit-writing job and couples two
    failure modes M4 deliberately separated -- or they are computed and
    discarded on every enrichment.

    A sibling method is the added field's honest form: same purity, same
    signature shape, and the same "a pure function of a payload that may have
    come out of `raw_payloads` months after the fetch that produced it, with
    no ref alongside it" that `kind_of_payload` already documents.

    **Every id here is a placeholder.** `Person.id` and `Collection.id` are
    fresh UUIDv7s minted per sighting, exactly as ingest mints one per
    season, and each `Credit.person_id` names the `Person` beside it in this
    same result. A person or collection the catalog already holds keeps the
    id it was inserted with, so the caller upserts on `tmdb_id`, reads the
    real ids back, and re-points. `EpisodeRepository.resolve_seasons` exists
    for the identical reason and `PersonRepository.resolve_tmdb_ids` is the
    method to use.

    `collection` is `None` for three ordinary shapes, none of which is an
    error: `belongs_to_collection: null` (a standalone film), the key absent
    entirely (**every series**), and an object with no usable id or name.

    **`images` is the one field whose ids are *not* placeholders, and that is a
    property of the table rather than of this type.** `Person` and `Collection`
    are re-pointed through `resolve_tmdb_ids` because a provider gives each an
    integer id; artwork has none, so `images` carries the natural key
    `uq_images_owner_provider_path` infers -- the caller hands these rows
    straight to `ImageRepository.replace_for_titles`, whose upsert answers with
    the id the row was first inserted with. A minted `Image.id` therefore
    survives only for a path this catalog has never seen, which is what makes
    `Cache-Control: immutable` on `GET /images/{id}` true across re-derivations
    (ADR-0032).

    Every image is owned by the `title_id` this call was given. Episode stills
    and person headshots are the two owner kinds `images` models and M9 writes
    neither -- group C's boundary call, and `ck_images_exactly_one_owner` is
    what keeps a future writer honest rather than a convention here.

    Frozen but **not hashable in practice**, exactly like `EnrichmentResult`:
    `Title.field_provenance` is a dict on the neighbouring type and the same
    property is recorded here so the two read alike.
    """

    people: tuple[Person, ...]
    credits: tuple[Credit, ...]
    collection: Collection | None
    images: tuple[Image, ...]


@dataclass(frozen=True, slots=True)
class ChangedPage:
    """One page of a provider's change feed, plus where to resume.

    `next_cursor` is `None` at the end. Opaque to the caller -- TMDb's is a
    page number, another provider's could be a token -- the same shape
    `usher.ports.bulk.BulkCursor` already gives the bulk importers, so the
    daily re-enrichment job is resumable the way a bootstrap is rather than
    restarting a 14-day window every time it is interrupted.

    `refs` are `ProviderRef`s rather than bare ids for the reason ADR-0011
    records: TMDb's movie and series id spaces overlap on 26,968 ids, so a
    page of integers is a page the caller has to guess the kind of.
    """

    refs: tuple[ProviderRef, ...]
    next_cursor: str | None


class MetadataProvider(ABC):
    """Supplies high-quality metadata for a canonical Title."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier, recorded in field provenance.

        Also the `provider` half of every `ProviderRef` this provider
        produces or accepts, so it has to agree with the vocabulary
        `TitleMatchRepository` matches on (`tmdb`, `imdb`, `tvdb`) rather
        than being a display string.
        """

    @property
    @abstractmethod
    def genre_vocabulary(self) -> frozenset[str]:
        """Which canonical genres (`usher.domain.genres`) this provider can
        name. **The set of concepts it is entitled to delete.**

        `genres` is in `EnrichService`'s replace list, so a provider that
        supplies any genre at all replaced the whole array — and a label naming
        a concept this provider has no word for was therefore not re-spelled
        but **deleted**. Measured against the real IMDb dump and the live
        catalog on 2026-08-19: of 132,116 enriched titles the dump also gives
        genres for, 53,724 lost at least one IMDb label — 69,160 deletions, of
        which 11,466 were of a concept TMDb cannot express. `Film-Noir` was
        deleted 827 times and survived zero. Control: **0 of 1,021,623**
        skeletons lost one, so this is the enrichment boundary and nothing
        else. [ADR-0039](../../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md).

        **A fact about the provider's vocabulary, not about any one response.**
        A response that omits `Drama` when the provider *has* a `Drama` is the
        provider disagreeing, and it wins — 13,141 of those deletions are
        exactly that and they are right. A response that omits `Biography` when
        the provider has no `Biography` says nothing at all, and silence is not
        a claim (the distinction ADR-0014 draws one lane over).

        **Abstract with no default**, for the reason `EnrichmentResult.seasons`
        has none: a default of "expresses everything" restores the deletion
        silently, and a default of "expresses nothing" leaves a provider unable
        to correct a genre it does know better. A provider that has not thought
        about this should have to write the set down.

        Canonical labels, never the provider's own spellings — `Sci-Fi` and
        `Science Fiction` are one concept, and a set written in either spelling
        answers the question wrongly for every title carrying the other.
        """

    @abstractmethod
    async def search(
        self, name: str, year: int | None, kind: TitleKind | None = None
    ) -> list[MetadataCandidate]:
        """Candidate matches for a name and optional year.

        `kind` narrows the search to one of a provider's id spaces and is
        optional: a provider with a single space ignores it, and a caller
        that genuinely does not know passes `None` and filters on
        `MetadataCandidate.kind`. TMDb searches movies and series through
        separate endpoints, so scoping is one upstream request instead of
        two — which matters because PRD 03 makes this the *last* tier of the
        match ladder, run once per unmatched item off the queue.

        Ordering is the provider's own relevance ordering, unchanged. Picking
        a winner is the caller's, and PRD 03 stage 5 requires it to decline
        rather than guess.
        """

    @abstractmethod
    async def fetch(self, ref: ProviderRef) -> dict[str, Any]:
        """Full raw payload for one entity. Stored verbatim in
        `raw_payloads` and consumed only by `to_result`.

        Returning a raw `dict` here is deliberate and different in kind
        from `search`'s old raw-dict return (now `MetadataCandidate`):
        this is an opaque blob by design, not a shortcut that skipped
        normalisation. Nothing above `to_result` reads it.

        Takes a `ProviderRef` rather than an `int`: the ref carries a string
        value and a kind, so it already fits IMDb's `tt99000100` and TMDb's
        `90000550`/`movie` alike. A ref this provider cannot serve — the wrong
        `provider`, or a kind-less ref for a namespaced provider — is
        `PortDataMalformed`, not `PortUnavailable`: no amount of retrying
        turns it into an answer, and `JobWorker` parks the first rather than
        backing off five times on the second.

        An entity the provider no longer serves (TMDb answers 404 for an id
        it has merged away, and the catalog holds 291,737 TMDb ids from a
        bulk export that ages) is `PortDataMalformed` for the same reason.
        """

    @abstractmethod
    def to_result(self, payload: dict[str, Any], title_id: uuid.UUID) -> EnrichmentResult:
        """Normalise a raw payload into canonical state. See
        `EnrichmentResult` for what it does and does not carry, and why.

        `title_id` is passed in and never minted here: identity is Usher's
        own UUIDv7 (ADR-0003), and a provider that generated one would create
        a second canonical row for a title the catalog already holds, on
        every re-enrichment.

        **Never sets `enrichment_state`.** The tier is the pipeline's to
        decide and it is only ever raised through `ENRICHMENT_RANK`
        (ADR-0008) -- a provider that stamped `ENRICHED` on a partial payload
        would promote a title its own answer did not earn, and one that
        stamped `SKELETON` would demote a title another provider enriched.
        Synchronous rather than `async`: this is a pure function of a payload
        the caller already holds.
        """

    @abstractmethod
    def to_derivation(self, payload: dict[str, Any], title_id: uuid.UUID) -> DerivationResult:
        """Normalise a raw payload into people, credits, a collection and
        artwork. See `DerivationResult` for what it carries and why it is not a
        field on `EnrichmentResult`.

        Same contract as `to_result`, clause for clause: `title_id` is passed
        in and never minted (ADR-0003), and this is **synchronous and pure**
        -- a function of a payload the caller already holds, with no network
        call of any kind. ADR-0016 kept `raw_payloads` precisely so M7 could
        re-derive these three entities with no second request, and a
        `to_derivation` that fetched would re-request the whole enriched tier
        against a rate limit to read data already sitting in a JSONB column.

        A payload this provider cannot read -- no `credits`, no
        `created_by`, no `belongs_to_collection`, no `images` -- yields an
        **empty** result, never an error. That is what most of the catalog
        looks like: a payload cached before `credits` joined
        `*_APPEND_TO_RESPONSE`, or an entity the provider has none for.

        **`images` is the field where that clause is least likely to be
        exercised and most likely to be misread.** Two shapes reach it and
        neither is an error: a payload cached before `images` joined the append
        list has no such key at all, and a payload that has one may carry three
        empty arrays -- which is what the recorded `series.json` holds. Neither
        is empty *overall*, because a detail response's `poster_path` and
        `backdrop_path` are top-level fields rather than an appended namespace,
        so an unappended payload still derives its two primaries. An operator
        reading a low `images written` against a large cache is seeing the age
        of the cache, not a defect.
        """

    @abstractmethod
    async def changed_since(self, since: AwareDatetime, cursor: str | None = None) -> ChangedPage:
        """One page of entities mutated since `since`, plus where to resume.

        TMDb's `/movie/changes` feed is paginated and capped at a 14-day
        window; `days: int` in and `list[int]` out could not express a
        resumable position through it, which is the marker this settles.

        **A provider may answer a narrower window than it was asked for**,
        and the caller may not read an exhausted feed as proof that nothing
        older changed. TMDb caps the window at 14 days; a `since` older than
        that is clamped rather than rejected, because the alternative — an
        error on the one call a re-enrichment sweep makes after an outage —
        turns a partial answer into no answer. PRD 04's Phase 5 runs this
        daily, so the clamp is unreachable in steady state and is the
        recovery path after a fortnight of downtime. ADR-0017.

        `cursor` is opaque and comes from a previous `ChangedPage`. Passing
        one from a different `since` is undefined; a caller that changed its
        window starts over.
        """
