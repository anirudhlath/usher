"""The identity layer restore resolves its references through."""

import pytest

from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.db.backup_identity import (
    RESOLUTION_ORDER,
    Unresolved,
    episode_reference,
    keys_tried,
    resolve_episodes,
    resolve_titles,
    title_reference,
)
from usher.domain.enums import TitleKind
from usher.domain.episode import Episode
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.bulk import ImdbTitle
from usher.ports.repository import EpisodeReference, TitleReference

_DUMP = (
    ImdbTitle(
        imdb_id="tt99000011",
        kind=TitleKind.MOVIE,
        name="A Prison Drama",
        original_name=None,
        year=1994,
        end_year=None,
        runtime_minutes=142,
    ),
    ImdbTitle(
        imdb_id="tt99000012",
        kind=TitleKind.MOVIE,
        name="A Crime Saga",
        original_name=None,
        year=1972,
        end_year=None,
        runtime_minutes=175,
    ),
)


def _title(**changes: object) -> Title:
    return Title.model_validate(
        {"kind": TitleKind.MOVIE, "name": "A Film", "sort_name": "A Film", **changes}
    )


async def test_a_reference_whose_natural_key_finds_nothing_is_refused_rather_than_written_null() -> (  # noqa: E501
    None
):
    """An unresolved reference is a named refusal, never a `None`.

    A `None` would be mistaken for "this column is nullable".
    `watch_states.title_id` is `ON DELETE RESTRICT`, so a `None` written there
    is an insert Postgres refuses with a foreign-key error -- an operator
    reading a driver exception instead of "this watch state's title is not in
    the target". `search_queries.clicked_title_id` is `SET NULL`, which is why
    the two tables cannot share one answer: `UNRESOLVED_RULE` decides per table
    and this resolver decides nothing. Two premises come first, because an
    absence assertion over a resolver that answers `Unresolved` for everything
    is worthless.
    """
    repository = FakeTitleRepository()
    held = _title(imdb_id="tt99000011", name="Shawshank", sort_name="Shawshank")
    other = _title(imdb_id="tt99000012", name="Godfather", sort_name="Godfather")
    await repository.add(held)
    await repository.add(other)
    catalog = repository.stored()
    assert len(catalog) >= 2, "the premise: the target holds more than one title to miss"

    missing = TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99009999")
    answers = await resolve_titles(repository, [title_reference(held), missing])

    resolved_ok = answers[title_reference(held)]
    assert resolved_ok is not None, "the premise: the resolver resolves what the target holds"
    assert resolved_ok == held.id

    refused = answers[missing]
    assert isinstance(refused, Unresolved), f"a missing key answered {refused!r}, not a refusal"
    assert refused.reference is missing
    assert "tt99009999" in " ".join(refused.keys_tried)


async def test_two_catalogs_built_from_one_dump_share_no_title_id() -> None:
    """Two catalogs built from one dump share no title id.

    `db/repositories/bulk.py` mints `new_id()` for every row on the way into the
    staging table and resolves the collision with
    `ON CONFLICT (imdb_id) WHERE imdb_id IS NOT NULL DO UPDATE`. So *within* one
    database a re-import is idempotent and a title keeps the id it was first
    given; *across* two databases built from the same `title.basics.tsv.gz`
    every title gets a different id, because the `new_id()` calls are
    independent -- there is no seed and no derivation from `imdb_id`. The
    natural key is the only thing the two catalogs agree on, which is why a
    backup carries that and not the id.
    """
    one, two = FakeBulkCatalogRepository(), FakeBulkCatalogRepository()
    await one.upsert_titles(_DUMP)
    await two.upsert_titles(_DUMP)

    ids_one = {row.imdb_id: one.title_id(row.imdb_id) for row in _DUMP}
    ids_two = {row.imdb_id: two.title_id(row.imdb_id) for row in _DUMP}
    assert None not in ids_one.values() and None not in ids_two.values(), (
        "the premise: both catalogs really loaded the dump"
    )
    assert len(set(ids_one.values())) == len(_DUMP), (
        "the premise: one catalog gives its own rows distinct ids"
    )

    assert set(ids_one.values()).isdisjoint(set(ids_two.values())), (
        "two catalogs built from one dump shared a title id, which would make "
        "carrying the id sound and this whole design unnecessary"
    )

    for row in _DUMP:
        left = title_reference(_title(kind=row.kind, imdb_id=row.imdb_id, id=ids_one[row.imdb_id]))
        right = title_reference(_title(kind=row.kind, imdb_id=row.imdb_id, id=ids_two[row.imdb_id]))
        assert left.id != right.id
        assert (left.kind, left.imdb_id, left.tmdb_id) == (right.kind, right.imdb_id, right.tmdb_id)


@pytest.mark.parametrize("spelling", ["TT99000011", "tt99000011 ", " tt99000011"])
async def test_the_resolver_is_case_and_whitespace_exact(spelling: str) -> None:
    r"""`tt99000011` and `TT99000011` are different keys.

    `replace_aliases` folds under SQL `lower()` because it is comparing a
    *name* somebody typed; a provider id is not that. IMDb issues one
    spelling, `Title.imdb_id` carries `pattern=r"^tt\d{7,8}$"`, and a
    resolver that folded would let an artifact naming `TT99000011` claim a
    row it was not written from -- a silent mis-attribution of watch state,
    which is the one thing the raw-id rung exists to make impossible.

    The premise is the same reference with the exact spelling resolving.
    """
    repository = FakeTitleRepository()
    held = _title(imdb_id="tt99000011")
    await repository.add(held)

    exact = TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99000011")
    assert await resolve_titles(repository, [exact]) == {exact: held.id}, (
        "the premise: the exact spelling resolves"
    )

    variant = TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id=spelling)
    assert isinstance((await resolve_titles(repository, [variant]))[variant], Unresolved)


async def test_a_tmdb_id_without_a_kind_is_refused_by_the_type() -> None:
    """A `tmdb_id` without a kind is refused by the type.

    TMDb's movie and series id spaces overlap, so `tmdb_id` alone is not an
    identity and `ix_titles_tmdb_id_kind` is composite for that reason. A
    default would be the defect: `kind=MOVIE` for a reference whose title is a
    series resolves to whichever film shares the integer, and every watch state
    carried against it lands on the wrong show. `TitleReference` makes `kind`
    required, so the mistake is a `TypeError` at construction rather than a
    wrong row at restore.
    """
    with pytest.raises(TypeError):
        TitleReference(id=new_id(), tmdb_id=99000550)  # type: ignore[call-arg]


async def test_two_titles_sharing_a_tmdb_id_across_kinds_resolve_to_their_own() -> None:
    """The behavioural half of the case above.

    The fallback rung is `(kind, tmdb_id)` and not `tmdb_id`.
    """
    repository = FakeTitleRepository()
    film = _title(kind=TitleKind.MOVIE, tmdb_id=99000550, name="Film", sort_name="Film")
    show = _title(kind=TitleKind.SERIES, tmdb_id=99000550, name="Show", sort_name="Show")
    await repository.add(film)
    await repository.add(show)

    references = [title_reference(film), title_reference(show)]
    answers = await resolve_titles(repository, references)

    assert answers[references[0]] == film.id
    assert answers[references[1]] == show.id
    assert film.id != show.id, "the premise: the two rows really are two rows"


async def test_a_title_with_neither_provider_id_is_carried_by_raw_id_and_only_accepted_when_the_target_already_holds_it() -> (  # noqa: E501
    None
):
    """A raw id is accepted only where the target already holds it.

    A title with neither provider id is a first-class citizen, so the raw-id
    rung is exercised by real rows rather than being defensive. It is a *check*
    rather than a trust: the artifact carries the UUID and restore accepts it if
    and only if the target already holds a title with that exact id, which is
    the same-database case expressed as a lookup rather than as a mode. Both
    arms in one case, because "accepted" alone is satisfied by a resolver that
    trusts every id it is handed.
    """
    orphan = _title(name="Home Movie", sort_name="Home Movie")
    assert orphan.imdb_id is None and orphan.tmdb_id is None, (
        "the premise: this fixture really has neither provider id"
    )
    reference = title_reference(orphan)
    assert reference.imdb_id is None and reference.tmdb_id is None

    holding = FakeTitleRepository()
    await holding.add(orphan)
    assert (await resolve_titles(holding, [reference]))[reference] == orphan.id

    elsewhere = FakeTitleRepository()
    await elsewhere.add(_title(imdb_id="tt99000011", name="Other", sort_name="Other"))
    refused = (await resolve_titles(elsewhere, [reference]))[reference]
    assert isinstance(refused, Unresolved), (
        "a raw id was accepted against a target that does not hold it"
    )
    assert str(orphan.id) in " ".join(refused.keys_tried)


async def test_an_empty_batch_asks_the_repository_nothing() -> None:
    """`resolve_natural_keys([])` is a round trip to learn nothing.

    The same guard `list_by_ids` and `resolve_tmdb_ids` carry.
    """
    assert await resolve_titles(FakeTitleRepository(), []) == {}


async def test_every_reference_gets_an_answer_even_when_it_repeats() -> None:
    """Restore iterates the mapping it is handed.

    A reference absent from it is a row silently written with no target, and a
    repeated reference -- two watch states for one title, the ordinary shape of
    a household -- is one probe and two answers.
    """
    repository = FakeTitleRepository()
    held = _title(imdb_id="tt99000011")
    await repository.add(held)
    reference = title_reference(held)
    missing = TitleReference(kind=TitleKind.MOVIE, id=new_id(), imdb_id="tt99009999")

    answers = await resolve_titles(repository, [reference, missing, reference, missing])

    assert set(answers) == {reference, missing}
    assert answers[reference] == held.id
    assert isinstance(answers[missing], Unresolved)


def test_the_reference_a_title_is_carried_as_names_every_key_the_row_has() -> None:
    """The builder decides what an artifact writes down.

    Asserted directly rather than only through a resolve: a builder that dropped
    `tmdb_id` would still pass every case above, because every one of them
    resolves on the rung before it. `uuid.UUID` for the id and `TitleKind` for
    the kind are carried whatever the provider ids say, because the kind is half
    of the tmdb rung's key and the id is the last rung.
    """
    title = _title(kind=TitleKind.SERIES, imdb_id="tt99000011", tmdb_id=99000550)
    reference = title_reference(title)
    assert (reference.kind, reference.id, reference.imdb_id, reference.tmdb_id) == (
        TitleKind.SERIES,
        title.id,
        "tt99000011",
        99000550,
    )


def test_the_unresolved_rules_are_the_ones_this_group_argued_for() -> None:
    """Stored per table rather than derived.

    The answer is a function of the table and of nothing else recorded, and the
    two differ on purpose. A `watch_states` row whose title is missing is a real
    loss the operator must see, and it is recoverable: enrich the title, restore
    again. `search_queries.clicked_title_id` is already `ON DELETE SET NULL`, so
    `NULL` is a state the column and every reader handle, and the analytic value
    is the query text and the outcome rather than the id.
    """
    from usher.db.backup_identity import UNRESOLVED_RULE, UnresolvedRule

    assert UNRESOLVED_RULE["watch_states"] is UnresolvedRule.REFUSE
    assert UNRESOLVED_RULE["media_items"] is UnresolvedRule.REFUSE
    assert UNRESOLVED_RULE["search_queries"] is UnresolvedRule.NULL


async def test_an_episode_whose_series_resolves_but_whose_numbers_do_not_is_refused_by_name() -> (
    None
):
    """The episode half of "a named refusal, never `None`".

    The series resolving while the numbers do not is the failure that matters: a
    resolver that answered the *series'* id here would write a watch state
    against the wrong row, and both a `None` and a wrong id are silent. The
    premise below separates this from an episode whose series is missing too --
    that case is refused for a different reason and would pass this assertion
    without the numbers ever being consulted.
    """
    repository = FakeEpisodeRepository()
    series = _title(kind=TitleKind.SERIES, imdb_id="tt99000011")
    series_reference = title_reference(series)
    repository.title_keys[series.id] = series_reference

    held = Episode.model_validate(
        {
            "title_id": series.id,
            "season_id": new_id(),
            "season_number": 1,
            "episode_number": 1,
            "name": "Pilot",
        }
    )
    await repository.upsert_episodes([held])

    resolved = episode_reference(held, series_reference)
    missing = EpisodeReference(title=series_reference, season_number=1, episode_number=99)

    answers = await resolve_episodes(repository, [resolved, missing])

    assert answers[resolved] == held.id, (
        "the premise: the series really does resolve, so the refusal below is "
        "about the numbers rather than about the series"
    )
    refused = answers[missing]
    assert isinstance(refused, Unresolved), "a missing episode was not refused by name"
    assert refused != held.id and refused is not None
    assert all("S01E99" in one for one in refused.keys_tried), (
        f"the refusal does not name the episode it looked for: {refused.keys_tried}"
    )
    assert any("imdb_id=tt99000011" in one for one in refused.keys_tried), (
        "the refusal names the episode but not the series it hangs from"
    )


def test_the_rendered_rungs_name_the_kind_beside_the_tmdb_id() -> None:
    """The rendered tmdb rung names the kind beside the id.

    A rung rendered `tmdb_id=99000550` rather than `series+tmdb_id=99000550`
    reads, in a report about a failed restore, as though one number were looked
    up -- the namespacing mistake this layer refuses to make, restated in the
    one place a human sees it.
    """
    reference = title_reference(_title(kind=TitleKind.SERIES, tmdb_id=99000550))
    rendered = keys_tried(reference)

    assert any(one == "series+tmdb_id=99000550" for one in rendered), rendered
    assert not any(one == "tmdb_id=99000550" for one in rendered), (
        "the tmdb rung is rendered without its kind"
    )
    assert rendered[-1].startswith("id="), "the raw id is the last rung, not the first"


async def test_resolution_order_names_the_ladder_both_arms_actually_walk() -> None:
    """`RESOLUTION_ORDER` is bound to behaviour, not to a second copy of itself.

    Each rung is exercised by a reference that can resolve *only* through it, so
    a constant that renames or reorders a rung disagrees with what the resolver
    does. Its only other occurrences are its `__all__` entry and prose, so
    without this a rewritten constant would survive the whole suite: a
    documented constant nothing reads is documentation, not a contract.
    """
    assert RESOLUTION_ORDER == ("imdb_id", "kind+tmdb_id", "id")

    by_imdb = _title(imdb_id="tt99000011", tmdb_id=None)
    by_tmdb = _title(kind=TitleKind.SERIES, imdb_id=None, tmdb_id=99000550)
    by_raw = _title(imdb_id=None, tmdb_id=None)

    repository = FakeTitleRepository()
    for title in (by_imdb, by_tmdb, by_raw):
        await repository.add(title)

    walked = {
        "imdb_id": title_reference(by_imdb),
        "kind+tmdb_id": title_reference(by_tmdb),
        "id": title_reference(by_raw),
    }
    assert set(walked) == set(RESOLUTION_ORDER), (
        "a rung is named in the constant that no reference here exercises"
    )

    answers = await resolve_titles(repository, list(walked.values()))
    for rung, reference in walked.items():
        assert answers[reference] is not None and not isinstance(answers[reference], Unresolved), (
            f"the {rung} rung does not resolve, so the constant names a rung the arms do not walk"
        )
