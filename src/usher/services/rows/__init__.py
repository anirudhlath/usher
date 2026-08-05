"""The nine row providers, and the registry that is the composition point.

**A provider that is not registered is dead code**, so the registry is here
rather than in `HomeService`: a provider enabled by *registration in code* is
boundary call 9, and a list a composition root assembles by hand is a list a
new provider is forgotten from.

**`BASE_SCORES` lives here and is derived rather than restated.** Group A left
it homeless -- Task 2 settles the scores as module constants and creates no
module -- and the amendment puts it beside the registry. It imports each
provider's own constant rather than repeating the number, so the table cannot
drift from the providers it describes, and Group I asserts the observed range
across it: **nine incomparable scales make the composer's sort meaningless
while looking exactly like a sort.**

**A score is not the pin.** `ContinueWatchingProvider` is PRD 06's *"1 row,
always ranked first"* and that guarantee is `ScoredRow.pinned`, a flag Group A
settled — not the fact that its score happens to be the largest here. The two
orderings agree today; only one of them is a promise.
"""

from collections.abc import Mapping

from usher.ports.rows import RowProvider
from usher.services.rows.base import BaseRow, Chapter, Progress
from usher.services.rows.continue_watching import (
    CONTINUE_WATCHING_SCORE,
    ContinueWatchingProvider,
)
from usher.services.rows.next_up import NEXT_UP_SCORE, NextUpProvider
from usher.services.rows.recently_added import (
    RECENTLY_ADDED_SCORE_CEILING,
    RecentlyAddedProvider,
)
from usher.services.rows.rediscover import REDISCOVER_SCORE, RediscoverProvider

# The **ceiling** each provider can return, keyed by class name. A ceiling
# rather than a fixed value because `RecentlyAddedProvider` is the one provider
# whose score is a function of time -- "new" is the one relevance claim that
# genuinely decays -- so its entry is what it scores on an import that landed
# this morning.
BASE_SCORES: Mapping[str, float] = {
    ContinueWatchingProvider.__name__: CONTINUE_WATCHING_SCORE,
    NextUpProvider.__name__: NEXT_UP_SCORE,
    RecentlyAddedProvider.__name__: RECENTLY_ADDED_SCORE_CEILING,
    RediscoverProvider.__name__: REDISCOVER_SCORE,
}

__all__ = [
    "BASE_SCORES",
    "CONTINUE_WATCHING_SCORE",
    "NEXT_UP_SCORE",
    "RECENTLY_ADDED_SCORE_CEILING",
    "REDISCOVER_SCORE",
    "ROW_PROVIDERS",
    "BaseRow",
    "Chapter",
    "ContinueWatchingProvider",
    "NextUpProvider",
    "Progress",
    "RecentlyAddedProvider",
    "RediscoverProvider",
]

# **The registry, and it is the composition point.** A provider that is not
# registered is dead code -- and dead code that looks exactly like a provider
# with nothing to say, which is the one failure this milestone cannot see from
# the outside. Group I's own case asserts this holds nine once every provider
# exists; it holds four here, and the five that are missing are named rather
# than implied: `BecauseYouWatched`, `Franchise`, `GenreAffinity`, `Seasonal`
# and `People`, tasks 26-28. `CuratedProvider` is M8's whole family (boundary
# call 2) and is deliberately not among them.
#
# A tuple rather than a list: it is read by every request and written by none,
# and a composition root that could append to it would be a second registry.
ROW_PROVIDERS: tuple[RowProvider, ...] = (
    ContinueWatchingProvider(),
    NextUpProvider(),
    RecentlyAddedProvider(),
    RediscoverProvider(),
)
