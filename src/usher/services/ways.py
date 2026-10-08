"""Ways: every way a title's or an episode's copies could play on the house's screens."""

import uuid
from dataclasses import dataclass
from typing import Final

from usher.domain.screens import Screen, project
from usher.domain.ways import Way, rank
from usher.ports.errors import UsherPortError
from usher.ports.players import Players
from usher.ports.repository import MediaItemRepository


@dataclass(frozen=True, slots=True)
class WaysAnswer:
    """The screens ranked on, in reading order, and their ways, each screen's possibly none.

    `screens_available` is false while the house cannot be read, and there are
    then no screens and no ways.
    """

    screens_available: bool
    screens: tuple[Screen, ...]
    ways: tuple[Way, ...]


_NO_SCREENS: Final = WaysAnswer(screens_available=False, screens=(), ways=())


class UnknownScreen(LookupError):
    """No screen has this area id."""


class WaysService:
    def __init__(self, players: Players | None, media_items: MediaItemRepository) -> None:
        self._players = players
        self._media_items = media_items

    async def for_title(self, title_id: uuid.UUID, *, screen: str | None = None) -> WaysAnswer:
        """Every way a title's own copies play, on one screen or on every one."""
        screens = self._screens(screen)
        if screens is None:
            return _NO_SCREENS
        copies = await self._media_items.list_for_title(title_id)
        return WaysAnswer(screens_available=True, screens=screens, ways=rank(screens, copies))

    async def for_episode(self, episode_id: uuid.UUID, *, screen: str | None = None) -> WaysAnswer:
        """The same for one episode, whose copies `list_for_title` never answers."""
        screens = self._screens(screen)
        if screens is None:
            return _NO_SCREENS
        copies = await self._media_items.list_for_episode(episode_id)
        return WaysAnswer(screens_available=True, screens=screens, ways=rank(screens, copies))

    def _screens(self, screen: str | None) -> tuple[Screen, ...] | None:
        """The screens to rank on, in reading order; `None` with no house to read.

        Read before any copy, and before a named screen is judged: with no
        house, nothing can say whether a screen exists.
        """
        if self._players is None:
            return None
        try:
            view = project(self._players.snapshot())
        except UsherPortError:
            return None
        if screen is None:
            return view.screens
        found = view.screen(screen)
        if found is None:
            raise UnknownScreen(screen)
        return (found,)
