"""A send's stages and record, and what a player's reading says about each stage."""

import uuid
from datetime import UTC, datetime

import pytest

from usher.domain.house import HouseState
from usher.domain.recipes import RECIPES, Recipe
from usher.domain.sends import (
    FINISHED,
    MAY_NOT_BE_INSTALLED,
    NOT_ANSWERING,
    STAYED_ASLEEP,
    USHER_FAILED,
    Send,
    SendStage,
    StageMark,
    busy_with,
    didnt_take,
    opened,
    showing,
    started,
)

INFUSE_RECIPE: Recipe = next(one for one in RECIPES if one.name == "Infuse")
INFUSE = INFUSE_RECIPE.app_id
NETFLIX = "com.netflix.Netflix"
T0 = datetime(2026, 11, 1, 20, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 1, 20, 0, 5, tzinfo=UTC)


def _state(
    *,
    state: str = "idle",
    app_id: str | None = None,
    app_name: str | None = None,
    media_title: str | None = None,
    media_duration_seconds: float | None = None,
) -> HouseState:
    return HouseState(
        entity_id="media_player.example_tv",
        state=state,
        app_id=app_id,
        app_name=app_name,
        media_title=media_title,
        media_duration_seconds=media_duration_seconds,
    )


def _send(stage: SendStage = SendStage.WAKING, detail: str | None = None) -> Send:
    return Send(
        id=uuid.uuid4(),
        title_id=uuid.uuid4(),
        episode_id=None,
        way_id=uuid.uuid4(),
        area_id="living_room",
        screen_name="Living Room",
        entity_id="media_player.example_tv",
        device_name="Living Room Apple TV",
        recipe_key=INFUSE_RECIPE.key,
        marks=(StageMark(stage=stage, at=T0, detail=detail),),
    )


def test_the_infuse_recipe_names_an_app() -> None:
    assert INFUSE == "com.firecore.infuse"


def test_a_send_is_finished_when_it_played_didnt_take_or_was_superseded() -> None:
    assert {SendStage.PLAYING, SendStage.DIDNT_TAKE, SendStage.SUPERSEDED} == FINISHED
    for stage in SendStage:
        assert _send(stage).finished is (stage in FINISHED)


def test_a_send_reads_its_stage_and_detail_from_the_last_mark() -> None:
    first = _send(SendStage.WAKING, "first")
    moved = first.moved(SendStage.OPENING, T1, detail="second")
    assert (first.stage, first.detail) == (SendStage.WAKING, "first")
    assert (moved.stage, moved.detail) == (SendStage.OPENING, "second")
    assert [one.stage for one in moved.marks] == [SendStage.WAKING, SendStage.OPENING]
    assert moved.marks[-1] == StageMark(SendStage.OPENING, T1, "second")


def test_moving_a_send_leaves_the_original_untouched() -> None:
    first = _send()
    first.moved(SendStage.OPENING, T1)
    assert len(first.marks) == 1


def test_a_send_that_did_not_take_records_the_stage_it_failed_at() -> None:
    sent = _send(SendStage.OPENING).moved(
        SendStage.DIDNT_TAKE, T1, detail="x", failed_stage=SendStage.OPENING
    )
    assert sent.stage is SendStage.DIDNT_TAKE
    assert sent.failed_stage is SendStage.OPENING
    assert sent.finished


@pytest.mark.parametrize("stage", sorted(FINISHED))
def test_a_finished_send_does_not_move(stage: SendStage) -> None:
    sent = _send(stage)
    assert sent.finished
    with pytest.raises(ValueError):
        sent.moved(SendStage.OPENING, T1)


@pytest.mark.parametrize("stage", [s for s in SendStage if s is not SendStage.DIDNT_TAKE])
def test_a_failed_stage_belongs_only_to_didnt_take(stage: SendStage) -> None:
    with pytest.raises(ValueError):
        _send().moved(stage, T1, failed_stage=SendStage.WAKING)


def test_a_paused_player_is_not_busy_and_a_playing_one_says_what_with() -> None:
    assert busy_with(_state(state="paused", app_name="Netflix")) is None
    assert busy_with(None) is None
    assert (
        busy_with(_state(state="playing", app_name="Netflix", media_title="Example Episode"))
        == "NETFLIX · EXAMPLE EPISODE"
    )
    assert busy_with(_state(state="playing")) == "SOMETHING ELSE"


def test_a_playing_player_says_only_what_it_has() -> None:
    assert busy_with(_state(state="playing", app_name="Netflix")) == "NETFLIX"
    assert busy_with(_state(state="playing", media_title="Example Episode")) == "EXAMPLE EPISODE"


def test_a_reading_opens_the_app_when_it_changed_and_shows_that_app() -> None:
    at = _state(state="idle")
    assert opened(_state(app_id=INFUSE), at_handover=at, app_id=INFUSE)


def test_no_reading_is_not_the_app_opening() -> None:
    assert not opened(None, at_handover=_state(), app_id=INFUSE)


def test_the_reading_at_the_handover_is_not_the_app_opening() -> None:
    at = _state(app_id=INFUSE)
    assert not opened(at, at_handover=at, app_id=INFUSE)


def test_another_app_is_not_the_app_opening() -> None:
    assert not opened(_state(app_id=NETFLIX), at_handover=_state(), app_id=INFUSE)


def test_an_app_that_opened_with_no_reading_at_the_handover_counts() -> None:
    assert opened(_state(app_id=INFUSE), at_handover=None, app_id=INFUSE)


def test_a_recipe_with_no_app_asks_only_that_the_reading_differs() -> None:
    at = _state(state="idle")
    assert opened(_state(state="playing", app_id=NETFLIX), at_handover=at, app_id=None)
    assert not opened(at, at_handover=at, app_id=None)
    assert not opened(None, at_handover=at, app_id=None)


def test_started_needs_the_app_open_and_playing() -> None:
    at = _state(state="idle")
    assert started(
        _state(state="playing", app_id=INFUSE), at_handover=at, app_id=INFUSE, runtime_seconds=None
    )
    assert not started(
        _state(state="paused", app_id=INFUSE), at_handover=at, app_id=INFUSE, runtime_seconds=None
    )
    assert not started(
        _state(state="playing", app_id=NETFLIX), at_handover=at, app_id=INFUSE, runtime_seconds=None
    )
    assert not started(None, at_handover=at, app_id=INFUSE, runtime_seconds=None)


def test_a_duration_two_percent_under_the_runtime_agrees_and_one_second_more_does_not() -> None:
    at = _state(state="playing", app_id=INFUSE, media_duration_seconds=1_000.0)

    def now(d: float) -> HouseState:
        return _state(state="playing", app_id=INFUSE, media_duration_seconds=d)

    assert started(now(7_056.0), at_handover=at, app_id=INFUSE, runtime_seconds=7_200)
    assert not started(now(7_055.0), at_handover=at, app_id=INFUSE, runtime_seconds=7_200)
    assert started(now(7_344.0), at_handover=at, app_id=INFUSE, runtime_seconds=7_200)
    assert not started(now(7_345.0), at_handover=at, app_id=INFUSE, runtime_seconds=7_200)


def test_an_unknown_duration_agrees_with_any_runtime() -> None:
    at = _state(state="idle")
    now = _state(state="playing", app_id=INFUSE, media_duration_seconds=None)
    assert started(now, at_handover=at, app_id=INFUSE, runtime_seconds=7_200)


def test_an_unknown_runtime_agrees_with_any_duration() -> None:
    at = _state(state="idle")
    now = _state(state="playing", app_id=INFUSE, media_duration_seconds=123.0)
    assert started(now, at_handover=at, app_id=INFUSE, runtime_seconds=None)


def test_the_reading_at_the_handover_is_never_the_send_playing() -> None:
    at = _state(state="playing", app_id=INFUSE, media_duration_seconds=7_200.0)
    assert not started(at, at_handover=at, app_id=INFUSE, runtime_seconds=7_200)


def test_what_a_player_shows_at_a_deadline() -> None:
    def show(state: HouseState | None) -> str:
        return showing(state, device_name="Living Room Apple TV", recipe=INFUSE_RECIPE)

    assert show(None) == "LIVING ROOM APPLE TV TURNED OFF"
    assert show(_state(state="standby")) == "LIVING ROOM APPLE TV TURNED OFF"
    assert (
        show(_state(state="idle", app_id=NETFLIX, app_name="Netflix")) == "SOMEONE OPENED NETFLIX"
    )
    assert (
        show(_state(state="paused", app_id=INFUSE, app_name="Infuse"))
        == "INFUSE DID NOT START PLAYING"
    )


@pytest.mark.parametrize("off", ["off", "standby", "unavailable"])
def test_a_player_that_is_off_shows_turned_off_whatever_app_it_names(off: str) -> None:
    reading = _state(state=off, app_id=NETFLIX, app_name="Netflix")
    assert (
        showing(reading, device_name="Living Room Apple TV", recipe=INFUSE_RECIPE)
        == "LIVING ROOM APPLE TV TURNED OFF"
    )


def test_another_app_with_no_name_falls_through_to_did_not_start() -> None:
    reading = _state(state="idle", app_id=NETFLIX)
    assert (
        showing(reading, device_name="Living Room Apple TV", recipe=INFUSE_RECIPE)
        == "INFUSE DID NOT START PLAYING"
    )


def test_a_didnt_take_line_names_the_stage_and_the_cause() -> None:
    assert didnt_take(SendStage.WAKING, "X") == "DIDN'T TAKE · WAKING · X"
    assert didnt_take(SendStage.STARTING, "Y") == "DIDN'T TAKE · STARTING · Y"


def test_the_printed_templates_are_uppercase_and_fill_in() -> None:
    assert (
        STAYED_ASLEEP.format(device="LIVING ROOM APPLE TV") == "LIVING ROOM APPLE TV STAYED ASLEEP"
    )
    assert MAY_NOT_BE_INSTALLED.format(app="INFUSE") == "INFUSE MAY NOT BE INSTALLED"
    assert NOT_ANSWERING == "HOME ASSISTANT IS NOT ANSWERING"
    assert USHER_FAILED == "USHER FAILED"
