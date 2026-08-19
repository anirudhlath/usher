"""`ConfiguredSourceAdapterFactory` -- the registry, and the only module in
`src/` outside `usher.adapters.emby` that may name `EmbyAdapter`.

That last part is enforced, not asserted here: `pyproject.toml`'s sixth
import-linter contract forbids `usher.domain`, `usher.ports`,
`usher.services`, `usher.api`, and `usher.db` from reaching
`usher.adapters.emby` at all.
"""

from enum import StrEnum

import pytest
from pydantic import SecretStr

from usher.adapters.emby.adapter import EmbyAdapter
from usher.adapters.factory import ConfiguredSourceAdapterFactory
from usher.adapters.http import SourceGateRegistry
from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceNotSupported

CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))
SOURCE = Source(
    id=new_id(),
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id=str(new_id()),
)


class _FutureKind(StrEnum):
    """Stands in for the next `SourceKind` member, which does not exist yet.

    `SourceKind` has exactly one member, so the factory's `raise` cannot be
    reached through `Source`'s own constructor -- and an unreachable branch
    with no test is one an editor is free to delete.
    """

    JELLYFIN = "jellyfin"


async def test_an_emby_source_gets_an_emby_adapter() -> None:
    adapter = ConfiguredSourceAdapterFactory().build(SOURCE, CREDENTIALS)
    try:
        assert isinstance(adapter, EmbyAdapter)
        assert adapter.source_id == SOURCE.id
    finally:
        await adapter.aclose()


async def test_the_deployment_tuning_reaches_the_adapter() -> None:
    """A factory that accepted its knobs and then dropped them would build a
    working adapter with none of this deployment's configuration applied --
    default paging against a 94,395-item library, a default timeout against
    an upstream PRD 01 measures at 1-5 s per request -- and every other test
    here would still pass.

    Reaches through private attributes because there is no public accessor
    for any of the three, and inventing one purely so a test could read it
    would be a wider API for a narrower reason.
    """
    gates = SourceGateRegistry(1.25)
    factory = ConfiguredSourceAdapterFactory(
        page_size=17,
        timeout_seconds=3.5,
        reauth_cooldown_seconds=7.25,
        gates=gates,
        push_stale_after_seconds=11.5,
        push_poll_seconds=0.75,
    )
    adapter = factory.build(SOURCE, CREDENTIALS)
    try:
        assert isinstance(adapter, EmbyAdapter)
        assert adapter._page_size == 17
        assert adapter._client.timeout.read == 3.5
        assert adapter._session._reauth_cooldown == 7.25
        # The outbound gate reaches the session that sends through it
        # (ADR-0039). A factory that dropped it would build an adapter that
        # never paces a call, and the `usher.source.throttle.wait` panel would
        # be empty not because the limiter never binds but because it was never
        # wired. **The registry's gate, not a gate built from a rate**: that is
        # M10's S3, and it is asserted by identity below rather than by reading
        # `_rate` alone, because a session that minted its own gate at the same
        # rate is indistinguishable from one that shares the registry's by
        # every value assertion available.
        assert adapter._session._limiter is gates.gate(SOURCE.id, SOURCE.name)
        assert adapter._session._limiter._rate == 1.25
        # The two push knobs, and this is the whole of what makes
        # `USHER_PUSH_STALE_AFTER_SECONDS` a setting rather than a field
        # that validates and then influences nothing: the registry is the
        # only thing between `Settings` and the adapter that owns the
        # ledger, so a factory that dropped them would leave an operator
        # who widened the staleness window still reconnecting at 90 s.
        assert adapter._health.stale_after == 11.5
        assert adapter._push_poll_seconds == 0.75
    finally:
        await adapter.aclose()


async def test_each_call_builds_a_new_adapter_and_every_one_shares_the_sources_gate() -> None:
    """`SourceAdapterFactory.build`'s docstring: "the caller owns it and must
    `aclose()` it". A factory that cached one instance would hand a closed
    adapter to the next caller.

    **And the one thing that must *not* be per adapter, asserted in the same
    place because the two rules pull opposite ways.** An adapter is a
    connection pool and a message ledger, so a fresh one per call is correct;
    the outbound rate gate is a *ceiling on a server somebody else owns*, so a
    fresh one per call multiplies the configured rate by the number of
    adapters open. Two adapters for one source, one gate (ADR-0039 §4).
    """
    factory = ConfiguredSourceAdapterFactory(gates=SourceGateRegistry(0.4))
    first = factory.build(SOURCE, CREDENTIALS)
    second = factory.build(SOURCE, CREDENTIALS)
    try:
        assert first is not second
        assert isinstance(first, EmbyAdapter)
        assert isinstance(second, EmbyAdapter)
        assert first._session._limiter is second._session._limiter, (
            "two adapters for one source paced independently, so this deployment "
            "spends 2 x USHER_SOURCE_REQUESTS_PER_SECOND against that server"
        )
    finally:
        await first.aclose()
        await second.aclose()


def test_an_unregistered_kind_is_refused_rather_than_defaulted() -> None:
    """The next `SourceKind` member must land on the `raise`, not on an Emby
    adapter pointed at something that is not Emby -- which would
    authenticate, walk, and return plausible nonsense rather than fail.

    `model_construct` deliberately bypasses `Source`'s validation: it is the
    only way to stand a not-yet-existing enum member up today, and the object
    it produces never leaves this test.
    """
    unsupported = Source.model_construct(**{**SOURCE.model_dump(), "kind": _FutureKind.JELLYFIN})
    with pytest.raises(SourceNotSupported, match="jellyfin"):
        ConfiguredSourceAdapterFactory().build(unsupported, CREDENTIALS)
