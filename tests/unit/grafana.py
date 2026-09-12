"""Reading a committed Grafana dashboard, for the cases that grade one."""

import json
import pathlib
from typing import Any


def panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Every panel, including the children a collapsed row nests under itself.

    Grafana keeps an *expanded* row's panels as siblings and a *collapsed*
    row's under `panel["panels"]`, so a scan of the top level alone stops
    seeing a dashboard's panels the moment someone collapses a row and saves.
    """
    found: list[dict[str, Any]] = []
    queue: list[Any] = list(dashboard.get("panels") or [])
    while queue:
        panel = queue.pop(0)
        if not isinstance(panel, dict):
            continue
        found.append(panel)
        queue.extend(panel.get("panels") or [])
    return found


def panel_titles(path: pathlib.Path) -> set[str]:
    """Every panel title in a committed dashboard, collapsed rows included.

    Takes the path rather than closing over one board's: the cases that use
    this grade several, and a copy per board is a second thing to fix when a
    nested row stops being read.
    """
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    return {str(panel["title"]) for panel in panels(document) if panel.get("title")}
