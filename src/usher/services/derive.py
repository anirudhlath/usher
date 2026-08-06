"""PRD 06's people, credits and collections, out of the cache ADR-0016 kept.

M4's boundary call 2 deferred `Person`/`Credit`/`Collection` to this milestone
by name and promised they would be **re-derived from `raw_payloads` with no
second network call**. ADR-0016 kept that table for exactly this, and
`ports/metadata.py` wrote the same note from the other end when it explained
why `EnrichmentResult` has no `people` field. This module is where the note is
presented.

**Nothing here reads a provider's JSON key, and that is the layering call of
the milestone rather than a style rule.** M6 kept the search document's weight
class B empty and said why, in `services/search.py`: *"the only place credits
physically exist is `raw_payloads.payload`, so assembling them here would put
a provider's JSON shape in `services/`."* Cashing that deferral is no reason
to spend it. The payload -> entity mapping lives in
`usher.adapters.tmdb.mapping` beside `title_from_payload`, reached through
`MetadataProvider.to_derivation`, and this service orchestrates. `dict` is not
an import, so `lint-imports` cannot see the difference -- the review question
is whether a string literal that is a TMDb field name appears anywhere under
`src/usher/services/`, and the answer must stay no.

**The join back is `(provider, kind, reference)` and `kind` is half of it.**
`raw_payloads` has no `title_id` and no foreign key to `titles`, so a walk
that starts from a payload has to resolve its title -- and the payload's own
`id` field is the bare integer sitting right there. ADR-0011: TMDb keys movies
and series in separate spaces that overlap on 26,968 measured ids, so a
resolution keyed on the integer alone writes a series' cast onto a film. With
the right counts, the right people, and nothing to say so.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger
from opentelemetry import trace

from usher.domain.collection import Collection
from usher.domain.enums import TitleKind
from usher.domain.people import Credit, CreditKind, Person
from usher.ports.metadata import MetadataProvider
from usher.ports.repository import (
    CachedPayload,
    CollectionRepository,
    CreditRepository,
    PersonRepository,
    RawPayloadStore,
    TitleRepository,
)

_tracer = trace.get_tracer("usher.derive")

# The provider whose cache this walks. A constant rather than an argument
# because the mapping on the other side of `to_derivation` is that provider's:
# a second metadata provider is a second `MetadataProvider` implementation and
# a second walk, not a parameter here.
_PROVIDER = "tmdb"

# How many cast names reach `titles.credit_names`, which is **not** the number
# of cast rows `credits` stores (that is `mapping._CAST_LIMIT`, 50). The gap is
# the point, and both halves of it are about the search document rather than
# about the cast:
#
# - **The embedding.** `cli.py:_index` measures a realistic document at
#   ~100-130 tokens and prices the enriched tier off it. Two hundred names at
#   ~2 tokens each is ~400 tokens: the document quadruples and the film's own
#   name goes from ~4% of the text to under 1%, on a vector that is a mean
#   over the whole thing.
# - **The tsvector.** Class-B lexemes are the great majority of a big film's
#   document once B is filled at all. `ts_rank`'s weight vector separates the
#   classes, but a document whose B class is twenty times its A class is one
#   where a two-word query matching one B lexeme accumulates against a much
#   larger lexeme set.
#
# **Ten is chosen, not measured**, on the bargain `services/search.py` states
# for `_POPULARITY_MIDPOINT`: small enough that A still dominates, large enough
# to cover the billed cast anybody searches for. A search-quality pass is what
# would move it, not an argument.
_CREDIT_NAME_CAST_LIMIT = 10


@dataclass(frozen=True, slots=True)
class DerivationReport:
    """What one run of the walk did, in numbers an operator can act on.

    Counts rather than a ratio, and `usher derive`'s report prints them the
    same way for the reason PRD 08 gives: a derived-coverage percentage is
    `titles_derived / payloads_read`, and that is `0/0` on the empty database
    every command has to work against.

    `payloads_read` and `titles_derived` differ by the payloads naming a title
    the catalog no longer holds, which is ordinary -- `raw_payloads` outlives
    `titles`.
    """

    payloads_read: int = 0
    titles_derived: int = 0
    people_written: int = 0
    credits_written: int = 0
    collections_written: int = 0


class DeriveService:
    """People, credits and collections for a page of cached payloads.

    **Nothing here fetches.** The collaborator list is a `RawPayloadStore`, a
    `MetadataProvider` and four repositories, and the provider is held for
    `to_derivation` alone -- the same purity `to_result` has. A derivation
    that called `fetch` would re-request the enriched tier against a rate
    limit to read data already sitting in a JSONB column, and
    `test_deriving_makes_no_provider_fetch` asserts it over a provider whose
    `fetch` raises.

    `commit` is injected because `services/` may depend only on `domain/` and
    `ports/` (ADR-0009), and a session is neither.

    **One transaction per page, not per title and not per run.** Per title is
    a round trip per row of a walk over the whole cache; per run holds one
    transaction open for the length of a full derivation and its locks with
    it. The page is `iterate`'s page, which is also the unit `after` advances
    by, so a process killed mid-walk resumes at a page boundary and re-derives
    at most one page -- which is free, because the write is a replace.
    """

    def __init__(
        self,
        *,
        payloads: RawPayloadStore,
        provider: MetadataProvider,
        titles: TitleRepository,
        people: PersonRepository,
        credits: CreditRepository,
        collections: CollectionRepository,
        commit: Callable[[], Awaitable[None]],
    ) -> None:
        self._payloads = payloads
        self._provider = provider
        self._titles = titles
        self._people = people
        self._credits = credits
        self._collections = collections
        self._commit = commit

    async def derive_all(self, *, page_size: int = 500, limit: int = 0) -> DerivationReport:
        """Walk the whole cache, one page per transaction. `limit` of 0 drains.

        The one-shot backfill, and it exists because M7 arrives after a
        catalog is already enriched: those titles were enriched by M4/M5/M6,
        their payloads are in the cache, and **nothing will ever re-enrich
        them**, so nothing will ever enqueue a `derive` job for them.

        `limit` bounds the walk rather than the report -- a flag that bounded
        only the numbers would read like a bound and not be one. The page that
        crosses it is finished rather than truncated, because a half-written
        page would leave the scoped replace covering fewer titles than the
        delete it already ran.
        """
        with _tracer.start_as_current_span("derive.walk") as span:
            report = DerivationReport()
            after: uuid.UUID | None = None
            while True:
                page = await self._payloads.iterate(_PROVIDER, limit=page_size, after=after)
                if not page:
                    break
                report = _add(report, await self._apply(await self._resolve(page)), read=len(page))
                # After the write, so a killed process resumes at a boundary
                # whose page is durably derived rather than one whose cursor
                # moved past work that was rolled back.
                await self._commit()
                after = page[-1].id
                if limit and report.payloads_read >= limit:
                    break
            span.set_attribute("usher.derive.payloads", report.payloads_read)
            span.set_attribute("usher.derive.titles", report.titles_derived)
            return report

    async def derive(self, title_id: uuid.UUID) -> None:
        """One title, from the one cache row that holds it. Raises
        `UsherPortError`.

        The `derive` job's unit of work, and what makes it a `JobKind` at all:
        everything this reads is one `raw_payloads` row found by one key, and
        no other title's data is touched. `SimilarityService`'s rebuild is the
        counter-example and is deliberately not a kind, because a neighbour
        list is a function of every other embedded vector.

        Three states complete rather than park, and each is ordinary rather
        than defensive: a title the catalog no longer holds (`raw_payloads`
        has no foreign key to `titles`), a title with no `tmdb_id` at all (979
        thousand of the one measured catalog's 1.27M rows), and a title with
        no cached payload (enriched before `credits` joined
        `*_APPEND_TO_RESPONSE`). Parking any of them needs a human to release
        work whose only problem is that there is none.
        """
        with _tracer.start_as_current_span("derive.title") as span:
            span.set_attribute("usher.title_id", str(title_id))
            title = await self._titles.get(title_id)
            if title is None or title.tmdb_id is None:
                logger.debug(
                    "derive job names a title that is gone or has no provider id: {id}",
                    id=title_id,
                )
                return
            found = await self._payloads.get(_PROVIDER, title.kind.value, str(title.tmdb_id))
            if found is None:
                logger.debug("no cached {p} payload for title {id}", p=_PROVIDER, id=title_id)
                return
            await self._apply([(title_id, found[0])])
            await self._commit()

    async def _resolve(self, page: Sequence[CachedPayload]) -> list[tuple[uuid.UUID, Any]]:
        """Cached rows -> `(title_id, payload)` pairs, in **one read per id
        space**.

        The kind comes from the cache row, never from the payload, and never
        from the integer: `CachedPayload.kind` is half the key the row was
        stored under. A row whose `kind` is not one this catalog models, or
        whose `reference` is not an integer, is skipped rather than raised on
        -- `raw_payloads` is keyed by three free strings and nothing validates
        them on the way in.

        Two statements per page rather than one per payload. A page is 500
        payloads, and a lookup per payload is the round-trip-per-item shape
        batching exists to remove.
        """
        wanted: dict[TitleKind, dict[int, list[Any]]] = {}
        for row in page:
            kind = _kind_of(row.kind)
            reference = _int_or_none(row.reference)
            if kind is None or reference is None:
                continue
            wanted.setdefault(kind, {}).setdefault(reference, []).append(row.payload)

        resolved: list[tuple[uuid.UUID, Any]] = []
        for kind, by_reference in wanted.items():
            found = await self._titles.resolve_tmdb_ids(kind, list(by_reference))
            for reference, payloads in by_reference.items():
                title_id = found.get(reference)
                if title_id is None:
                    # Ordinary: `raw_payloads` outlives `titles`, so a payload
                    # for a title deleted since the fetch names nothing. The
                    # walk skips it rather than aborting the page.
                    continue
                resolved.extend((title_id, payload) for payload in payloads)
        return resolved

    async def _apply(self, resolved: Sequence[tuple[uuid.UUID, Any]]) -> DerivationReport:
        """Map, upsert people, re-point credits, link collections, replace.

        The order is a dependency chain and not a preference. People must
        exist before a credit may name one (`credits.person_id` is a real
        foreign key), and their **stored** ids are knowable only by reading
        them back: the mapper mints a fresh UUIDv7 per sighting, exactly as
        ingest does for seasons, and a person the catalog already holds keeps
        the id it was inserted with. `EpisodeRepository.resolve_seasons`
        exists for the identical reason and its absence failed on the *second*
        enrichment rather than the first.
        """
        if not resolved:
            return DerivationReport()

        derivations = [
            (title_id, self._provider.to_derivation(payload, title_id))
            for title_id, payload in resolved
        ]

        people: dict[int, Person] = {}
        collections: dict[int, Collection] = {}
        # minted `Person.id` -> that person's provider id, which is the only
        # bridge between a credit the mapper built and the row the catalog
        # holds. Built across the whole page because one working actor is on
        # several of its titles.
        provider_id_of: dict[uuid.UUID, int] = {}
        for _, derivation in derivations:
            for person in derivation.people:
                if person.tmdb_id is None:
                    # Unreachable through today's mapper, which drops an entry
                    # with no id -- and kept as a guard rather than an assert
                    # because a person with a NULL provider id is *inserted*
                    # rather than merged (the unique index is partial), so its
                    # stored id can never be read back and any credit naming
                    # it would be permanently orphaned.
                    continue
                provider_id_of[person.id] = person.tmdb_id
                people.setdefault(person.tmdb_id, person)
            if derivation.collection is not None and derivation.collection.tmdb_id is not None:
                collections.setdefault(derivation.collection.tmdb_id, derivation.collection)

        person_ids: dict[int, uuid.UUID] = {}
        if people:
            await self._people.upsert_many(list(people.values()))
            person_ids = await self._people.resolve_tmdb_ids(list(people))

        collection_links: list[tuple[uuid.UUID, uuid.UUID]] = []
        if collections:
            await self._collections.upsert_many(list(collections.values()))
            collection_ids = await self._collections.resolve_tmdb_ids(list(collections))
            for title_id, derivation in derivations:
                if derivation.collection is None or derivation.collection.tmdb_id is None:
                    continue
                stored = collection_ids.get(derivation.collection.tmdb_id)
                if stored is not None:
                    collection_links.append((title_id, stored))
        # `attach_titles` filters `kind = 'movie'` itself and does not trust
        # this caller -- which is why a series never receives one even though
        # nothing here checks a kind. Two independent places, deliberately.
        linked = await self._collections.attach_titles(collection_links) if collection_links else 0

        rows: list[Credit] = []
        credit_names: dict[uuid.UUID, list[str]] = {}
        for title_id, derivation in derivations:
            repointed = [
                credit.evolve(person_id=person_ids[provider_id_of[credit.person_id]])
                for credit in derivation.credits
                if credit.person_id in provider_id_of
                and provider_id_of[credit.person_id] in person_ids
            ]
            rows.extend(repointed)
            credit_names[title_id] = _credit_names(derivation.credits, people, provider_id_of)

        written = await self._credits.replace_for_titles(
            [title_id for title_id, _ in derivations], rows, credit_names=credit_names
        )
        return DerivationReport(
            titles_derived=len(derivations),
            people_written=len(people),
            credits_written=written,
            collections_written=linked,
        )


def _credit_names(
    credits: Sequence[Credit],
    people: dict[int, Person],
    provider_id_of: dict[uuid.UUID, int],
) -> list[str]:
    """Weight class B's text: the top ten billed, then every stored crew name.

    **Cast first and in billing order**, because the order *is* the ranking --
    a director ahead of the lead actor is a class-B ordering nobody chose.
    Sorted by `billing_order` before the slice, never sliced first: the two
    agree on every array a provider happened to sort and disagree on the rest,
    and slicing first keeps the wrong ten.

    Crew is not truncated. `mapping.CREDITED_JOBS` already bounds it to six
    jobs, so the tail this would cut does not exist -- and the people it holds
    (director, writer) are the ones a viewer names a film by.

    Deduplicated while keeping first position, because one person may hold
    several credits on one title and repeating a name in a tsvector inflates
    its term frequency for no reason a searcher would recognise.
    """
    cast = sorted(
        (one for one in credits if one.kind is CreditKind.CAST),
        # `billing_order` is nullable and Postgres sorts ASC NULLS LAST, which
        # this mirrors: an uncredited entry goes behind every billed one
        # rather than in front of the lead.
        key=lambda one: (one.billing_order is None, one.billing_order or 0),
    )[:_CREDIT_NAME_CAST_LIMIT]
    crew = [one for one in credits if one.kind is CreditKind.CREW]

    names: list[str] = []
    for credit in (*cast, *crew):
        provider_id = provider_id_of.get(credit.person_id)
        person = people.get(provider_id) if provider_id is not None else None
        if person is not None and person.name not in names:
            names.append(person.name)
    return names


def _kind_of(value: str) -> TitleKind | None:
    try:
        return TitleKind(value)
    except ValueError:
        # `raw_payloads.kind` is a free string -- nothing validates it on the
        # way in -- so a row written by some other producer is skipped rather
        # than raised on.
        return None


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _add(total: DerivationReport, page: DerivationReport, *, read: int) -> DerivationReport:
    return DerivationReport(
        payloads_read=total.payloads_read + read,
        titles_derived=total.titles_derived + page.titles_derived,
        people_written=total.people_written + page.people_written,
        credits_written=total.credits_written + page.credits_written,
        collections_written=total.collections_written + page.collections_written,
    )


__all__ = ["DerivationReport", "DeriveService"]
