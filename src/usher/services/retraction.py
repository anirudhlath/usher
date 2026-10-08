"""Asking a source which ids it no longer serves, counted for both retraction paths."""

from collections.abc import Collection
from typing import Literal

from opentelemetry import metrics

from usher.ports.source import SourceAdapter

_meter = metrics.get_meter("usher.retraction")
_retraction_checks = _meter.create_counter(
    "usher.sync.retraction.checks",
    unit="1",
    description="Ids asked of a source before retracting them, by path and outcome",
)


async def confirmed_gone(
    adapter: SourceAdapter,
    external_ids: Collection[str],
    *,
    source: str,
    path: Literal["sweep", "push"],
) -> frozenset[str]:
    """The ids among `external_ids` the source confirms gone, each counted by outcome.

    Only an asked id is returned or counted, whatever the adapter answers. An empty
    question asks nothing and counts nothing, and an outcome with no id adds nothing.
    `source` is the source's name, the label `usher.sync.run.duration` carries too.
    """
    asked = frozenset(external_ids)
    if not asked:
        return frozenset()
    gone = asked & await adapter.confirm_gone(asked)
    for outcome, count in (("gone", len(gone)), ("served", len(asked) - len(gone))):
        if count:
            _retraction_checks.add(count, {"source": source, "path": path, "outcome": outcome})
    return gone
