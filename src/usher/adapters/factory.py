"""The one place a `SourceKind` becomes a concrete adapter.

PRD 01 lists "additional sources" as an extension seam left open in v1. This
module is that seam's actual hinge: a Jellyfin adapter adds a member to
`SourceKind`, an implementation under `usher/adapters/jellyfin/`, and one
branch below. Nothing in `services/` or `api/` moves, because neither ever
names an adapter class -- they hold a `SourceAdapterFactory`.

Lives in `adapters/`, not `services/`, because it imports every adapter and
`services/` may depend only on `domain/` and `ports/` (PRD 01, layering
rule 2). The composition roots -- `usher.api.deps` and `usher.cli` -- are the
only things allowed to construct one, and `pyproject.toml`'s sixth
import-linter contract ("no concrete source adapter escapes its package") is
what keeps that true rather than customary.
"""

from usher.adapters.emby.adapter import EmbyAdapter
from usher.adapters.emby.push import DEFAULT_POLL_SECONDS, DEFAULT_STALE_AFTER_SECONDS
from usher.adapters.http import SourceGateRegistry
from usher.domain.enums import SourceKind
from usher.domain.source import Source
from usher.ports.credentials import SourceCredentials
from usher.ports.source import SourceAdapter, SourceAdapterFactory, SourceNotSupported


class ConfiguredSourceAdapterFactory(SourceAdapterFactory):
    """Builds adapters with this deployment's tuning applied.

    Named for what it does rather than for a service, because it is not one
    -- it is the registry. The settings it carries come from
    `usher.config.Settings` at the composition root, so no adapter has to
    read configuration itself.

    **The outbound gate is the one thing here that is an object rather than a
    value, and that is the whole of M10's S3.** Every other knob below is a
    number this factory copies into each adapter it builds, so two factories
    configured alike are interchangeable. A rate limiter is not: a *value*
    threaded down mints a fresh gate per adapter, and since
    `usher.composition.adapter_factory` is called once per unit of work, that
    is a fresh gate per lane task and per request. So this holds the shared
    `SourceGateRegistry` and hands out **its** gate, which is what makes the
    ceiling per source per process rather than per pipeline (ADR-0039 §4).
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
        # `None` is a factory nobody handed a registry to -- a directly
        # constructed one in a test. It gets a private registry at the default
        # unlimited rate rather than `None`, so `build` has one shape and an
        # unconfigured factory still shares a gate across the adapters *it*
        # builds. A default-constructed `SourceGateRegistry()` reads no
        # configuration, which is what keeps `usher.config` out of this module.
        self._gates = gates if gates is not None else SourceGateRegistry()
        # The two push knobs travel the same route as the three above: from
        # `Settings` at a composition root, through this registry, into the
        # adapter that owns the message ledger. Defaulted from the adapter
        # package's own constants rather than repeated as literals, so
        # `usher.adapters.emby.push` stays the single definition of what a
        # channel does when nobody configures it.
        self._push_stale_after_seconds = push_stale_after_seconds
        self._push_poll_seconds = push_poll_seconds

    def build(self, source: Source, credentials: SourceCredentials) -> SourceAdapter:
        """Construct the adapter for `source.kind`. The caller owns it.

        The `raise` below is unreachable today -- `SourceKind` has exactly
        one member -- and is kept rather than collapsed into an unconditional
        `return` precisely because of that: the *next* member added must land
        on it rather than on a silently-wrong Emby adapter pointed at a
        Jellyfin server, which would authenticate, walk, and return plausible
        nonsense. `tests/unit/test_adapters_factory.py` stands a
        not-yet-existing kind up to prove it does.
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
