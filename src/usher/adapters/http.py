"""HTTP helpers shared by every adapter that talks to an upstream over
httpx.

PRD 01 anticipates this module: "a shared `BaseHTTPAdapter` carries the
httpx client lifecycle, retry/backoff, and rate-limit handling that the
Emby and TMDb adapters both need, instead of each reimplementing it." This
is the first piece of it -- the client lifecycle stays per-adapter for now,
because `CachedDatasetFile` is handed a shared client it does not own while
`EmbyAdapter` owns one per source, and forcing those two into one base
class would be shape for its own sake.
"""

import datetime as dt
import email.utils


def retry_after_seconds(value: str | None) -> float | None:
    """Parse a `Retry-After` header value into seconds from now, or `None`
    if there was no header or it couldn't be parsed at all.

    RFC 9110 permits `Retry-After` to be *either* an integer number of
    seconds *or* an HTTP-date -- `float(value)` alone raises `ValueError`
    on the latter (`could not convert string to float: 'Wed, 21 Oct 2026
    07:28:00 GMT'`), and this is the 429 path: the one moment upstream is
    explicitly asking for backoff. A caller that only handled the numeric
    form would raise instead of backing off exactly when backing off
    matters most. Shared by every adapter's 429 handling rather than
    duplicated -- the bug this fixes existed in two places for exactly
    that reason.
    """
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        target = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.UTC)
    return max(0.0, (target - dt.datetime.now(dt.UTC)).total_seconds())
