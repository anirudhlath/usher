"""Season and Episode.

Every constraint here is asserted with a value that violates it, not with
one that satisfies it -- a test that only constructs a valid model passes
against a model with no validators at all.
"""

import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from usher.domain.episode import Episode, Season
from usher.domain.ids import new_id

TITLE_ID = new_id()
SEASON_ID = new_id()


def test_a_season_is_identified_by_ushers_own_id() -> None:
    season = Season(title_id=TITLE_ID, season_number=2)
    assert isinstance(season.id, uuid.UUID)
    assert season.id.version == 7
    assert season.tmdb_id is None


def test_a_season_number_may_be_zero_for_specials() -> None:
    """TMDb numbers a series' specials as season 0, and Emby emits
    `ParentIndexNumber: 0` for them. Rejecting zero would drop every
    special in the library."""
    assert Season(title_id=TITLE_ID, season_number=0).season_number == 0


def test_a_negative_season_number_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Season(title_id=TITLE_ID, season_number=-1)


def test_a_negative_episode_count_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Season(title_id=TITLE_ID, season_number=1, episode_count=-1)


def test_an_episode_carries_its_place_in_the_series() -> None:
    episode = Episode(
        title_id=TITLE_ID,
        season_id=SEASON_ID,
        season_number=2,
        episode_number=5,
        name="Kissed by Fire",
        air_date=date(2013, 4, 28),
        runtime_minutes=57,
        tmdb_id=63_070,
        imdb_id="tt99000110",
    )
    assert (episode.season_number, episode.episode_number) == (2, 5)
    assert episode.absolute_number is None


def test_a_negative_episode_number_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Episode(title_id=TITLE_ID, season_id=SEASON_ID, season_number=1, episode_number=-1)


def test_a_negative_season_number_is_rejected_on_an_episode_too() -> None:
    """`Season.season_number` and `Episode.season_number` are two separate
    bounds on two separate models, and only the first had a case. Deleting
    this one's `ge=0` left the suite green -- verified by running exactly
    that mutation before this test existed."""
    with pytest.raises(ValidationError):
        Episode(title_id=TITLE_ID, season_id=SEASON_ID, season_number=-1, episode_number=1)


def test_a_negative_absolute_number_is_rejected() -> None:
    """Not in the plan's original case list, which nonetheless told the
    implementer to mutation-test `absolute_number`'s `ge=0`. An unenforced
    constraint is worse than no constraint, because it reads as enforced."""
    with pytest.raises(ValidationError):
        Episode(
            title_id=TITLE_ID,
            season_id=SEASON_ID,
            season_number=1,
            episode_number=1,
            absolute_number=-1,
        )


def test_a_negative_runtime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Episode(
            title_id=TITLE_ID,
            season_id=SEASON_ID,
            season_number=1,
            episode_number=1,
            runtime_minutes=-1,
        )


def test_a_malformed_imdb_id_is_rejected() -> None:
    """Same `^tt\\d{7,8}$` pattern `Title.imdb_id` carries. An episode's
    IMDb id is a matcher input exactly as a title's is, and a source that
    emitted `"2178782"` without the prefix would otherwise be stored and
    never match anything."""
    with pytest.raises(ValidationError):
        Episode(
            title_id=TITLE_ID,
            season_id=SEASON_ID,
            season_number=1,
            episode_number=1,
            imdb_id="2178782",
        )


def test_both_models_are_frozen_and_evolve() -> None:
    season = Season(title_id=TITLE_ID, season_number=1)
    with pytest.raises(ValidationError):
        season.season_number = 2  # type: ignore[misc]
    assert season.evolve(season_number=2).season_number == 2

    episode = Episode(title_id=TITLE_ID, season_id=SEASON_ID, season_number=1, episode_number=1)
    with pytest.raises(ValidationError):
        episode.episode_number = 2  # type: ignore[misc]
    assert episode.evolve(episode_number=2).episode_number == 2


def test_evolve_revalidates_where_model_copy_would_not() -> None:
    """`.evolve()` is the only sanctioned write path precisely because it
    re-runs validation; `model_copy(update=...)` would hand back an
    `Episode` with a negative episode number that serializes without
    complaint (CLAUDE.md, `DomainModel`)."""
    episode = Episode(title_id=TITLE_ID, season_id=SEASON_ID, season_number=1, episode_number=1)
    with pytest.raises(ValidationError):
        episode.evolve(episode_number=-1)


def test_both_models_reject_an_unknown_field() -> None:
    """`extra="forbid"` from `DomainModel`. A typo'd keyword in the TMDb
    mapper (`episode_num=`) must fail at construction, not be dropped."""
    with pytest.raises(ValidationError):
        Season(
            title_id=TITLE_ID,
            season_number=1,
            episode_num=3,  # type: ignore[call-arg]  # deliberate typo of episode_count
        )
    with pytest.raises(ValidationError):
        Episode(
            title_id=TITLE_ID,
            season_id=SEASON_ID,
            season_number=1,
            episode_number=1,
            episode_num=3,  # type: ignore[call-arg]  # deliberate typo of episode_number
        )
