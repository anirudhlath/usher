from datetime import datetime

import pytest
from pydantic import ValidationError

from usher.domain.enums import WatchStateOrigin
from usher.domain.ids import new_id
from usher.domain.watch import User, WatchState


def _watch_state(**overrides: object) -> WatchState:
    fields: dict[str, object] = {
        "user_id": new_id(),
        "title_id": new_id(),
        "origin": WatchStateOrigin.API,
        **overrides,
    }
    return WatchState.model_validate(fields)


def test_user_has_identity_and_name() -> None:
    user = User(name="default")
    assert user.name == "default"
    assert user.id.version == 7


def test_watch_state_attaches_to_a_title() -> None:
    state = WatchState(
        user_id=new_id(),
        title_id=new_id(),
        position_seconds=1840,
        origin=WatchStateOrigin.SOURCE,
    )
    assert state.played is False
    assert state.play_count == 0
    # The load-bearing invariant: watch state attaches to the canonical
    # Title, never to a source-specific record. Asserted against
    # model_fields, not just "the constructor doesn't require one", so a
    # future refactor that reintroduces source_id/media_item_id onto
    # WatchState fails this test instead of silently reintroducing the
    # coupling watch state is designed to survive.
    assert "source_id" not in WatchState.model_fields
    assert "media_item_id" not in WatchState.model_fields


# --- frozen-ness -----------------------------------------------------------


def test_user_is_immutable() -> None:
    user = User(name="default")
    with pytest.raises(ValidationError):
        user.name = "Other"  # type: ignore[misc]  # verifying the runtime rejection frozen=True enforces


def test_watch_state_is_immutable() -> None:
    state = _watch_state()
    with pytest.raises(ValidationError):
        state.played = True  # type: ignore[misc]  # verifying the runtime rejection frozen=True enforces


# --- hashability (contrast with the deliberately-unhashable Title) ---------


def test_user_and_watch_state_are_hashable() -> None:
    """Neither carries a dict or list field, so — unlike Title — both hash
    cleanly. See DomainModel's docstring for the asymmetry."""
    hash(User(name="default"))
    hash(_watch_state())


# --- extra="forbid" ---------------------------------------------------------


def test_watch_state_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _watch_state(oops="typo")


def test_user_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        User.model_validate({"name": "default", "oops": "typo"})


# --- AwareDatetime -----------------------------------------------------------


def test_user_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError):
        User.model_validate({"name": "default", "created_at": datetime(2026, 1, 1)})


def test_watch_state_rejects_naive_last_played_at() -> None:
    with pytest.raises(ValidationError):
        _watch_state(last_played_at=datetime(2026, 1, 1))


def test_watch_state_updated_at_defaults_to_aware_now() -> None:
    assert _watch_state().updated_at.tzinfo is not None


# --- value constraints -------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("position_seconds", -5),
        ("play_count", -3),
        ("runtime_seconds", -1),
    ],
)
def test_watch_state_negative_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _watch_state(**{field: value})


def test_user_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        User(name="")


# --- exactly one of title_id / episode_id ------------------------------------


def test_watch_state_rejects_neither_title_nor_episode() -> None:
    with pytest.raises(ValidationError):
        WatchState.model_validate({"user_id": new_id(), "origin": WatchStateOrigin.API})


def test_watch_state_rejects_both_title_and_episode() -> None:
    with pytest.raises(ValidationError):
        WatchState.model_validate(
            {
                "user_id": new_id(),
                "title_id": new_id(),
                "episode_id": new_id(),
                "origin": WatchStateOrigin.API,
            }
        )


def test_watch_state_accepts_exactly_episode() -> None:
    state = WatchState.model_validate(
        {"user_id": new_id(), "episode_id": new_id(), "origin": WatchStateOrigin.API}
    )
    assert state.episode_id is not None
    assert state.title_id is None


# --- origin: renamed from updated_by, no default ------------------------


def test_origin_has_no_default() -> None:
    """Defaulting to API would silently mislabel source-pushed state as
    user-originated if a sync path forgot to set it. Provenance must be
    supplied explicitly."""
    with pytest.raises(ValidationError):
        WatchState.model_validate({"user_id": new_id(), "title_id": new_id()})


def test_watch_state_rejects_unknown_origin() -> None:
    with pytest.raises(ValidationError):
        WatchState.model_validate({"user_id": new_id(), "title_id": new_id(), "origin": "webhook"})


# --- serialization round-trip (the wire contract from M4 onward) -----------


def test_user_serialization_round_trips() -> None:
    user = User(name="default", is_default=True)
    restored = User.model_validate_json(user.model_dump_json())
    assert restored == user


def test_watch_state_serialization_round_trips() -> None:
    state = _watch_state(position_seconds=1840)
    restored = WatchState.model_validate_json(state.model_dump_json())
    assert restored == state
