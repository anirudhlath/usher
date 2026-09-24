"""The seam that makes `SourceAdapterContract` source-agnostic."""

from abc import ABC, abstractmethod

from pydantic import AwareDatetime

from usher.domain.source import Source
from usher.ports.source import SourceAdapter, SourceEvent, SourceItem, SourceWatchState


class SourceHarness(ABC):
    @property
    @abstractmethod
    def source(self) -> Source:
        """The `Source` the adapter under test was configured with."""

    @property
    @abstractmethod
    def adapter(self) -> SourceAdapter:
        """The adapter under test.

        The same instance for the whole test.
        """

    @abstractmethod
    async def given_item(self, item: SourceItem, *, changed_at: AwareDatetime) -> None:
        """Make the source hold `item`, last changed at `changed_at`."""

    @abstractmethod
    async def given_watch_state(self, state: SourceWatchState) -> None:
        """Make the source hold `state` for `state.external_id`."""

    @abstractmethod
    async def remove_item(self, external_id: str) -> None:
        """Delete an item from the source, as a user deleting a file would."""

    @abstractmethod
    async def recorded_watch_state(self, external_id: str) -> tuple[int, bool] | None:
        """`(position_seconds, played)` as the source now holds it, or `None`.

        Read back from the source's own state, never from a log of calls the adapter
        made -- a harness that recorded "push_watch_state was called" would pass against
        an adapter that called the wrong upstream endpoint and got a 200 from something
        that ignored it.
        """

    @abstractmethod
    async def go_offline(self) -> None:
        """Make every subsequent request fail at the transport layer.

        The way an unplugged server or a dead DNS entry does -- not a 5xx, because a
        transport failure is the case an adapter is most likely to translate wrongly.
        """

    @abstractmethod
    async def fail_after_items(self, count: int) -> None:
        """Serve at least `count` items successfully during a walk, then fail.

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
        """Invalidate the adapter's *session*, leaving the stored credentials correct.

        A source with no expiring session may implement this as a no-op; the contract's
        assertions still hold -- the operation succeeds, and no storm of
        authentications follows.
        """

    @abstractmethod
    def authentications(self) -> int:
        """How many times the source has been asked to authenticate since construction.

        `0` for a source with no authentication step.
        """

    def observed_overlap(self) -> int | None:
        """The greatest number of upstream requests this harness saw in flight at once.

        `None` if it cannot tell -- the default, which a harness with no transport to
        instrument leaves in place.
        """
        return None

    # -- push ------------------------------------------------------------

    @abstractmethod
    async def push_event(self, event: SourceEvent) -> None:
        """Make the source's push channel deliver this event."""

    @abstractmethod
    async def push_silence(self) -> None:
        """Deliver nothing from here on, without closing the channel.

        The connection is open, the subscription was accepted, and nothing arrives. An
        implementation that closed the connection instead would be arranging a
        *different* failure -- one every WebSocket library already detects.

        **Whatever is already queued must also stop arriving.** Otherwise this is a
        no-op in the only case that reads it, and a harness could implement it as `pass`
        with the suite still green.
        """

    @abstractmethod
    async def push_drop(self) -> None:
        """Break the channel as a peer disconnect does.

        Distinct from `push_silence`: this one must surface as a raise
        promptly rather than after a staleness window.
        """

    @abstractmethod
    async def advance_push_clock(self, seconds: float) -> None:
        """Move the adapter's push clock forward.

        A staleness window is a duration, and a contract case that slept
        through one would add it to every run of this suite, twice. A
        harness that cannot control its adapter's clock returns `False` from
        `can_advance_push_clock` below, and the two staleness cases skip --
        the same shape `observed_overlap` uses, and for the same reason:
        claiming a capability you do not have ratifies an implementation
        this suite never exercised.
        """

    def can_advance_push_clock(self) -> bool:
        """Whether `advance_push_clock` does anything.

        Default `False`.
        """
        return False

    def push_stale_after(self) -> float:
        """The adapter's staleness window.

        A case steps past it without hard-coding a constant that belongs to the
        implementation. Only ever called after `can_advance_push_clock`, which is why
        this may raise rather than return a number a harness would have to invent.
        """
        raise NotImplementedError

    def can_disable_push(self) -> bool:
        """Whether this harness can arrange an adapter with no push channel at all.

        Default `False`.
        """
        return False

    async def disable_push(self) -> None:
        """Leave the adapter with no push channel, so `events()` raises `SourceNotSupported`.

        Not every adapter has such a state and `EmbyAdapter` is one that
        does not -- it always has a channel to offer and finds out
        afterwards whether it delivers. So this is the `observed_overlap`
        shape rather than an abstract method: a capability declined, with
        the case skipping, rather than a capability faked. The
        implementation it exists for is a Jellyfin adapter behind a proxy
        that strips `Upgrade`.
        """
        raise NotImplementedError

    @abstractmethod
    async def aclose(self) -> None:
        """Tear the harness down.

        Not the same as `adapter.aclose()` -- the contract closes the adapter itself in
        some cases, and this must still be safe afterwards.
        """
