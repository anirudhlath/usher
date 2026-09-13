"""Registering, inspecting, and removing configured sources."""

import secrets
import uuid
from collections.abc import Callable
from dataclasses import replace

from loguru import logger

from usher.domain.enums import SourceKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.ports.credentials import CredentialStore, SourceCredentials
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import SourceRepository
from usher.ports.source import SourceAdapterFactory, SourceStatus

# 24 bytes of `secrets.token_urlsafe` -- 192 bits, base64url-encoded to 32
# characters. Sized as an unguessable name rather than as a password: a ref
# is not a secret, but a *guessable* one would let anything that can address
# the credential store by ref reach a row it was never handed a pointer to.
_REF_BYTES = 24


class SourceService:
    def __init__(
        self,
        sources: SourceRepository,
        credentials: CredentialStore,
        adapters: SourceAdapterFactory,
        push_health: Callable[[uuid.UUID], bool | None] | None = None,
    ) -> None:
        self._sources = sources
        self._credentials = credentials
        self._adapters = adapters
        # The *running lane's* push health, injected by the composition root rather than
        # probed here.
        self._push_health = push_health

    async def register(
        self,
        *,
        kind: SourceKind,
        name: str,
        base_url: str,
        credentials: SourceCredentials,
    ) -> Source:
        """Persist a new source and its encrypted credentials.

        The `device_id` is generated **here, once**, and persisted -- that is
        what PRD 03's durable client actually is. An adapter that generated
        one per process would appear in Emby's dashboard as a new device
        every restart, which is the accumulating-sessions failure the design
        exists to avoid.

        The `credentials_ref` is a random token, not a function of the source
        id. A derived ref would make PRD 08's indirection decorative and make
        rotation -- write the new secret under a new ref, flip the pointer,
        delete the old row -- impossible to express.

        The source row is written *before* the credential, so the credential
        has an owner to cascade from the moment it exists. The reverse order
        can leave an encrypted row whose `owner_id` names nothing, which is
        the orphan `CredentialStore.put`'s `owner_id` exists to prevent.
        """
        source = Source(
            kind=kind,
            name=name,
            base_url=base_url,
            credentials_ref=secrets.token_urlsafe(_REF_BYTES),
            device_id=str(new_id()),
        )
        await self._sources.add(source)
        await self._credentials.put(source.credentials_ref, credentials, owner_id=source.id)
        # The name and the id, never the credential. PRD 08: credentials are
        # never logged, "including in error paths and request dumps".
        logger.info("registered source {name} ({source_id})", name=source.name, source_id=source.id)
        return source

    async def list_sources(self) -> list[Source]:
        return await self._sources.list_all()

    async def status(self, source_id: uuid.UUID) -> SourceStatus | None:
        """Connection, authentication, and push availability for one source.

        `None` only when the source itself does not exist -- every other
        outcome is a `SourceStatus`, including a source whose credential row
        has gone missing and one whose credential no longer decrypts. PRD
        08: "a degraded subsystem narrows functionality; it never fails a
        request local state can answer", and "this source is misconfigured"
        is exactly the answer an admin screen is asking for.
        """
        source = await self._sources.get(source_id)
        if source is None:
            return None
        try:
            credentials = await self._credentials.get(source.credentials_ref)
        except PortDataMalformed as exc:
            # A rotated `USHER_SECRET_KEY`, or a row restored from a backup taken under
            # a different one.
            logger.warning(
                "source {source_id} has an unreadable credential: {exc}",
                source_id=source.id,
                exc=exc,
            )
            return SourceStatus(
                reachable=False,
                authenticated=False,
                detail=(
                    "the stored credentials for this source could not be decrypted; "
                    "USHER_SECRET_KEY may have been rotated -- re-enter them to reconnect"
                ),
            )
        if credentials is None:
            # Answered without building an adapter: there is nothing to
            # authenticate with, so a probe could only spend an upstream
            # round trip (0.1253 s for the cheapest one measured -- M10 S1,
            # `.claude/rules/emby-push-and-ingest.md`) to learn what local
            # state already knows.
            return SourceStatus(
                reachable=False,
                authenticated=False,
                detail="no stored credentials for this source; re-enter them to reconnect",
            )
        adapter = self._adapters.build(source, credentials)
        # No `except UsherPortError` here, deliberately: `verify()` already promises not
        # to raise for an expected failure, so anything that does escape is a bug, and
        # catching it would hide that behind a green status.
        try:
            status = await adapter.verify()
        finally:
            await adapter.aclose()
        return self._with_lane_push_health(source_id, status)

    def _with_lane_push_health(self, source_id: uuid.UUID, status: SourceStatus) -> SourceStatus:
        """Replace `verify()`'s `push_available` with the running lane's."""
        if self._push_health is None or not status.authenticated:
            return status
        return replace(status, push_available=self._push_health(source_id))

    async def remove(self, source_id: uuid.UUID) -> bool:
        """Delete a source and its credentials.

        Returns whether it existed.

        The credential is deleted first. If the process dies between the two
        writes, what survives is a source row with no credential -- which
        `status()` reports as a misconfiguration an operator can see and fix.
        The other order would survive as an encrypted row with no owner (the
        `ON DELETE CASCADE` covers this within a transaction, but not a crash
        between two separately-committed calls), which nothing surfaces and
        nothing can attribute.
        """
        source = await self._sources.get(source_id)
        if source is None:
            return False
        await self._credentials.delete(source.credentials_ref)
        return await self._sources.delete(source_id)
