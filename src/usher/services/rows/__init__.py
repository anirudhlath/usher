"""The ten row providers, and the registry that is the composition point.

**A provider that is not registered is dead code**, so the registry is here
rather than in `HomeService`: a provider enabled by *registration in code* is
boundary call 9, and a list a composition root assembles by hand is a list a
new provider is forgotten from.

**`BASE_SCORES` lives here and is derived rather than restated.** Group A left
it homeless -- Task 2 settles the scores as module constants and creates no
module -- and the amendment puts it beside the registry. It imports each
provider's own constant rather than repeating the number, so the table cannot
drift from the providers it describes, and Group I asserts the observed range
across it: **ten incomparable scales make the composer's sort meaningless
while looking exactly like a sort.** That invariant is what M8's
`CURATED_SCORE` was chosen against, and it did not have to move to admit it.

**A score is not the pin.** `ContinueWatchingProvider` is PRD 06's *"1 row,
always ranked first"* and that guarantee is `ScoredRow.pinned`, a flag Group A
settled — not the fact that its score happens to be the largest here. The two
orderings agree today; only one of them is a promise.
"""

from collections.abc import Mapping

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

# The **ceiling** each provider can return, keyed by class name. A ceiling
# rather than a fixed value because `RecentlyAddedProvider` is the one provider
# whose score is a function of time -- "new" is the one relevance claim that
# genuinely decays -- so its entry is what it scores on an import that landed
# this morning.
#
# Read as a ladder it is also the argument for `CURATED_SCORE`, which is the
# first entry here chosen against the whole table rather than against one
# sibling: 1.0 and 0.90 are the two rows about *intent*, everything at 0.80 and
# below is a discovery claim from a single signal, and 0.85 is the gap between
# them.
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

# **The rows a watch state can move**, named by the providers that own them
# rather than by two string literals in `services/push.py`. A watch state
# changes where you are in something (`continue-watching`) and which episode is
# next (`next-up`); everything else it touches -- a seed's neighbours, a genre
# lift -- reaches the screen through the composed screen's own 30 s expiry,
# which `RowCache.invalidate` drops alongside these.
#
# Read off the constructed providers rather than restated, for the reason
# `BASE_SCORES` imports each provider's own constant: a table that repeats a
# value is a table that can drift from it, and a slug this list spelled wrongly
# would invalidate nothing, silently, forever.
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
    "SeasonalProvider",
    "row_providers",
]


def row_providers(*, semantic: bool = False) -> tuple[RowProvider, ...]:
    """**The registry, and it is the composition point.**

    A provider that is not registered is dead code -- and dead code that looks
    exactly like a provider with nothing to say, which is the one failure this
    milestone cannot see from the outside. **It holds ten, which is PRD 06's
    table whole**, and `test_rows_invariants.py` asserts that by name rather
    than by count, because a count passes against a registry holding one
    provider twice.

    **This paragraph named eight and a ninth still to come, and both numbers
    were false the moment `PeopleProvider` landed in the tuple below.** It is
    recorded rather than quietly corrected, because that is the shape this
    docstring was carrying: a *restated* fact, true when written, in a file
    nobody re-reads when they edit the tuple three lines down. The M8 amendment
    that added the tenth is the same trap one milestone later, and the defence
    is the same one this module already relies on for `BASE_SCORES` -- state a
    fact where it is derived, and where it cannot be derived, let the case that
    enumerates the registry be the copy that fails.

    **A function rather than a bare tuple, and exactly one deployment fact
    reaches it.** `BecauseYouWatchedProvider` says a different sentence when
    the neighbour blend had no cosine term to include, and that is a property
    of the *deployment* (is an embedder installed) rather than of a request or
    of a household -- so it cannot ride on `RowContext`, and a provider
    constructed once at import cannot read it either. The alternative was a
    second list in `composition.build_pipeline`, which is precisely the "a
    list a composition root assembles by hand is a list a new provider is
    forgotten from" failure this module exists to refuse. There is one list,
    here, and the composition root passes an argument to it.

    A tuple rather than a list: it is read by every request and written by
    none, and a composition root that could append to it would be a second
    registry.
    """
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
        # **M8's, and it takes no argument from the deployment.** Whether an
        # LLM is configured is not a fact this provider may see: with
        # `USHER_LLM_ENABLED=false` there is no generation and therefore no
        # curated shelf, which is the same answer a household gets on the day
        # before its first one runs. Two states, one observable outcome, no
        # branch -- fewer rows, not worse rows.
        CuratedProvider(),
    )


# The default wiring, derived from the function above rather than restated:
# `semantic=False` is the shipped default (no embedding extra, ADR-0022), and
# it is also the *safe* default, because the sentence it selects claims less.
ROW_PROVIDERS: tuple[RowProvider, ...] = row_providers()
