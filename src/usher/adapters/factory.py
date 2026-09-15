"""The one place a `SourceKind` becomes a concrete adapter."""

from usher.adapters.emby.adapter import EmbyAdapter
from usher.adapters.emby.push import DEFAULT_POLL_SECONDS, DEFAULT_STALE_AFTER_SECONDS
from usher.adapters.http import SourceGateRegistry
from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceAdapter, SourceAdapterFactory, SourceNotSupported


class ConfiguredSourceAdapterFactory(SourceAdapterFactory):
    """Builds adapters with this deployment's tuning applied.

    Named for what it does rather than for a service, because it is not one --
    it is the registry. Its settings come from `usher.config.Settings` at the
    composition root, so no adapter has to read configuration itself.

    **The outbound gate is the one thing here that is an object rather than a
    value.** Every other knob is a number copied into each adapter, so two
    factories configured alike are interchangeable; a rate limiter is not. A
    *value* threaded down mints a fresh gate per adapter, and
    `usher.composition.adapter_factory` runs once per unit of work -- a fresh
    gate per lane task and per request. Holding the shared `SourceGateRegistry`
    and handing out **its** gate is what makes the ceiling per source per
    process rather than per pipeline.
    """

    def __init__(
        self,
        *,
        page_size: int = 200,
        timeout_seconds: float = 30.0,
        reauth_cooldown_seconds: float = 60.0,
        gates: SourceGateRegistry | None = None,
        push_stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        push_poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._page_size = page_size
        self._timeout_seconds = timeout_seconds
        self._reauth_cooldown_seconds = reauth_cooldown_seconds
        # `None` is a factory nobody handed a registry to -- a directly constructed one
        # in a test.
        self._gates = gates if gates is not None else SourceGateRegistry()
        self._push_stale_after_seconds = push_stale_after_seconds
        self._push_poll_seconds = push_poll_seconds

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        """Construct the adapter for `source.kind`; the caller owns it.

        The `raise` below is unreachable while `SourceKind` has one member, and
        is kept rather than collapsed into an unconditional `return` so that the
        *next* member lands on it rather than on an Emby adapter pointed at a
        Jellyfin server, which would authenticate, walk and return plausible
        nonsense.
        """
        if source.kind is SourceKind.EMBY:
            return EmbyAdapter(
                source,
                credentials,
                page_size=self._page_size,
                timeout_seconds=self._timeout_seconds,
                reauth_cooldown_seconds=self._reauth_cooldown_seconds,
                limiter=self._gates.gate(source.id, source.name),
                push_stale_after_seconds=self._push_stale_after_seconds,
                push_poll_seconds=self._push_poll_seconds,
            )
        raise SourceNotSupported(f"no adapter is registered for source kind {source.kind}")
