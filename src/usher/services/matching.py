"""PRD 03 stage 2: resolve a source item to a canonical `Title`."""

import re
import uuid
from collections.abc import Sequence

from loguru import logger
from opentelemetry import metrics, trace

from usher.domain.enums import EnrichmentState, MatchMethod, TitleKind
from usher.domain.jobs import JobKind, JobPriority
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict
from usher.ports.ingest import MatchOutcome, NameYearProbe, ProviderRef
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.metadata import MetadataCandidate, MetadataProvider
from usher.ports.repository import TitleMatchRepository, TitleRepository
from usher.ports.source import SourceItem, SourceItemKind
from usher.telemetry import current_traceparent

_tracer = trace.get_tracer("usher.match")
_meter = metrics.get_meter("usher.match")
_result_counter = _meter.create_counter(
    "usher.match.result", unit="1", description="Source items resolved, by method"
)

# `Title.imdb_id`'s own pattern, restated so a source's stray string is
# dropped here rather than raising a `ValidationError` two frames later.
# Duplicated deliberately: importing a private pydantic field constraint
# would couple this to how that model happens to be spelled.
_IMDB_ID = re.compile(r"^tt\d{7,8}$")

# A source item's kind is narrower than a title's -- sources address
# episodes directly (`SourceItemKind`'s own docstring). `EPISODE` is
# deliberately absent rather than mapped to `SERIES`: an episode's provider
# ids and name describe the episode, not the series, so there is no honest
# title kind to match them under. See the module docstring.
_TITLE_KIND: dict[SourceItemKind, TitleKind] = {
    SourceItemKind.MOVIE: TitleKind.MOVIE,
    SourceItemKind.SERIES: TitleKind.SERIES,
}

# Ordered by descending confidence. `kind_scoped` is ADR-0011: TMDb keys
# movies and series separately, so its refs carry the kind. IMDb's `tt` ids
# are one global namespace; TVDb's series ids are too, and
# `TitleMatchRepository`'s statement for it filters on the value alone -- a
# ref that carried a kind anyway would be a key no other caller's ref equals.
_PROVIDER_TIERS: tuple[tuple[str, MatchMethod, bool], ...] = (
    ("tmdb", MatchMethod.TMDB_ID, True),
    ("imdb", MatchMethod.IMDB_ID, False),
    ("tvdb", MatchMethod.TVDB_ID, False),
)


class MatchService:
    def __init__(
        self,
        titles: TitleRepository,
        matching: TitleMatchRepository,
        queue: JobQueue,
        provider: MetadataProvider | None = None,
    ) -> None:
        self._titles = titles
        self._matching = matching
        self._queue = queue
        # Optional because the *batch* path must never use it: `match()` runs inside a
        # walk and a network call per unmatched item would make the walk's duration a
        # function of TMDb's rate limit.
        self._provider = provider

    async def match_remote(self, item: SourceItem) -> MatchOutcome:
        """PRD 03's tier 4, one item at a time, off the queue."""
        kind = _TITLE_KIND.get(item.kind)
        if self._provider is None or kind is None:
            return MatchOutcome(
                external_id=item.external_id, title_id=None, method=MatchMethod.UNMATCHED
            )
        candidate = _confident(await self._provider.search(item.name, item.year, kind), item)
        if candidate is None:
            _result_counter.add(1, {"method": "provider_search", "confident": "false"})
            return MatchOutcome(
                external_id=item.external_id, title_id=None, method=MatchMethod.UNMATCHED
            )
        ref = ProviderRef(
            # The provider's own name, never a literal: it is the same string
            # `TitleMatchRepository` matches on, and the provider is the only
            # thing that knows it.
            provider=self._provider.name,
            value=str(candidate.provider_id),
            kind=candidate.kind,
        )
        found = (await self._lookup_refs([ref])).get(ref)
        if found is None:
            # The catalog holds 1,271,138 titles and only 291,737 carry a
            # `tmdb_id`, so a confident search result the catalog does not
            # hold is the common case rather than the exception -- the same
            # reasoning that makes tier 5 load-bearing.
            found = await self._create_stub(item, {ref.provider: candidate.provider_id})
        _result_counter.add(1, {"method": "provider_search", "confident": "true"})
        return MatchOutcome(
            external_id=item.external_id,
            title_id=found,
            # `PROVIDER_SEARCH` whether the search *found* a title or minted
            # one, because the label answers "how was this resolved" and the
            # answer is the search either way -- which is what makes PRD 10's
            # "is the TMDb-search tier earning its rate limit" answerable.
            method=MatchMethod.PROVIDER_SEARCH,
        )

    async def match(self, items: Sequence[SourceItem]) -> list[MatchOutcome]:
        """Resolve a batch.

        Returns one outcome per item, in order.
        """
        with _tracer.start_as_current_span("match.title") as span:
            span.set_attribute("usher.batch.items", len(items))
            refs = {item.external_id: self._refs_for(item) for item in items}
            # `dict.fromkeys`, never `sorted(set(...))`: `ProviderRef` and
            # `NameYearProbe` are frozen dataclasses without `order=True`, so `sorted`
            # raises `TypeError: '<' not supported` on the first batch carrying two of
            # either.
            by_ref = await self._lookup_refs(
                list(dict.fromkeys(ref for entry in refs.values() for ref, _ in entry))
            )
            probes = {item.external_id: self._probe_for(item) for item in items}
            by_probe = await self._lookup_probes(
                list(dict.fromkeys(p for p in probes.values() if p is not None))
            )
            outcomes: list[MatchOutcome] = []
            # Stubs created earlier in this batch, so a film and its two alternate cuts
            # -- three items carrying one TMDb id, which is what a multi-version library
            # looks like -- produce one title rather than three.
            created: dict[ProviderRef, uuid.UUID] = {}
            for item in items:
                outcomes.append(
                    await self._resolve(
                        item, refs[item.external_id], by_ref, probes, by_probe, created
                    )
                )
            await self._enqueue_remote_searches(items, outcomes)
            for outcome in outcomes:
                _result_counter.add(
                    1,
                    {
                        "method": outcome.method.value,
                        "confident": str(outcome.title_id is not None).lower(),
                    },
                )
            span.set_attribute("usher.matched", sum(1 for o in outcomes if o.title_id is not None))
            return outcomes

    async def _lookup_refs(self, refs: Sequence[ProviderRef]) -> dict[ProviderRef, uuid.UUID]:
        # An empty batch is not a round trip. A page of home videos carries no
        # provider id at all, and asking a database to match nothing is still
        # a network hop per batch.
        return await self._matching.match_by_provider_ids(refs) if refs else {}

    async def _lookup_probes(
        self, probes: Sequence[NameYearProbe]
    ) -> dict[NameYearProbe, uuid.UUID]:
        return await self._matching.match_by_name_year(probes) if probes else {}

    def _refs_for(self, item: SourceItem) -> list[tuple[ProviderRef, MatchMethod]]:
        kind = _TITLE_KIND.get(item.kind)
        if kind is None:
            # An episode. See the module docstring -- its ids are its own.
            return []
        refs: list[tuple[ProviderRef, MatchMethod]] = []
        for provider, method, kind_scoped in _PROVIDER_TIERS:
            value = item.provider_ids.get(provider)
            if value:
                refs.append(
                    (
                        ProviderRef(
                            provider=provider, value=value, kind=kind if kind_scoped else None
                        ),
                        method,
                    )
                )
        return refs

    def _probe_for(self, item: SourceItem) -> NameYearProbe | None:
        # An episode is never matched by its own name and year: "Kissed by
        # Fire, 2013" would match a film of the same name, and the episode's
        # canonical parent is the series it belongs to, not itself.
        kind = _TITLE_KIND.get(item.kind)
        if kind is None or item.year is None:
            return None
        return NameYearProbe(name=item.name, year=item.year, kind=kind)

    async def _resolve(
        self,
        item: SourceItem,
        refs: list[tuple[ProviderRef, MatchMethod]],
        by_ref: dict[ProviderRef, uuid.UUID],
        probes: dict[str, NameYearProbe | None],
        by_probe: dict[NameYearProbe, uuid.UUID],
        created: dict[ProviderRef, uuid.UUID],
    ) -> MatchOutcome:
        for ref, method in refs:
            found = by_ref.get(ref)
            if found is not None:
                return MatchOutcome(external_id=item.external_id, title_id=found, method=method)
        probe = probes[item.external_id]
        if probe is not None:
            found = by_probe.get(probe)
            if found is not None:
                return MatchOutcome(
                    external_id=item.external_id, title_id=found, method=MatchMethod.NAME_YEAR
                )
        for ref, _ in refs:
            already = created.get(ref)
            if already is not None:
                return MatchOutcome(
                    external_id=item.external_id,
                    title_id=already,
                    method=MatchMethod.CREATED_STUB,
                )
        usable = _usable_ids(refs)
        if usable:
            title_id = await self._create_stub(item, usable)
            for ref, _ in refs:
                created[ref] = title_id
            return MatchOutcome(
                external_id=item.external_id, title_id=title_id, method=MatchMethod.CREATED_STUB
            )
        return MatchOutcome(
            external_id=item.external_id, title_id=None, method=MatchMethod.UNMATCHED
        )

    async def _create_stub(self, item: SourceItem, usable: dict[str, int | str]) -> uuid.UUID:
        """PRD 03's stub-on-sight.

        a canonical title from the source's own metadata, `enrichment_state = stub`,
        queryable immediately.

        `usable` is already filtered to values `Title` will accept, so this
        never fabricates a title from a bare name and never raises a
        `ValidationError` at a source's expense -- see the module docstring.
        """
        kind = _TITLE_KIND[item.kind]
        title = Title(
            kind=kind,
            # `external_id` is the fallback because `Title.name` is
            # `min_length=1` and a source is not contracted to give a
            # non-empty one; an item with no name at all is still a real
            # item, and its id is the only handle anybody has on it.
            name=item.name or item.external_id,
            # No normalisation: `Title.sort_name` has an explicit
            # no-normalisation contract, and inventing one here would be the
            # adapter-side convention that model deliberately refused.
            sort_name=item.name or item.external_id,
            year=item.year if item.year is not None and item.year >= 0 else None,
            tmdb_id=_as_int(usable.get("tmdb")),
            imdb_id=_as_imdb(usable.get("imdb")),
            tvdb_id=_as_int(usable.get("tvdb")),
            enrichment_state=EnrichmentState.STUB,
        )
        try:
            await self._titles.add(title)
        except RepositoryConflict as exc:
            # Two workers creating the same stub, or a title the match repository's own
            # read did not see.
            existing = await self._lookup_conflict(title)
            if existing is None:
                logger.warning(
                    "stub for {external_id} conflicted on {constraint} and no title "
                    "carries any of its provider ids; re-raising",
                    external_id=item.external_id,
                    constraint=exc.constraint,
                )
                raise
            logger.debug(
                "stub for {external_id} lost a race; attaching to {title_id}",
                external_id=item.external_id,
                title_id=existing,
            )
            return existing
        return title.id

    async def _lookup_conflict(self, title: Title) -> uuid.UUID | None:
        """The winner of a stub race, or `None` if nothing explains it.

        Two point lookups then one batch read, in ladder order. The batch
        read is not redundant with them: `TitleRepository` has no
        `get_by_tvdb_id`, and a TVDb id with no TMDb one is the *common*
        shape for an Emby series -- so a handler built from the two point
        lookups alone re-raises for most of television and takes the whole
        walk with it. `TitleMatchRepository` answers all three providers in
        one statement, and against Postgres it reads the same rows the point
        lookups do; only the fakes keep two stores.
        """
        if title.tmdb_id is not None:
            found = await self._titles.get_by_tmdb_id(title.tmdb_id, title.kind)
            if found is not None:
                return found.id
        if title.imdb_id is not None:
            by_imdb = await self._titles.get_by_imdb_id(title.imdb_id)
            if by_imdb is not None:
                return by_imdb.id
        refs = [
            ProviderRef(provider=provider, value=str(value), kind=kind)
            for provider, value, kind in (
                ("tmdb", title.tmdb_id, title.kind),
                ("imdb", title.imdb_id, None),
                ("tvdb", title.tvdb_id, None),
            )
            if value is not None
        ]
        resolved = await self._lookup_refs(refs)
        return next((resolved[ref] for ref in refs if ref in resolved), None)

    async def _enqueue_remote_searches(
        self, items: Sequence[SourceItem], outcomes: Sequence[MatchOutcome]
    ) -> None:
        """One `enqueue` per batch, never one per item.

        Episodes are excluded: searching TMDb for a title called "Kissed by
        Fire" is not a resolution path, and enqueueing one per episode is
        999,827 rows a walk that no handler can complete. `IngestService`
        enqueues a `match` job for the episodes whose series it genuinely
        could not resolve, which is bounded by the series it has not matched
        rather than by the library.
        """
        traceparent = current_traceparent()
        unmatched = [
            JobRequest(
                kind=JobKind.MATCH,
                key=outcome.external_id,
                priority=JobPriority.BACKFILL,
                traceparent=traceparent,
            )
            for item, outcome in zip(items, outcomes, strict=True)
            if outcome.title_id is None and item.kind is not SourceItemKind.EPISODE
        ]
        if unmatched:
            await self._queue.enqueue(unmatched)


def _confident(
    candidates: Sequence[MetadataCandidate], item: SourceItem
) -> MetadataCandidate | None:
    """The one candidate a remote search resolved to, or `None`."""
    wanted = item.name.strip().casefold()
    matches = [
        candidate
        for candidate in candidates
        if candidate.name.strip().casefold() == wanted
        and (
            item.year is None
            or (candidate.year is not None and abs(candidate.year - item.year) <= 1)
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _usable_ids(refs: Sequence[tuple[ProviderRef, MatchMethod]]) -> dict[str, int | str]:
    """The subset of an item's provider ids `Title` will actually store.

    A source is free to report `ProviderIds.Tmdb: "unknown"` or an IMDb id
    that is not an IMDb id. Creating a stub from those produces a title
    carrying no provider id at all -- a canonical row built from a bare
    name, which is exactly what tier 5 is scoped to forbid, arriving through
    the back door. An item whose every id is unusable is unmatched.
    """
    usable: dict[str, int | str] = {}
    for ref, _ in refs:
        if ref.provider in ("tmdb", "tvdb"):
            number = _as_int(ref.value)
            if number is not None:
                usable[ref.provider] = number
        elif ref.provider == "imdb":
            value = _as_imdb(ref.value)
            if value is not None:
                usable[ref.provider] = value
    return usable


def _as_int(value: int | str | None) -> int | None:
    """A source is free to report `ProviderIds.Tmdb: "unknown"`.

    That is a matching failure, not a pipeline failure, and it must not abort a batch of
    5,000 items.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _as_imdb(value: int | str | None) -> str | None:
    """`Title.imdb_id` is pattern-validated.

    so an id that is not one is dropped here rather than raising a `ValidationError` the
    reconciler re-raises.
    """
    if not isinstance(value, str) or not _IMDB_ID.match(value):
        return None
    return value
