"""Ports are ABCs (ADR-0001), not Protocols: an incomplete implementation
must fail at instantiation, not at the call site."""

from abc import ABC
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, get_type_hints

import pytest

from tests.fakes.title_repository import FakeTitleRepository
from usher.domain.curation import LLMCall
from usher.domain.enums import TitleKind
from usher.domain.ids import new_id
from usher.ports.bulk import BulkDataset
from usher.ports.credentials import CredentialCiphertextStore, CredentialStore
from usher.ports.embedding import Embedder
from usher.ports.errors import (
    PortAuthFailed,
    PortRateLimited,
    PortUnavailable,
    RepositoryConflict,
    RepositoryNotFound,
    UsherPortError,
)
from usher.ports.events import EventPublisher
from usher.ports.images import ImageBlobStore, ImageFetcher
from usher.ports.jobs import JobQueue
from usher.ports.llm import LLMClient, LLMPurpose, LLMUsage
from usher.ports.metadata import MetadataCandidate, MetadataProvider
from usher.ports.repository import (
    BackupRepository,
    BulkCatalogRepository,
    CollectionRepository,
    CreditRepository,
    CuratedRowRepository,
    EpisodeRepository,
    GenomeRepository,
    ImageRepository,
    ImportRunRepository,
    LLMCallRepository,
    MediaItemRepository,
    PersonRepository,
    RawPayloadStore,
    RestoreRepository,
    RowProviderSettingsRepository,
    SearchQueryRecord,
    SearchQueryRepository,
    SourceRepository,
    SyncRunRepository,
    TasteRepository,
    TitleEmbeddingRepository,
    TitleMatchRepository,
    TitleNeighborRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.rows import Row, RowProvider
from usher.ports.scheduler import JobOutcome, ScheduledJob
from usher.ports.search import (
    FilterNotSupported,
    SearchIndex,
    SearchMode,
    SearchRequest,
    SearchSurface,
    SuggestIndex,
    SuggestTier,
)
from usher.ports.source import SourceAdapter, SourceAdapterFactory, SourceNotSupported

# Every ABC in `usher.ports` that declares an abstract method, and
# `test_every_port_abc_is_registered_in_all_ports` is what keeps that true.
# Until M6 wrote that case this list held eight of twenty-one -- the two
# checks below had never once run against `JobQueue`, `EventPublisher`, or
# any of the nine repository ports (ADR-0009: repositories are ports too).
ALL_PORTS: list[type[ABC]] = [
    SourceAdapter,
    SourceAdapterFactory,
    CredentialStore,
    # M10 K7. A second credential port rather than three more methods on
    # the first, so a route holding a `CredentialStore` still cannot reach
    # a raw ciphertext -- `ports/credentials.py` carries the argument.
    CredentialCiphertextStore,
    MetadataProvider,
    SearchIndex,
    SuggestIndex,
    Embedder,
    LLMClient,
    ImageFetcher,
    ImageBlobStore,
    BulkDataset,
    EventPublisher,
    JobQueue,
    BackupRepository,
    BulkCatalogRepository,
    CollectionRepository,
    CreditRepository,
    CuratedRowRepository,
    EpisodeRepository,
    GenomeRepository,
    ImageRepository,
    ImportRunRepository,
    LLMCallRepository,
    MediaItemRepository,
    PersonRepository,
    RawPayloadStore,
    RestoreRepository,
    RowProviderSettingsRepository,
    SearchQueryRepository,
    SourceRepository,
    SyncRunRepository,
    TasteRepository,
    TitleEmbeddingRepository,
    TitleMatchRepository,
    TitleNeighborRepository,
    TitleRepository,
    WatchStateRepository,
    Row,
    RowProvider,
    # M10's J4. Not a repository and not a driven adapter: the *loop* is
    # `usher.services.scheduler`, and this is the shape a batch has to have
    # to be registered with it. It is on this list because
    # `test_every_port_abc_is_registered_in_all_ports` below would
    # otherwise report it missing -- which is the whole reason that case
    # exists.
    ScheduledJob,
]


@pytest.mark.parametrize("port", ALL_PORTS)
def test_port_cannot_be_instantiated_directly(port: type[ABC]) -> None:
    with pytest.raises(TypeError):
        port()


@pytest.mark.parametrize("port", ALL_PORTS)
def test_port_declares_abstract_methods(port: type[ABC]) -> None:
    assert port.__abstractmethods__


@pytest.mark.parametrize("port", ALL_PORTS)
def test_no_port_is_a_protocol(port: type[ABC]) -> None:
    """ADR-0001: ports are `abc.ABC`, never `typing.Protocol`. A Protocol is
    satisfied structurally, so a fake that drifts from the port keeps passing
    and the contract suite silently stops being a contract.

    **Nothing in this file checked that, and the M7 group B sweep is what
    found it.** The obvious assertions all pass against the rewrite ADR-0001
    forbids: a `Protocol` subclass that keeps its `@abstractmethod`
    decorators has a populated `__abstractmethods__`, so
    `test_port_declares_abstract_methods` is green, and instantiating it
    raises `TypeError` with the message *"Can't instantiate abstract class P
    without an implementation for abstract methods ..."* -- **byte-identical
    to the ABC's**, verified directly, so `test_port_cannot_be_instantiated_
    directly` is green too and even `pytest.raises(TypeError,
    match="abstract")` cannot tell them apart. The three mutations
    `class <Port>(Protocol)` survived the whole file before this case
    existed.

    `ABC in __mro__` is what discriminates: `typing.Protocol` derives from
    `Generic` and only its *metaclass* is an `ABCMeta`, so the class object
    itself never has `ABC` in its MRO. Both halves are asserted, because
    `ABC in __mro__` alone would pass for a class inheriting from both.
    """
    assert ABC in port.__mro__, f"{port.__name__} is not an ABC (ADR-0001)"
    # Widened to `object` deliberately: `typing.Protocol` is a typing special
    # form rather than a `type`, so the direct `Protocol not in port.__mro__`
    # is a mypy `comparison-overlap` error against a `tuple[type, ...]` --
    # and silencing that with an ignore would leave the check itself
    # unverified by the type checker.
    protocol: object = Protocol
    assert protocol not in port.__mro__, f"{port.__name__} is a Protocol (ADR-0001)"


@pytest.mark.parametrize(
    "port,methods",
    [
        # `get` is here as of M9's `GET /people/{id}`, which is the caller
        # Task 6 said the absence was waiting for -- "nothing in M7 reads one
        # person by id and `GET /people/{id}` is M9's". So the deliberate gap
        # closes because its route arrived, not because a fake wanted a
        # read-back: the suite still reads a stored person through
        # `PersonHistorySeeder.stored`, which is what keeps that seam off the
        # shipped surface.
        (
            PersonRepository,
            {"get", "upsert_many", "resolve_tmdb_ids", "list_recurring_for_user", "count"},
        ),
        (
            CreditRepository,
            {
                "replace_for_titles",
                "list_for_title",
                "list_for_person",
                "count_titles_with_credits",
            },
        ),
        # `get` is here as of M9's `GET /collections/{id}`, the second of Task
        # 6's four absences to close and for the same reason as
        # `PersonRepository.get`: the route arrived. It is not `list_owned`
        # with a filter -- it carries **no `min_owned`**, which is the whole
        # difference between "what belongs on a screen" and "the franchise the
        # client asked for by id".
        (
            CollectionRepository,
            {"get", "upsert_many", "resolve_tmdb_ids", "attach_titles", "list_owned", "count"},
        ),
        # Not one of Task 6's three, and added here by M7's Task 35 because
        # this is exactly the list that catches what that task did: it grew
        # `count_stale` on a port six milestones old, and nothing else in the
        # suite would have noticed the surface move.
        # `resume_cursor` is M10's J6 and lands on the same terms `count_stale`
        # did: a port six milestones old grew a method, and this list is the
        # only thing in the suite that notices a surface move. It reads across
        # `title_embeddings` as well as this table -- the lowest embedded seed
        # with no row under the running blend, minus one -- which is why it is
        # here rather than on `TitleEmbeddingRepository`: the question is what
        # the *artefact* says has been done.
        (
            TitleNeighborRepository,
            {"replace", "list_for", "computed_at", "count_stale", "resume_cursor"},
        ),
        # M8 Task 9, and it is on this list for the reason `count_stale` is:
        # the surface is where the decisions live. **`replace_for_user` takes
        # no `generation_id` parameter** -- every `CuratedRow` carries one, so
        # a third argument would be a second spelling of a fact the rows
        # already hold, and the delete's scope is `user_id` rather than the
        # generation (M8's plan names a three-argument signature; the port
        # docstring carries the argument for departing from it). A parameter
        # re-added here without that argument being answered moves this set.
        (
            CuratedRowRepository,
            {"replace_for_user", "list_for_user"},
        ),
        # **An append and no read, and the absence is the entry's content.**
        # A windowed `list_since` was added in M10 and deleted again once it
        # was measured to have no caller in `src/`: every spend panel and the
        # cost-anomaly alert are SQL living in Grafana, so a read here is a
        # surface maintained for a consumer that never imports it.
        # `ix_llm_calls_at` answers that alert directly.
        (
            LLMCallRepository,
            {"record"},
        ),
    ],
)
def test_the_new_repository_ports_declare_exactly_these_abstract_methods(
    port: type[ABC], methods: set[str]
) -> None:
    """The exact set, not merely a non-empty one, and the sweep is why.

    `test_port_declares_abstract_methods` asserts `port.__abstractmethods__`
    is truthy, which is satisfied by a port that lost the decorator on one
    method of three -- the other two keep the class abstract, so
    instantiation still raises and nothing notices. That mutation survived
    the whole file. A method that silently became concrete returns `None`,
    and a fake that never implements it passes its own contract suite while
    the real repository is the only thing that works.

    Also a spelled-out inventory of the methods Task 6 settled, so the four
    deliberately-absent ones -- `PersonRepository.get`,
    `CollectionRepository.get`, `list_members`, and any `rebuild` -- cannot
    be added without this list moving and someone reading the reason.
    **Two of those four are now present** -- `PersonRepository.get` and
    `CollectionRepository.get` -- and each arrived with the M9 route Task 6
    named as the caller it was waiting for. `list_members` and `rebuild` are
    still absent and still deliberate: `OwnedCollection` carries the member
    list, so a separate members read would be a second opinion about the same
    fact.

    It moved once, and this is the record of it: M7's `usher derive` report
    added `PersonRepository.count`, `CollectionRepository.count` and
    `CreditRepository.count_titles_with_credits`. Each is read by that
    command's bare form, which is the same bargain `usher index`'s bare form
    takes with `count_stale`/`count_refused` -- a count with a caller, not a
    port method whose only test is its own. `count_titles_with_credits`
    counts **titles**, never rows, and its name says so because a report
    reading "412,000 credits" answers a question nobody asked.
    """
    assert set(port.__abstractmethods__) == methods


def test_every_port_abc_is_registered_in_all_ports() -> None:
    """`ALL_PORTS` is hand-maintained, and until this case existed nothing
    noticed a port that was left out of it -- so a new port silently got
    neither the "cannot be instantiated" check nor the "declares abstract
    methods" one, which are the two properties ADR-0001 chose ABCs *for*.

    M6 adds `SuggestIndex` and is the second milestone in a row to add a
    port. The plan assumed the list was correct so far by attention;
    running this case for the first time reported **thirteen** missing
    names, not one -- `JobQueue`, `EventPublisher`, `BulkDataset` and all
    nine repository ports had never been checked at all. Attention had not
    in fact been keeping it. Two lines of `pkgutil` is cheaper.

    Deliberately walks `usher.ports.*` rather than asserting a count: a
    count moves for a rename, which teaches people to edit the number.

    The two `declared` assertions are the control, and they are not
    decoration. A scan that globs nothing passes exactly like a scan that
    passes -- the failure mode `tests/unit/test_no_third_party_data.py`
    carries the same guard against, and the one this case's own mutation
    sweep found: emptying `pkgutil.iter_modules(...)` broke nothing.

    **`walk_packages`, not `iter_modules`, and M9 is what that is for.**
    `iter_modules` does not descend into a subpackage, and the filter below
    keeps a class only when `value.__module__ == namespace.__name__` -- so
    the moment `ports/repository.py` became `ports/repository/`, every one
    of its re-exported ABCs carried `usher.ports.repository.title` against a
    namespace called `usher.ports.repository` and stopped matching.
    Measured both ways at the split: `iter_modules` finds **13** ports and
    `walk_packages` finds **32**, so the naive spelling dropped all
    **nineteen** repository ports at once -- and both of this case's own
    controls survive that, because `declared` is still full and `SearchIndex`
    is still in it. Demonstrated rather than reasoned about: with
    `TitleRepository` deleted from `ALL_PORTS`, the `iter_modules` spelling
    passes and this one fails naming it. That is the cheapest false green in
    the milestone -- a scan whose *subject* narrowed while every guard on it
    stayed true.
    """
    import importlib
    import pkgutil

    import usher.ports

    declared: set[type[ABC]] = set()
    for module in pkgutil.walk_packages(usher.ports.__path__, prefix="usher.ports."):
        namespace = importlib.import_module(module.name)
        for value in vars(namespace).values():
            if (
                isinstance(value, type)
                and issubclass(value, ABC)
                and value.__module__ == namespace.__name__
                # `None`, not `frozenset()`: `issubclass` is true for a
                # *virtual* subclass too, which need not carry the
                # attribute at all -- and mypy rejects the empty-frozenset
                # default outright, picking `getattr`'s `bool` overload.
                and getattr(value, "__abstractmethods__", None)
            ):
                declared.add(value)
    assert declared, "the port scan found nothing, so it proves nothing"
    assert SearchIndex in declared
    missing = {port.__name__ for port in declared} - {port.__name__ for port in ALL_PORTS}
    assert not missing, f"ports missing from ALL_PORTS: {sorted(missing)}"


def test_suggest_index_has_no_write_method() -> None:
    """**The structural half of ADR-0021.** The whole argument for splitting
    this port is that adding a second engine for the instant-search box must
    require *adding* a write path, visibly, rather than filling in one that
    was already declared. A future `index`/`remove` here would make that
    change look like satisfying an abstract method instead of acquiring the
    dual write ADR-0002 refused.

    So this is not a style assertion -- it is the reason the class exists,
    written as a check. Deleting it and adding `index` is a decision; doing
    it without deleting this is a failing test.
    """
    assert SuggestIndex.__abstractmethods__ == frozenset({"suggest"})


def test_a_scheduled_job_declares_exactly_these_four_members() -> None:
    """**The exact set, and `name`/`period` being in it is the decision.**

    ADR-0046's contract is a name, a period, a `last_done()` and a `run()`, and
    the M10 plan sketches the first two as bare annotations (`name: str`). An
    annotation is satisfied by an implementation that never assigns it, so the
    first thing to notice would be an `AttributeError` reading a metric label
    at the first tick -- which is precisely the failure mode ADR-0001 chose
    ABCs to move to instantiation time. They are abstract properties instead,
    and a plain class attribute on the subclass satisfies one, so an
    implementation pays nothing for the enforcement.

    A fifth member added here without that argument being re-made -- a
    `should_run()`, an `on_failure()`, a `timeout` -- moves this set, and the
    scheduler holding no state (ADR-0046, decision 2) is what most of those
    would quietly reverse.
    """
    assert ScheduledJob.__abstractmethods__ == frozenset({"name", "period", "last_done", "run"})
    # Each carries its own reasoning rather than sharing the class docstring:
    # `period`'s "minimum interval, never a wall clock" and `last_done()`'s
    # "`min`, not `max`" are the two things an implementer gets wrong, and
    # neither is inferable from a signature.
    for member in ("name", "period", "last_done", "run"):
        assert getattr(ScheduledJob, member).__doc__, f"{member} carries no docstring"


def test_a_scheduled_job_that_forgets_its_name_cannot_be_instantiated() -> None:
    """The behavioural half of the case above, because a `frozenset` equality
    is satisfied by a class whose abstractness Python does not enforce.

    This is the whole of what ADR-0001 buys over a `Protocol` here, and the
    plan's bare-annotation spelling is what it would have cost: the job below
    is a complete implementation *except* for the one member that is only ever
    read by a metric label and a span name.
    """

    class _Nameless(ScheduledJob):
        period = timedelta(hours=1)

        async def last_done(self) -> datetime | None:
            return None

        async def run(self) -> JobOutcome:
            return JobOutcome.DONE

    # `type: ignore[abstract]` and the ignore is itself part of the finding:
    # mypy refuses this line too, statically, which a bare annotation would
    # also not have bought. The runtime assertion is what the deployment gets;
    # the ignore is the record that the type checker agrees.
    with pytest.raises(TypeError, match="name"):
        _Nameless()  # type: ignore[abstract]


def test_the_cost_ledger_takes_the_domain_model_rather_than_its_parts() -> None:
    """`LLMCallRepository.record`'s signature is the answer to "what happens
    when the constructor raises inside an exception handler", and the port's
    docstring is where that argument is made. Pinned here because a signature
    is the part of it a later change can undo without reading a word of the
    reasoning.

    Asserted on the annotation rather than on the parameter count, because a
    parts-shaped `record(**fields: Any)` has one parameter too.
    """
    hints = get_type_hints(LLMCallRepository.record)
    assert hints == {"call": LLMCall, "return": type(None)}


def test_incomplete_implementation_fails_at_instantiation() -> None:
    class Incomplete(Embedder):
        pass

    with pytest.raises(TypeError, match="abstract"):
        Incomplete()  # type: ignore[abstract]  # verifying the runtime rejection ABC enforces


def test_complete_implementation_instantiates() -> None:
    class Fake(Embedder):
        @property
        def model_name(self) -> str:
            return "fake"

        @property
        def dimension(self) -> int:
            return 3

        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[0.0, 0.0, 0.0] for _ in texts]

        async def aclose(self) -> None:
            pass

    assert Fake().dimension == 3


# --- Error taxonomy (usher.ports.errors) -------------------------------


def test_source_not_supported_is_a_usher_port_error() -> None:
    """Reparented under UsherPortError so a service can catch the shared
    base without knowing every port's own exception names."""
    assert issubclass(SourceNotSupported, UsherPortError)


@pytest.mark.parametrize(
    "error",
    [
        PortUnavailable,
        PortAuthFailed,
        PortRateLimited,
        RepositoryConflict,
        RepositoryNotFound,
        FilterNotSupported,
    ],
)
def test_port_errors_are_usher_port_errors(error: type[UsherPortError]) -> None:
    """A service must be able to catch UsherPortError alone and handle every
    port failure, without importing httpx or sqlalchemy -- which would break
    the `adapters are driven, not driving` and `db is driven, not driving`
    contracts that ADR-0009 rests on."""
    assert issubclass(error, UsherPortError)


def test_port_rate_limited_carries_retry_after() -> None:
    assert PortRateLimited(retry_after=30.0).retry_after == 30.0


def test_port_rate_limited_retry_after_defaults_to_none() -> None:
    assert PortRateLimited().retry_after is None


# --- LLMUsage / LLMPurpose ------------------------------------------------


def test_llm_usage_is_a_real_equatable_value() -> None:
    """Was a plain class with only a generated __init__, so equality was
    identity. Now a frozen dataclass: two calls that recorded the same
    numbers compare equal, which is what a test asserting on usage
    actually wants."""
    a = LLMUsage(
        model="gpt-4", tokens_in=10, tokens_out=5, cost_usd=Decimal("0.01"), latency_ms=200
    )
    b = LLMUsage(
        model="gpt-4", tokens_in=10, tokens_out=5, cost_usd=Decimal("0.01"), latency_ms=200
    )
    assert a == b


def test_llm_usage_cost_is_decimal_not_float() -> None:
    usage = LLMUsage(
        model="gpt-4", tokens_in=1, tokens_out=1, cost_usd=Decimal("0.001"), latency_ms=1
    )
    assert isinstance(usage.cost_usd, Decimal)


def test_llm_purpose_is_a_closed_string_vocabulary() -> None:
    assert {p.value for p in LLMPurpose} == {"curation", "query_expansion"}


# --- SearchMode ------------------------------------------------------------


def test_search_mode_fused_is_reachable() -> None:
    """The bug this replaced: `semantic: bool` could not express a third
    "fused" option, even though RRF fusion is the actual design
    (ADR-0002), not a hypothetical alongside full-text and semantic.

    Carries a `query_vector` since M6: a fused request without one is
    refused at construction, because the caller owns the model.
    """
    request = SearchRequest(query="an empty room", mode=SearchMode.FUSED, query_vector=(1.0, 0.0))
    assert request.mode is SearchMode.FUSED


# --- MetadataCandidate -------------------------------------------------


def test_metadata_candidate_uses_the_canonical_kind_vocabulary() -> None:
    """The bug this replaced: search() returning list[dict[str, Any]] made
    the match stage index into TMDb's own keys, including its movie/TV
    divergence. MetadataCandidate normalises that away before it ever
    reaches M4."""
    candidate = MetadataCandidate(
        provider_id=90000100, name="Dune", year=2021, kind=TitleKind.MOVIE, popularity=95.2
    )
    assert candidate.kind is TitleKind.MOVIE


# --- TitleRepository (the port, not the domain model) -----------------------
#
# Repositories are ports too (ADR-0009): usher.services may not import
# usher.db, so a service that needs persistence can only depend on this ABC.
# FakeTitleRepository (tests/fakes/title_repository.py) is not a throwaway
# instantiation check -- it is the in-memory double services get unit-tested
# against from M4 onward, standing in for
# usher.db.repositories.title.PostgresTitleRepository the same way a fake
# Embedder above stands in for a real one. It lives outside this module so
# M4 can import it without dragging in this file's fixtures and parametrized
# tests.
#
# The behavioural suite that used to live here (add/get round trip, reject
# duplicate, update, count_by_state, ...) moved to
# tests/contract/title_repository_contract.py (Task 10), so the identical
# assertions run against both this fake and the real, Postgres-backed
# PostgresTitleRepository instead of two hand-maintained copies drifting
# apart -- see tests/unit/test_title_repository_contract.py and
# tests/integration/test_title_repository.py's
# TestPostgresTitleRepositoryContract. What's left here is the one check
# with no real-repository counterpart to share it with: the ABC-shape
# assertion this whole file is about.


def test_complete_title_repository_implementation_instantiates() -> None:
    assert isinstance(FakeTitleRepository(), TitleRepository)


# --- SearchQueryRecord's surface -------------------------------------------


@pytest.mark.parametrize(
    ("tier", "surface"),
    [
        (None, SearchSurface.SEARCH),
        (SuggestTier.PREFIX, SearchSurface.SUGGEST),
        (SuggestTier.FUZZY, SearchSurface.SUGGEST),
    ],
)
def test_the_surface_a_row_claims_is_the_one_its_tier_implies(
    tier: SuggestTier | None, surface: SearchSurface
) -> None:
    """A search row names no tier and a suggest row names the index that
    answered, so `surface` is derived rather than stored beside `tier`.

    That is what makes the two combinations that are not states -- a search row
    with a tier, a suggest row without one -- unconstructible rather than
    refused at runtime. Exhaustive over `SuggestTier | None`, so a third member
    added to the enum and not to this list is a missing case rather than a
    silent one.

    Fails: `surface` back as a field; either arm of the derivation inverted.
    """
    record = SearchQueryRecord(
        id=new_id(),
        at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        user_id=new_id(),
        query="the quiet vacuum",
        mode=SearchMode.FULL_TEXT,
        result_count=1,
        latency_ms=1,
        tier=tier,
    )
    assert (record.surface, record.tier) == (surface, tier)
