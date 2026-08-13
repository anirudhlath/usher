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
across it. **The risk that makes the range worth asserting is stated once, on
`ports/rows.py`'s `ScoredRow`, and without a count** -- this sentence used to
carry one, `ports/rows.py` carried a different one, and `test_rows_invariants.
py` carried a third. That invariant is what M8's `CURATED_SCORE` was chosen
against, and it did not have to move to admit it.

**A score is not the pin.** `ContinueWatchingProvider` is PRD 06's *"1 row,
always ranked first"* and that guarantee is `ScoredRow.pinned`, a flag Group A
settled — not the fact that its score happens to be the largest here. The two
orderings agree today; only one of them is a promise.
"""

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
    "RowProviderSetting",
    "SeasonalProvider",
    "enabled_row_providers",
    "row_provider_settings",
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


@dataclass(frozen=True, slots=True)
class RowProviderSetting:
    """One registered provider and whether it composes -- the row PRD 09 item 9
    means by *"one row per registered provider"*.

    **Carries the provider rather than only its slug**, because the two
    consumers want different halves of the same join and a second traversal to
    recover the object is the pairing failure `services/home.py::_Candidate`
    records (`_publish_watch_states` reconstructed a pairing outside the loop
    that built it and went one row out of step). `GET /admin/rows/providers`
    renders `slug` and `enabled`; both composition roots keep `provider`.
    """

    provider: RowProvider
    enabled: bool

    @property
    def slug(self) -> str:
        """`RowProvider.slug_prefix` -- the operator-facing identifier, which
        is what `row_provider_settings.slug_prefix` is keyed on and what
        `usher home`'s leftmost column and `usher.row.build.duration`'s
        `provider` label already carry. Never the class name (E1's port says
        why: a rename must not silently re-enable a provider somebody turned
        off)."""
        return self.provider.slug_prefix


def row_provider_settings(
    overrides: Mapping[str, bool], providers: Sequence[RowProvider] = ROW_PROVIDERS
) -> tuple[RowProviderSetting, ...]:
    """The registry **left-joined** onto the stored overrides.

    ⚠️ **`overrides.get(slug, True)` is the whole feature, and `False` is the
    one-character version that breaks it silently.** `row_provider_settings`
    ships empty and is never seeded, so `RowProviderSettingsRepository.
    overrides()` answers *only* what an operator has touched -- absence means
    **enabled**, and a caller defaulting to `False` disables every provider
    nobody has ever touched, which on a virgin database is all ten. The port's
    docstring warns about it three times and nothing in `Mapping[str, bool]`
    prevents it, so this line is the single place the default is spelled and
    `tests/unit/test_services_home.py::test_the_overrides_mapping_is_never_
    bound_outside_the_join_that_defaults_it` is what keeps it single.

    **An override for a slug the registry does not hold renders nothing**, on
    the same argument the `PUT` route refuses to write one: dead configuration
    reads exactly like working configuration, so an operator would see a
    disabled row and believe a shelf was off. Left join, never full outer.

    **The registry is an argument with a default rather than a module read**,
    because `pipeline.row_providers` is `row_providers(semantic=...)` -- a
    different tuple of different instances -- and both `usher home` and the
    refresh lane compose over that one.
    """
    return tuple(
        RowProviderSetting(provider=provider, enabled=overrides.get(provider.slug_prefix, True))
        for provider in providers
    )


def enabled_row_providers(settings: Sequence[RowProviderSetting]) -> tuple[RowProvider, ...]:
    """The composable half of a join, in registry order.

    **This is filtering, not enumeration, and the difference is boundary call
    9's.** *"A list a composition root builds by hand is a list the tenth
    provider is forgotten from"* is an argument against a root *naming*
    providers; a root that removes the ones a stored row disables names none of
    them, and the day an eleventh is registered it composes with no edit here
    or at any call site.

    **Takes the joined settings rather than the overrides**, so it cannot be a
    second place the absence default is spelled -- and so `usher home`, which
    needs *both* halves (the providers to compose, and the disabled slugs to
    print), reads the table once instead of twice.
    """
    return tuple(one.provider for one in settings if one.enabled)
