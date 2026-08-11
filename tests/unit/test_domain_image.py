"""`usher.domain.image.Image` -- the twin `m09a` deliberately shipped without.

Two claims live here and nowhere else. The **1:1 field/column correspondence**
with `ImageRow`, which `test_db_models.py`'s version is scoped to
`TitleRow`/`Title` only, so `images` had no such check until this file; and the
**exactly-one-owner** validator, which mirrors `ck_images_exactly_one_owner` the
way `WatchState._exactly_one_of_title_or_episode` mirrors
`ck_watch_states_exactly_one_target`.
"""

import uuid
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy import Table

from usher.db.models.image import ImageRow
from usher.domain.enums import ImageKind
from usher.domain.ids import new_id
from usher.domain.image import Image


def _image(**changes: object) -> Image:
    fields: dict[str, object] = {
        "title_id": new_id(),
        "kind": ImageKind.POSTER,
        "provider": "tmdb",
        "provider_path": "/an-invented-path.jpg",
        "is_primary": True,
    }
    fields.update(changes)
    return Image.model_validate(fields)


def test_image_and_image_row_have_matching_field_sets() -> None:
    """**The standing constraint, checked rather than assumed.**
    `title.py`, `episode.py` and `people.py` all carry it and
    `tests/unit/test_db_models.py`'s assertion is scoped to `TitleRow`/`Title`,
    so `m09a`'s note that "four rows with no domain twin break nothing" was
    true precisely because nothing checked this pair.

    `images` has no derived column, so this is the plain `columns == fields`
    form rather than `titles`' `columns - DERIVED_COLUMNS == fields` -- noted
    because the difference is otherwise indistinguishable from a forgotten
    filter.

    `episode_id` and `person_id` are carried rather than dropped. Nothing in
    M9 writes either (the group's own boundary call: episode stills and person
    headshots are not built), and the honest shape for a column the model
    cannot fill yet is to model it and say so, which is `api/dto/title.py`'s
    call one layer up -- not to quietly drop it so this assertion passes.
    """
    columns = {column.name for column in cast(Table, ImageRow.__table__).columns}
    assert columns == set(Image.model_fields)


def test_an_image_with_no_owner_is_refused() -> None:
    """`ck_images_exactly_one_owner` in Python, and it has to be here as well
    as in the DDL: `replace_for_titles` assembles rows from a provider payload,
    and an ownerless one reaching Postgres is a `RepositoryConflict` that fails
    the whole derivation batch rather than the one row that is wrong."""
    with pytest.raises(ValidationError, match="exactly one"):
        _image(title_id=None)


def test_an_image_with_two_owners_is_refused() -> None:
    """`= 1`, not `>= 1` -- the half a `num_nonnulls(...) >= 1` spelling would
    let through. An image belonging to both a title and a person is not a
    poster with two homes, it is a row two readers will disagree about."""
    with pytest.raises(ValidationError, match="exactly one"):
        _image(person_id=new_id())


@pytest.mark.parametrize("owner", ["title_id", "episode_id", "person_id"])
def test_each_owner_column_on_its_own_is_a_valid_image(owner: str) -> None:
    """All three, because a validator written as `title_id is None` reads
    correctly and refuses every episode still and every person headshot."""
    image = _image(**{"title_id": None, owner: new_id()})
    assert getattr(image, owner) is not None


def test_an_image_is_frozen_and_evolves_with_revalidation() -> None:
    """`DomainModel`'s standing rule. `.evolve()` re-validates, so a change
    that breaks the owner invariant raises here rather than at the write."""
    image = _image()
    with pytest.raises(ValidationError):
        image.evolve(person_id=new_id())
    assert image.evolve(is_primary=False).is_primary is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", ""),
        ("provider_path", ""),
        ("width", 0),
        ("height", 0),
    ],
)
def test_every_check_on_the_row_is_mirrored_as_a_field_bound(field: str, value: object) -> None:
    """This schema mirrors every Pydantic bound as a CHECK and the mirror runs
    both ways: `ck_images_provider_not_empty`,
    `ck_images_provider_path_not_empty`, `ck_images_width_positive` and
    `ck_images_height_positive` each have a field bound here, so the row a
    derivation assembles is refused at construction rather than by a constraint
    violation that takes its whole batch with it."""
    with pytest.raises(ValidationError):
        _image(**{field: value})


def test_a_dimensionless_image_is_ordinary() -> None:
    """Nullable on purpose: a provider that reports no dimensions and no
    language is ordinary, and a placeholder `0` is a lie a layout engine acts
    on. `gt=0` bounds the value when there is one and says nothing when there
    is not."""
    image = _image(width=None, height=None, language=None)
    assert (image.width, image.height, image.language) == (None, None, None)


def test_an_image_mints_its_own_id() -> None:
    """Identity is Usher's own UUIDv7, and the provider has no image id to
    borrow -- which is the whole reason `(owner, provider, provider_path)` has
    to be the natural key rather than a provider integer."""
    assert isinstance(_image().id, uuid.UUID)
    assert _image().id != _image().id
