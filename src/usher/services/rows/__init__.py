"""The ten row providers, and the registry that is the composition point."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from usher.ports.rows import RowProvider
from usher.services.rows.base import BaseRow, Chapter, Progress
from usher.services.rows.because_you_watched import (
    BECAUSE_YOU_WATCHED_SCORE_CEILING,
    BecauseYouWatchedProvider,
)
from usher.services.rows.continue_watching import (
    CONTINUE_WATCHING_SCORE,
    ContinueWatchingProvider,
)
from usher.services.rows.curated import CURATED_SCORE, CuratedProvider
from usher.services.rows.franchise import FRANCHISE_SCORE_CEILING, FranchiseProvider
from usher.services.rows.genre_affinity import (
    GENRE_AFFINITY_SCORE_CEILING,
    GenreAffinityProvider,
)
from usher.services.rows.next_up import NEXT_UP_SCORE, NextUpProvider
from usher.services.rows.people import PEOPLE_SCORE_CEILING, PeopleProvider
from usher.services.rows.recently_added import (
    RECENTLY_ADDED_SCORE_CEILING,
    RecentlyAddedProvider,
)
from usher.services.rows.rediscover import REDISCOVER_SCORE, RediscoverProvider
from usher.services.rows.seasonal import SEASONAL_SCORE, SeasonalProvider

# The ceiling each provider can return, keyed by class name.
BASE_SCORES: Mapping[str, float] = {
    ContinueWatchingProvider.__name__: CONTINUE_WATCHING_SCORE,
    NextUpProvider.__name__: NEXT_UP_SCORE,
    CuratedProvider.__name__: CURATED_SCORE,
    RecentlyAddedProvider.__name__: RECENTLY_ADDED_SCORE_CEILING,
    RediscoverProvider.__name__: REDISCOVER_SCORE,
    BecauseYouWatchedProvider.__name__: BECAUSE_YOU_WATCHED_SCORE_CEILING,
    FranchiseProvider.__name__: FRANCHISE_SCORE_CEILING,
    GenreAffinityProvider.__name__: GENRE_AFFINITY_SCORE_CEILING,
    SeasonalProvider.__name__: SEASONAL_SCORE,
    PeopleProvider.__name__: PEOPLE_SCORE_CEILING,
}

# The rows a watch state can move, named by the providers that own them rather
# than by two string literals in `services/push.py`.
WATCH_STATE_ROWS: tuple[str, ...] = (
    ContinueWatchingProvider().slug_prefix,
    NextUpProvider().slug_prefix,
)

__all__ = [
    "BASE_SCORES",
    "BECAUSE_YOU_WATCHED_SCORE_CEILING",
    "CONTINUE_WATCHING_SCORE",
    "CURATED_SCORE",
    "FRANCHISE_SCORE_CEILING",
    "GENRE_AFFINITY_SCORE_CEILING",
    "NEXT_UP_SCORE",
    "PEOPLE_SCORE_CEILING",
    "RECENTLY_ADDED_SCORE_CEILING",
    "REDISCOVER_SCORE",
    "ROW_PROVIDERS",
    "SEASONAL_SCORE",
    "WATCH_STATE_ROWS",
    "BaseRow",
    "BecauseYouWatchedProvider",
    "Chapter",
    "ContinueWatchingProvider",
    "CuratedProvider",
    "FranchiseProvider",
    "GenreAffinityProvider",
    "NextUpProvider",
    "PeopleProvider",
    "Progress",
    "RecentlyAddedProvider",
    "RediscoverProvider",
    "RowProviderSetting",
    "SeasonalProvider",
    "enabled_row_providers",
    "row_provider_settings",
    "row_providers",
]


def row_providers(*, semantic: bool = False) -> tuple[RowProvider, ...]:
    """The registry, and the composition point."""
    return (
        ContinueWatchingProvider(),
        NextUpProvider(),
        RecentlyAddedProvider(),
        RediscoverProvider(),
        BecauseYouWatchedProvider(semantic=semantic),
        FranchiseProvider(),
        GenreAffinityProvider(),
        SeasonalProvider(),
        PeopleProvider(),
        # It takes no argument from the deployment: whether an LLM is configured
        # is not a fact this provider may see. With `USHER_LLM_ENABLED=false` there
        # is no generation and therefore no curated shelf, which is the same answer
        # a household gets on the day before its first one runs.
        CuratedProvider(),
    )


# The default wiring, derived from the function above rather than restated:
# `semantic=False` is the shipped default (no embedding extra), and it is also
# the *safe* default, because the sentence it selects claims less.
ROW_PROVIDERS: tuple[RowProvider, ...] = row_providers()


@dataclass(frozen=True, slots=True)
class RowProviderSetting:
    """One registered provider and whether it composes.

    Carries the provider rather than only its slug, because the two consumers
    want different halves of the same join and a second traversal to recover the
    object is the pairing failure `services/home.py::_Candidate` records.
    `GET /admin/rows/providers` renders `slug` and `enabled`; both composition
    roots keep `provider`.
    """

    provider: RowProvider
    enabled: bool

    @property
    def slug(self) -> str:
        """`RowProvider.slug_prefix`, the operator-facing identifier.

        What the overrides are keyed on, and what `usher home`'s leftmost column
        and `usher.row.build.duration`'s `provider` label already carry. Never
        the class name: a rename must not silently re-enable a provider somebody
        turned off.
        """
        return self.provider.slug_prefix


def row_provider_settings(
    overrides: Mapping[str, bool], providers: Sequence[RowProvider] = ROW_PROVIDERS
) -> tuple[RowProviderSetting, ...]:
    """The registry, left-joined onto the stored overrides."""
    return tuple(
        RowProviderSetting(provider=provider, enabled=overrides.get(provider.slug_prefix, True))
        for provider in providers
    )


def enabled_row_providers(settings: Sequence[RowProviderSetting]) -> tuple[RowProvider, ...]:
    """The composable half of a join, in registry order.

    This is filtering, not enumeration. *"A list a composition root builds by
    hand is a list the tenth provider is forgotten from"* is an argument against
    a root *naming* providers; a root that removes the ones a stored row disables
    names none of them, and the day an eleventh is registered it composes with no
    edit here or at any call site.

    Takes the joined settings rather than the overrides, so it cannot be a second
    place the absence default is spelled -- and so `usher home`, which needs
    *both* halves, reads the table once instead of twice.
    """
    return tuple(one.provider for one in settings if one.enabled)
