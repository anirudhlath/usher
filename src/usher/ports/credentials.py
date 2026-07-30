"""Port for the credentials a source adapter authenticates with.

PRD 08: source credentials are **encrypted at rest** under
`USHER_SECRET_KEY`, `Source.credentials_ref` points at the encrypted row,
and the plaintext exists only in memory in the adapter that needs it. This
port is that indirection made concrete — a service holds a
`credentials_ref`, asks a `CredentialStore` for the secret, hands it
straight to a `SourceAdapter`, and never persists, returns, or logs it.

Separate from `SourceRepository` on purpose. Both could have been one port
with a `credentials` field on `Source`, and that is exactly the shape PRD
08's "credentials are never returned by any API, including admin" is
hardest to hold: every read of a source would carry the secret, and
write-only would be a convention enforced by whoever remembered. Splitting
them makes the read of a credential a deliberate, separately-auditable call
that the admin API simply never makes.

`password` is a `pydantic.SecretStr`, not a `str`, so the never-logged rule
is enforced by the type system rather than by discipline: `repr()` and
`str()` of a `SecretStr` are `'**********'`, so a credential cannot reach a
log line, a loguru record, a traceback frame summary, or an exception
message by accident. `usher.config.Settings` already holds `database_url`,
`secret_key`, and `tmdb_api_key` the same way.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from pydantic import SecretStr


@dataclass(frozen=True)
class SourceCredentials:
    """What a source needs in order to authenticate. Plaintext, in memory
    only, for the lifetime of one adapter.

    A plain dataclass rather than a `DomainModel` for the same reason
    `SourceItem` is one: it crosses a port boundary, it is never persisted
    in this shape, and it is never revalidated on the way back in.
    """

    username: str
    password: SecretStr


class CredentialStore(ABC):
    """Encrypted-at-rest storage for `SourceCredentials`, addressed by an
    opaque `credentials_ref`.

    The ref is opaque and unguessable rather than derived from the source id
    (`f"source:{id}"` would have worked and been simpler): a derived ref
    makes the indirection decorative, and rotation — write the new secret
    under a new ref, flip `Source.credentials_ref`, delete the old row —
    stops being expressible at all. `owner_id` exists so a backing store can
    cascade the delete when its owner goes away, which is what stops a
    crash between "delete the source" and "delete its credential" from
    leaving an encrypted orphan nobody can attribute.
    """

    @abstractmethod
    async def put(self, ref: str, credentials: SourceCredentials, *, owner_id: uuid.UUID) -> None:
        """Store (or replace) the credentials at `ref`.

        An upsert, not an insert: re-registering a source with a corrected
        password must overwrite, and rotation writes over the same ref.
        Same session/transaction ownership as `TitleRepository` — flushes,
        never commits.
        """

    @abstractmethod
    async def get(self, ref: str) -> SourceCredentials | None:
        """Decrypt and return the credentials at `ref`, or `None` if no such
        ref exists.

        `None` means "nothing is stored here" and nothing else. A stored
        value that cannot be *decrypted* — the key was rotated, the row was
        corrupted — raises `PortDataMalformed` (`usher.ports.errors`)
        instead, because retrying will not help and the operator has to
        re-enter the credential or restore the key. Returning `None` for
        that case would present a recoverable, operator-visible problem as
        an absent source.
        """

    @abstractmethod
    async def delete(self, ref: str) -> None:
        """Remove the credentials at `ref`. Idempotent: deleting a ref that
        does not exist is not an error, so a partially-failed source
        deletion can be retried."""
