"""The `ImageFetcher` and `ImageBlobStore` implementations.

A capability-named directory, the call `adapters/search/` and
`adapters/embedding/` already made: PRD 01 gives a directory the upstream's
name "when a port's implementation talks to one nameable external service", and
neither of these two does. `ProviderCdnImageFetcher` talks to whatever
`USHER_IMAGE_CDN_BASE_URL` names — a setting, in the shape `adapters/llm/`
settled — and `DiskImageBlobStore` talks to a filesystem, which is not an
upstream at all.

**Nothing in here logs.** `tests/unit/test_adapters_images.py` asserts it
structurally, and the reason is `adapters/tmdb/client.py`'s: a URL is a span
attribute the moment `HTTPXClientInstrumentor` sees it, so the discipline that
keeps a credential out of telemetry is that no message in this package carries
a URL — and the cheapest way to keep a log line from carrying one is to have no
log lines.
"""

from usher.adapters.images.disk import DiskImageBlobStore
from usher.adapters.images.provider import ProviderCdnImageFetcher

__all__ = ["DiskImageBlobStore", "ProviderCdnImageFetcher"]
