"""Shared error taxonomy for all ports."""

import gzip
import zlib
from typing import Final

#: What a gzip body that is damaged, truncated or not gzip at all raises, named
#: once because it cannot be stated as one base class: `BadGzipFile` is an
#: `OSError`, `zlib.error` is not, and a member that ends mid-stream raises a
#: bare `EOFError` out of `GzipFile.read`.
DAMAGED_GZIP: Final = (gzip.BadGzipFile, EOFError, zlib.error)


class UsherPortError(Exception):
    """Base for every error a port implementation may raise."""


class PortUnavailable(UsherPortError):
    """The upstream could not be reached, or did not respond in time.

    Distinct from "the requested thing does not exist" — see e.g.
    `SourceAdapter.get_item`, which returns `None` for that and never
    raises it as an error. A caller that sees this degrades rather than
    fails: PRD 08's "a degraded subsystem narrows functionality; it never
    fails a request that local state can answer."
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
    """`add()` was called for an id — or another unique key — that already exists."""

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
    """An upstream payload could not be parsed into the shape this port promises.

    Distinct from `PortUnavailable`: the upstream answered, and the answer
    was wrong. Retrying does not help, so a caller parks the work rather
    than backing off — PRD 08's "malformed data does not back off at all —
    it parks on the first attempt."

    `detail` carries enough to find the offending record without dumping
    it: the dataset's own row identifier and what was expected. It must
    never carry a credential or a whole payload.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.detail = detail
