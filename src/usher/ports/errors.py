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

    **M8 widened what this member covers, and the paragraphs above are now
    the common case rather than the whole of it.** It also carries "the
    backing store refused this row's *values*" — which is not a uniqueness
    conflict, is not a constraint at all, and in one measured instance never
    reached the server. Two shapes, both found in M8 and both reachable from a
    **validly constructed** domain model, because in each the column is
    narrower than the field feeding it: `llm_calls.cost_usd` is
    `NUMERIC(12, 8)` against a `Decimal` bounded by `ge=0` and nothing above,
    so a large enough call is `numeric field overflow` server-side; and
    `curated_rows."position"` is `integer` against `Field(ge=0)`, so `2**31`
    is refused client-side by asyncpg's own encoder. Neither is an
    `IntegrityError`; `usher.db.repositories._errors.refuses_the_row` is what
    both implementations filter on.

    **The reuse is deliberate and this paragraph is the record of it, because
    the alternative is worse.** No existing member fits — `PortDataMalformed`
    is about an *upstream payload* this project parsed, not about a row this
    project assembled — and a new member would fork every `except
    RepositoryConflict` in `services/` to catch two things that call for the
    same response: the write cannot succeed as given, a retry will not help,
    and the caller's own state is what is wrong. What the widening costs is
    that **`constraint = None` now means two different things**: "a constraint
    fired and the implementation could not name it" and "no constraint fired
    at all". A caller branching on a *specific* constraint name is unaffected,
    since that is still exact; a caller treating `None` as "some constraint,
    unknown" would be wrong, and none does. The alternative — leaving these
    untranslated — is a raw `sqlalchemy.exc.DBAPIError` at a service, which
    ADR-0009 forbids outright.
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
