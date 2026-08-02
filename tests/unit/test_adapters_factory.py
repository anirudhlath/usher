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
    factory = ConfiguredSourceAdapterFactory(
        page_size=17,
        timeout_seconds=3.5,
        reauth_cooldown_seconds=7.25,
        push_stale_after_seconds=11.5,
        push_poll_seconds=0.75,
    )
    adapter = factory.build(SOURCE, CREDENTIALS)
    try:
        assert isinstance(adapter, EmbyAdapter)
        assert adapter._page_size == 17
        assert adapter._client.timeout.read == 3.5
        assert adapter._session._reauth_cooldown == 7.25
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


async def test_each_call_builds_a_new_adapter() -> None:
    """`SourceAdapterFactory.build`'s docstring: "the caller owns it and must
    `aclose()` it". A factory that cached one instance would hand a closed
    adapter to the next caller."""
    factory = ConfiguredSourceAdapterFactory()
    first = factory.build(SOURCE, CREDENTIALS)
    second = factory.build(SOURCE, CREDENTIALS)
    try:
        assert first is not second
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
