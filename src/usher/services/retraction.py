"""Asking a source which ids it no longer serves, counted for both retraction paths."""

from collections.abc import Collection
from typing import Literal

from opentelemetry import metrics

from usher.ports.source import Confirmation, SourceAdapter

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
) -> Confirmation:
    """The source's answer about `external_ids`, kept to those ids, each counted by outcome.

    Only an asked id is returned or counted, whatever the adapter answers, and `served`
    keeps the adapter's order. An asked id that is not gone counts as served, described
    or not. An empty question asks nothing and counts nothing, and an outcome with no id
    adds nothing. `source` is the source's name, the label `usher.sync.run.duration`
    carries too.
    """
    asked = frozenset(external_ids)
    if not asked:
        return Confirmation()
    answer = await adapter.confirm_gone(asked)
    gone = asked & answer.gone
    served = tuple(item for item in answer.served if item.external_id in asked)
    for outcome, count in (("gone", len(gone)), ("served", len(asked) - len(gone))):
        if count:
            _retraction_checks.add(count, {"source": source, "path": path, "outcome": outcome})
    return Confirmation(gone=gone, served=served)
