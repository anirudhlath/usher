"""SourceService against port fakes. No network, no database.

The first service in the codebase to be driven entirely through ports --
`BootstrapService` already was, and this one inherits the pattern: ADR-0009
makes repositories ports, so `services/` may not import `db/`, and every
dependency here is either a domain object or a port fake.
"""

import uuid
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from tests.fakes.credential_store import FakeCredentialStore
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.source_repository import FakeSourceRepository
from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.errors import PortDataMalformed
from usher.ports.source import SourceAdapter, SourceAdapterFactory, SourceStatus
from usher.services.sources import SourceService

CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))


class RecordingFactory(SourceAdapterFactory):
    """Counts what the service builds and closes, and can hand back an
    adapter whose credentials the source rejects."""

    def __init__(self, *, reject: bool = False) -> None:
        self.built: list[tuple[Source, SourceCredentials]] = []
        self.closed = 0
        self._reject = reject

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        self.built.append((source, credentials))
        adapter = _CountingAdapter(source, self)
        if self._reject:
            adapter.reject_credentials()
        return adapter


class _UndecryptableStore(FakeCredentialStore):
    """A store whose rows are present and unreadable -- what a rotated
    `USHER_SECRET_KEY` leaves behind. Deliberately not a capability on
    `FakeCredentialStore` itself: the contract suite runs against that fake,
    and a store that can be told to fail its own contract is a fake with a
    mode nothing in `src/` can produce."""

    async def get(self, ref: str) -> SourceCredentials | None:
        raise PortDataMalformed(
            "stored source credentials could not be decrypted", detail=f"credentials_ref={ref}"
        )


class _CountingAdapter(FakeSourceAdapter):
    def __init__(self, source: Source, factory: RecordingFactory) -> None:
        super().__init__(source)
        self._factory = factory

    async def aclose(self) -> None:
        self._factory.closed += 1
        await super().aclose()


def _service(
    repo: FakeSourceRepository | None = None,
    store: FakeCredentialStore | None = None,
    factory: RecordingFactory | None = None,
    push_health: Callable[[uuid.UUID], bool | None] | None = None,
) -> tuple[SourceService, FakeSourceRepository, FakeCredentialStore, RecordingFactory]:
    repo = repo or FakeSourceRepository()
    store = store or FakeCredentialStore()
    factory = factory or RecordingFactory()
    return SourceService(repo, store, factory, push_health), repo, store, factory


async def test_register_persists_a_source_and_its_credentials() -> None:
    service, repo, store, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY,
        name="Living Room Emby",
        base_url="https://emby.invalid",
        credentials=CREDENTIALS,
    )
    stored = await repo.get(source.id)
    assert stored is not None
    assert stored.name == "Living Room Emby"
    secret = await store.get(stored.credentials_ref)
    assert secret is not None
    assert secret.password.get_secret_value() == "correct-horse-battery"


async def test_the_credential_is_owned_by_the_source_it_was_registered_for() -> None:
    """`CredentialStore.put`'s `owner_id` is what lets a backing store
    cascade the delete, which is the only thing standing between a crash
    mid-`remove` and an encrypted row nobody can attribute. Passing the
    wrong id -- or none -- type-checks and passes every other test here."""
    service, _, store, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert store.owner_of(source.credentials_ref) == source.id


async def test_register_generates_a_stable_device_id() -> None:
    """PRD 03: the DeviceId is generated *once* and persisted, so Usher is
    one device in Emby's dashboard rather than an accumulating pile of
    sessions. Generating it here, at registration, is what makes that
    true -- an adapter that made one up per process could not."""
    service, _, _, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY,
        name="A",
        base_url="https://emby.invalid",
        credentials=CREDENTIALS,
    )
    assert source.device_id
    uuid.UUID(source.device_id)


async def test_the_persisted_device_id_is_the_one_that_was_returned() -> None:
    """ "Generated once and persisted" is two claims. A service that returned
    a fresh `Source` while storing a differently-stamped one would satisfy
    the test above and still hand Emby a new device every registration."""
    service, repo, _, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    stored = await repo.get(source.id)
    assert stored is not None
    assert stored.device_id == source.device_id
    assert stored.credentials_ref == source.credentials_ref


async def test_two_sources_get_different_device_ids_and_refs() -> None:
    """Rules out a constant. A shared DeviceId would make two Emby servers
    fight over one session identity; a shared credentials_ref would make
    the second registration overwrite the first's password."""
    service, _, _, _ = _service()
    first = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    second = await service.register(
        kind=SourceKind.EMBY, name="B", base_url="https://b.invalid", credentials=CREDENTIALS
    )
    assert first.device_id != second.device_id
    assert first.credentials_ref != second.credentials_ref


async def test_the_credentials_ref_is_not_derived_from_the_source_id() -> None:
    """PRD 08 calls `credentials_ref` an indirection. A ref that is just
    the id spelled differently is not one, and rotation -- write the new
    secret under a new ref, flip the pointer, delete the old -- stops being
    expressible."""
    service, _, _, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert str(source.id) not in source.credentials_ref
    assert source.id.hex not in source.credentials_ref


async def test_status_verifies_through_a_freshly_built_adapter() -> None:
    service, _, _, factory = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert status.reachable is True
    assert factory.built[0][0].id == source.id
    assert factory.built[0][1].password.get_secret_value() == "correct-horse-battery"


async def test_status_closes_the_adapter_it_built() -> None:
    """One adapter owns one connection pool. A status endpoint that leaked
    one per call would exhaust file descriptors on a dashboard that polls."""
    service, _, _, factory = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    await service.status(source.id)
    await service.status(source.id)
    assert factory.closed == 2


async def test_status_closes_the_adapter_even_when_verify_raises() -> None:
    """`verify()` promises not to raise for an *expected* failure, so the
    service deliberately has no `except` around it -- a bug there must stay
    loud. The `finally` is a separate guarantee, and the one that keeps a
    bug in `verify` from also being a connection-pool leak."""

    class _Exploding(_CountingAdapter):
        async def verify(self) -> SourceStatus:
            raise RuntimeError("verify is broken")

    class _ExplodingFactory(RecordingFactory):
        def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
            self.built.append((source, credentials))
            return _Exploding(source, self)

    factory = _ExplodingFactory()
    service, _, _, _ = _service(factory=factory)
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    # Not caught and turned into a status: a service that swallowed this
    # would report a healthy source built on a broken `verify`.
    with pytest.raises(RuntimeError, match="verify is broken"):
        await service.status(source.id)
    assert factory.closed == 1


async def test_status_is_none_for_an_unknown_source() -> None:
    service, _, _, _ = _service()
    assert await service.status(uuid.uuid4()) is None


async def test_status_reports_missing_credentials_rather_than_crashing() -> None:
    """A source row whose credential row was deleted out from under it is
    an operator-visible misconfiguration, not a 500. PRD 08's degradation
    rule: narrow the functionality, never fail the request."""
    service, _, store, factory = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    await store.delete(source.credentials_ref)
    status = await service.status(source.id)
    assert status is not None
    assert status.authenticated is False
    assert status.detail is not None
    # No adapter was built for a source there is no credential for: building
    # one would authenticate against the upstream with nothing to send.
    assert factory.built == []


async def test_status_reports_an_undecryptable_credential_rather_than_crashing() -> None:
    """The other half of the same rule, and the likelier half in practice:
    `USHER_SECRET_KEY` was rotated, or the row was restored from a backup
    taken under a different key. `CredentialStore.get` raises
    `PortDataMalformed` for exactly that (it is a diagnosable failure, not
    an absent row), and `GET /admin/sources/{id}/status` is the one screen
    an operator would look at to find out. A 500 there tells them nothing
    and looks like a bug in Usher rather than a key mismatch they can fix.
    """
    service, _, _, factory = _service(store=_UndecryptableStore())
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert (status.reachable, status.authenticated) == (False, False)
    assert status.detail is not None
    assert factory.built == []


async def test_the_undecryptable_detail_does_not_name_the_credentials_ref() -> None:
    """`PortDataMalformed`'s own `detail` carries `credentials_ref=...` so
    an operator can find the row -- correct for a log line, wrong for a
    response body. The ref is sized as unguessable (`usher.services.
    sources`) precisely so that holding one is a capability; interpolating
    the store's exception into a rendered status would hand it to any
    client that can reach the admin API."""
    service, _, _, _ = _service(store=_UndecryptableStore())
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None and status.detail is not None
    assert source.credentials_ref not in status.detail


async def test_remove_deletes_the_source_and_its_credentials() -> None:
    service, repo, store, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert await service.remove(source.id) is True
    assert await repo.get(source.id) is None
    assert await store.get(source.credentials_ref) is None


async def test_remove_reports_an_unknown_source() -> None:
    service, _, _, _ = _service()
    assert await service.remove(uuid.uuid4()) is False


async def test_remove_leaves_no_credential_behind_for_another_source() -> None:
    """Removing one source must not take another's secret with it -- which
    a `delete` keyed on anything shared (or on a ref derived from a constant)
    would do."""
    service, _, store, _ = _service()
    first = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    second = await service.register(
        kind=SourceKind.EMBY, name="B", base_url="https://b.invalid", credentials=CREDENTIALS
    )
    await service.remove(first.id)
    assert await store.get(second.credentials_ref) is not None


async def test_list_sources_returns_what_was_registered() -> None:
    service, _, _, _ = _service()
    await service.register(
        kind=SourceKind.EMBY, name="Zeta", base_url="https://z.invalid", credentials=CREDENTIALS
    )
    await service.register(
        kind=SourceKind.EMBY, name="Alpha", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert [source.name for source in await service.list_sources()] == ["Alpha", "Zeta"]


async def test_the_service_never_returns_a_credential() -> None:
    """PRD 08: "Credentials are never returned by any API, including admin.
    Write-only." `Source` cannot carry one -- it has only the ref -- and
    this asserts the service does not smuggle one out some other way."""
    service, _, _, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    assert "correct-horse-battery" not in repr(source)
    assert "correct-horse-battery" not in repr(await service.list_sources())
    assert "correct-horse-battery" not in repr(await service.status(source.id))


async def test_a_rejected_credential_is_reported_not_raised() -> None:
    """`GET /admin/sources/{id}/status` renders this state rather than
    handling it: `SourceAdapter.verify()` already returns rather than
    raising, and the service must not reintroduce an exception path on top
    of it. Distinguished from the missing-credentials case above, which is
    the service's own answer -- this one comes from the adapter."""
    service, _, _, _ = _service(factory=RecordingFactory(reject=True))
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert status.reachable is True
    assert status.authenticated is False


async def test_status_reports_the_running_lanes_push_health() -> None:
    """`verify()` opens no socket, so a freshly built adapter can only ever
    answer `None` here -- a status screen a dashboard polls must not cost a
    WebSocket handshake per poll. The **running lane's** adapter has a real,
    message-grounded answer, and this route is where an operator reads it.
    """
    service, _, _, _ = _service(push_health=lambda _: True)
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert status.push_available is True


async def test_status_reports_null_when_no_lane_is_running() -> None:
    """ "Not probed", which is a different answer from "push is broken" and
    is the honest one for a source whose lane has not started -- rendering
    it as `False` would show an unperformed check as a performed one."""
    service, _, _, _ = _service(push_health=lambda _: None)
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert status.push_available is None


async def test_status_without_a_lane_reader_leaves_verifys_own_answer_alone() -> None:
    """The default, and what `usher.cli` gets: no lanes in this process, so
    nothing to report, and the adapter's own `None` stands."""
    service, _, _, _ = _service()
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert status.push_available is None


async def test_a_lane_reporting_push_on_an_unauthenticated_source_is_not_a_500() -> None:
    """`SourceStatus.__post_init__` refuses "push available without being
    authenticated", and `dataclasses.replace` re-runs it -- so the obvious
    one-liner raises `ValueError` out of the admin status route for a state
    a real deployment reaches: a lane that was delivering a second ago
    against a source whose password has just been rotated.

    The honest answer is the adapter's own: the operator's problem is the
    authentication, and claiming a working push channel on a source that
    cannot authenticate would be the more misleading of the two.
    """
    service, _, _, _ = _service(factory=RecordingFactory(reject=True), push_health=lambda _: True)
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    status = await service.status(source.id)
    assert status is not None
    assert status.authenticated is False
    assert status.push_available is None


async def test_status_asks_the_lane_about_the_source_it_was_asked_about() -> None:
    """A reader keyed on the wrong id answers about a different server, and
    with one source configured every other case here would still pass."""
    seen: list[uuid.UUID] = []

    def record(source_id: uuid.UUID) -> bool | None:
        seen.append(source_id)
        return True

    service, _, _, _ = _service(push_health=record)
    source = await service.register(
        kind=SourceKind.EMBY, name="A", base_url="https://a.invalid", credentials=CREDENTIALS
    )
    await service.status(source.id)
    assert seen == [source.id]
