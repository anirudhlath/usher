from usher.domain.enums import WatchStateOrigin
from usher.domain.ids import new_id
from usher.domain.watch import User, WatchState


def test_user_has_identity_and_name() -> None:
    user = User(name="default")
    assert user.name == "default"
    assert user.id.version == 7


def test_watch_state_attaches_to_a_title() -> None:
    state = WatchState(
        user_id=new_id(),
        title_id=new_id(),
        position_seconds=1840,
        updated_by=WatchStateOrigin.SOURCE,
    )
    assert state.played is False
    assert state.play_count == 0
