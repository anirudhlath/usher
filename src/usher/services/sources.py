"""Registering, inspecting, and removing configured sources.

Depends only on `domain/` and `ports/` (PRD 01, layering rule 2): it never
names `EmbyAdapter`, it receives a `SourceAdapterFactory`. ADR-0009 is the
other half of the same rule -- repositories are ports, so this never imports
`db/` either, and every test below runs against port fakes.

**Adapters are built per call and closed immediately.** That is wasteful --
each one authenticates from scratch -- and it is right for M3: a long-lived
adapter is a long-lived connection pool and, from M5, a long-lived
WebSocket, and the thing that owns those is the push lane's registry, which
does not exist yet. Building a pooled registry here would mean designing the
lifecycle for a consumer that has not been written. What matters now is that
nothing *leaks*: every adapter this service builds is closed in a `finally`.
"""

import secrets
import uuid

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
    ) -> None:
        self._sources = sources
        self._credentials = credentials
        self._adapters = adapters

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
            # A rotated `USHER_SECRET_KEY`, or a row restored from a backup
            # taken under a different one. `CredentialStore.get` raises for
            # this rather than returning `None` precisely because it is
            # diagnosable and fixable -- and the screen an operator would
            # look at to diagnose it is this one, so a 500 here would answer
            # the question with a stack trace.
            #
            # The detail returned is a fixed string, *not* `str(exc)`: the
            # store builds its message around `credentials_ref=...` so an
            # operator can find the row, which is right for the log line
            # below and wrong for a response body. The ref is sized as
            # unguessable (see `_REF_BYTES`), so handing it to a client
            # gives away a pointer it was never issued.
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
            # authenticate with, so a probe could only spend a 1-5 s upstream
            # round trip to learn what local state already knows.
            return SourceStatus(
                reachable=False,
                authenticated=False,
                detail="no stored credentials for this source; re-enter them to reconnect",
            )
        adapter = self._adapters.build(source, credentials)
        # No `except UsherPortError` here, deliberately: `verify()` already
        # promises not to raise for an expected failure, so anything that
        # does escape is a bug, and catching it would hide that behind a
        # green status. The `finally` is what this needs -- one adapter is
        # one connection pool, and a status endpoint a dashboard polls would
        # otherwise leak one per call.
        try:
            return await adapter.verify()
        finally:
            await adapter.aclose()

    async def remove(self, source_id: uuid.UUID) -> bool:
        """Delete a source and its credentials. Returns whether it existed.

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
