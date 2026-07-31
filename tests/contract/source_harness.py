"""The seam that makes `SourceAdapterContract` source-agnostic.

The contract suite never constructs an adapter, never touches HTTP, and
never mentions a wire format. It arranges state through this ABC, whose
whole vocabulary is `usher.ports.source`'s own DTOs, and each
implementation translates that into whatever its upstream actually needs:
`FakeSourceHarness` stores the DTOs directly, `EmbyHarness` renders them
into Emby JSON served by an in-memory server. A Jellyfin or Plex adapter
adds a third implementation of this ABC and the suite runs unchanged --
which is the only sense in which "the abstraction is real" is a testable
claim rather than an aspiration.

Every mutator is `async` even though both M3 implementations are
synchronous. The strongest form of this suite is one driven against a live
server, and that harness has to await; paying the `await` noise now is
cheaper than rewriting thirty tests later.
"""

from abc import ABC, abstractmethod

from pydantic import AwareDatetime

from usher.domain.source import Source
from usher.ports.source import SourceAdapter, SourceItem, SourceWatchState


class SourceHarness(ABC):
    @property
    @abstractmethod
    def source(self) -> Source:
        """The `Source` the adapter under test was configured with."""

    @property
    @abstractmethod
    def adapter(self) -> SourceAdapter:
        """The adapter under test. The same instance for the whole test."""

    @abstractmethod
    async def given_item(self, item: SourceItem, *, changed_at: AwareDatetime) -> None:
        """Make the source hold `item`, last changed at `changed_at`.

        `changed_at` is what a `since` cursor filters on, and it is separate
        from `SourceItem.added_at` on purpose: an item added last year and
        edited this morning must be found by a delta walk, and a DTO field
        named `added_at` cannot express that.

        An implementation renders `item` into its own upstream's shape. It
        must round-trip every field it is given -- the point of this hook is
        that `adapter.get_item(item.external_id)` returns something equal to
        `item` in the fields the port promises. Rendering a field only when
        it is set is the way this is usually broken: whatever the
        implementation's own template holds shows through instead, and a
        contract test then asserts happily on a value it never supplied.

        Two widenings are permitted, and an implementation that takes
        either must **say so in its own docstring** rather than leave it to
        be discovered:

        - `changed_at` may be held at a coarser resolution than a datetime
          carries -- Emby's date filters take whole seconds -- so a `since`
          window may return a superset. The port already permits that;
          callers deduplicate by `external_id`.
        - an item with no `container` is a folder to an upstream that
          models one, and a folder has no media source to hang codecs, a
          file size, a channel count, or an HDR format off. Those fields
          may read back `None` for such an item. Nothing seeds one: a
          source reporting a codec for something it cannot play is not a
          shape any source produces.
        """

    @abstractmethod
    async def given_watch_state(self, state: SourceWatchState) -> None:
        """Make the source hold `state` for `state.external_id`."""

    @abstractmethod
    async def remove_item(self, external_id: str) -> None:
        """Delete an item from the source, as a user deleting a file would."""

    @abstractmethod
    async def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        """`(position_seconds, played)` as the source now holds it after a
        `push_watch_state`, or `None` if nothing was ever written.

        Read back from the source's own state, never from a log of calls the
        adapter made -- a harness that recorded "push_watch_state was
        called" would pass against an adapter that called the wrong upstream
        endpoint and got a 200 from something that ignored it.
        """

    @abstractmethod
    async def go_offline(self) -> None:
        """Make every subsequent request fail at the transport layer, the
        way an unplugged server or a dead DNS entry does. Not a 5xx: a
        transport failure is the case an adapter is most likely to translate
        wrongly."""

    @abstractmethod
    async def fail_after_items(self, count: int) -> None:
        """Serve at least `count` items successfully during a walk, then
        fail.

        "At least" because upstreams page, and a page boundary rarely lands
        exactly on `count`: an implementation that serves items in pages of
        two will serve four before failing when asked for three. The
        contract only asserts that `count` items arrived before the failure
        did, which is what distinguishes a streaming walk from one that
        materialised the library and raised before yielding anything.
        """

    @abstractmethod
    async def reject_credentials(self) -> None:
        """Make the stored credentials wrong, as a changed password does.

        Must also invalidate any live session. Without that, an adapter that
        already authenticated keeps working and every assertion about
        rejected credentials passes vacuously.
        """

    @abstractmethod
    async def expire_credentials(self) -> None:
        """Invalidate the adapter's *session*, leaving the stored
        credentials correct -- the exact failure that motivated this
        project, where a token in a Home Assistant dashboard silently began
        returning 401 with no way to renew it.

        A source with no expiring session may implement this as a no-op; the
        contract's assertions still hold (the operation succeeds, and no
        storm of authentications follows).
        """

    @abstractmethod
    def authentications(self) -> int:
        """How many times the source has been asked to authenticate since
        the harness was created. `0` for a source with no authentication
        step."""

    def observed_overlap(self) -> int | None:
        """The greatest number of upstream requests this harness saw in
        flight at once, or `None` if it cannot tell. Optional: the default
        is `None`, and a harness with no transport to instrument leaves it
        there.

        This exists because `test_operations_recover_from_an_expired_
        credential` cannot otherwise mean what it looks like it means. It
        fires four `asyncio.gather`-ed calls and asserts at most one
        authentication follows, which reads as a single-flight assertion --
        and is not one. Measured directly: over `httpx.MockTransport`, with
        *both* of `EmbySession`'s locks deleted, four concurrent expired
        sessions still produce exactly one authentication. Nothing in that
        transport ever really awaits on the way to its handler, so the
        event loop tends to run one gathered call all the way through its
        own re-auth before starting the next, and every other call then
        observes an already-fresh token without ever racing for it.

        A harness that returns a number here is claiming its transport
        genuinely overlaps -- which needs a real `await` in the request
        path, not a synchronous handler. `EmbyHarness`
        (`tests/fakes/emby_harness.py`) makes that claim and backs it by
        running on `tests/fakes/slow_transport.py`; over it, each of the
        three mutations named above does fail that case. `FakeSourceHarness`
        leaves this at `None`, which is not a failure -- it is the honest
        answer for an adapter with no transport at all, and it is why the
        contract states in that test's own docstring what a green run does
        and does not prove for each.
        """
        return None

    @abstractmethod
    async def aclose(self) -> None:
        """Tear the harness down. Not the same as `adapter.aclose()` -- the
        contract closes the adapter itself in some cases, and this must
        still be safe afterwards."""
