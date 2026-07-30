"""Shared error taxonomy for all ports.

Every port implementation — an adapter talking to an upstream service, or a
repository talking to a backing store — translates whatever it catches
(httpx exceptions, Emby's own error shapes, TMDb's rate-limit responses,
`sqlalchemy.exc.IntegrityError`, ...) into one of these before it crosses
the port boundary. A service that only knows `usher.ports` can then branch
on failure kind without importing `httpx`, SQLAlchemy, or any other
adapter- or storage-specific library — importing one would break "adapters
are driven, not driving" (PRD 01) for adapters, and "db is driven, not
driving" (ADR-0009) for repositories, the same mechanism serving both
contracts.
"""


class UsherPortError(Exception):
    """Base for every error a port implementation may raise."""


class PortUnavailable(UsherPortError):
    """The upstream could not be reached, or did not respond in time.

    Distinct from "the requested thing does not exist" — see e.g.
    `SourceAdapter.get_item`, which returns `None` for that and never
    raises it as an error. A caller that sees this degrades rather than
    fails: PRD 08's "a degraded subsystem narrows functionality; it never
    fails a request local state can answer."
    """


class PortAuthFailed(UsherPortError):
    """Credentials were rejected.

    For `SourceAdapter`, PRD 03 requires the caller to treat this as the
    trigger for silent re-authentication with the stored credentials and
    the same device id — not as a terminal failure.
    """


class PortRateLimited(UsherPortError):
    """The upstream asked to be backed off.

    `retry_after` is seconds, when the upstream supplied a hint (e.g.
    TMDb's 429, an HTTP `Retry-After` header); `None` when it didn't, and
    the caller should apply its own backoff policy.
    """

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(f"rate limited, retry_after={retry_after}")
        self.retry_after = retry_after


class RepositoryConflict(UsherPortError):
    """`add()` was called for an id — or another unique key — that already
    exists. An implementation translates its backing store's own conflict
    error (e.g. Postgres's `IntegrityError` on a unique constraint) into
    this, so callers never need to import a storage-specific exception
    type to handle it. See `usher.ports.repository.TitleRepository.add`.

    `constraint` names whichever unique constraint or index actually
    fired (e.g. `"ix_titles_tmdb_id_kind"`), when the implementation can
    determine it — `None` if it can't. This is what lets a caller
    implement "try to add, fall back to update on conflict" correctly:
    without it, a service has no way to tell "this exact id already
    exists, retry as an update" apart from "some *other* row already
    holds one of this title's provider ids, look *that* row up instead" —
    both raise the identical exception otherwise, and the message alone
    is prose, not something a service should parse to recover the
    distinction.
    """

    def __init__(self, message: str, *, constraint: str | None = None) -> None:
        super().__init__(message)
        self.constraint = constraint


class RepositoryNotFound(UsherPortError):
    """`update()` targeted a row that does not exist.

    The read-side equivalent of "not found" is a plain `None` return (see
    e.g. `TitleRepository.get`) — this exists specifically for the
    write-side case, where absence must be an error rather than a value,
    because there is nothing sensible to update. See
    `usher.ports.repository.TitleRepository.update`.
    """


class PortDataMalformed(UsherPortError):
    """An upstream payload could not be parsed into the shape this port
    promises.

    Distinct from `PortUnavailable`: the upstream answered, and the answer
    was wrong. Retrying does not help, so a caller parks the work rather
    than backing off — PRD 08's "after N attempts a job is *parked* with its
    error, not retried forever and not silently dropped."

    `detail` carries enough to find the offending record without dumping
    it: the dataset's own row identifier and what was expected. It must
    never carry a credential or a whole payload.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.detail = detail
