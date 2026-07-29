"""Shared error taxonomy for all ports.

Adapters translate whatever their upstream throws (httpx exceptions, Emby's
own error shapes, TMDb's rate-limit responses, ...) into one of these before
it crosses the port boundary. A service that only knows `usher.ports` can
then branch on failure kind without importing `httpx` or any other
adapter-specific library — importing one would break "adapters are driven,
not driving" (PRD 01).
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
