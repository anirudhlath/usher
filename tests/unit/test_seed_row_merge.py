"""The merge of a seed's three candidate lists into one row."""

import uuid

from usher.domain.seed_rows import SEED_ROW_CARDS, CardSource
from usher.services.seed_row_merge import RRF_K, merge_seed_row

# An odd stride that scrambles the low bits `hash()` keys a set by, so a merge that sorts by
# nothing and merely iterates a set does not come out in id order by accident.
_STRIDE = 0x9E3779B97F4A7C15


def _ids(count: int, *, block: int = 0x40) -> list[uuid.UUID]:
    """Ids in ascending order, so `ids[0] < ids[1]`."""
    return [uuid.UUID(int=(block << 120) + n * _STRIDE) for n in range(count)]


def _row(
    seed: uuid.UUID,
    *,
    named: list[uuid.UUID] | None = None,
    recommended: list[uuid.UUID] | None = None,
    close: list[uuid.UUID] | None = None,
    votes: dict[uuid.UUID, int] | None = None,
    limit: int = SEED_ROW_CARDS,
) -> list[uuid.UUID]:
    cards = merge_seed_row(
        seed_id=seed,
        named=named or [],
        recommended=recommended or [],
        close=close or [],
        votes=votes or {},
        limit=limit,
    )
    return [card.title_id for card in cards]


def test_the_rrf_constant_is_the_specs_sixty() -> None:
    assert RRF_K == 60


def test_the_row_alternates_named_and_merged_and_never_shows_a_close_only_title() -> None:
    a, b, c, d, e, f, seed = _ids(7)
    cards = merge_seed_row(
        seed_id=seed,
        named=[a, b, c],
        recommended=[d, b],
        close=[e, c, f],
        votes={a: 100, b: 100, c: 100, d: 200, e: 900, f: 900},
    )
    # Premises: b scores 2/62, c 1/63 + 1/62, and a and d tie at 1/61, broken by votes.
    assert 2 / 62 > 1 / 63 + 1 / 62 > 1 / 61
    assert [x.title_id for x in cards] == [a, b, c, d]
    assert [x.sources for x in cards] == [
        {CardSource.NAMED},
        {CardSource.NAMED, CardSource.RECOMMENDED},
        {CardSource.NAMED, CardSource.CLOSE},
        {CardSource.RECOMMENDED},
    ]
    # Ids would have put a ahead of d: the votes are what moved d.
    assert a < d


def test_a_close_only_title_is_never_a_card_however_many_votes_it_has() -> None:
    named, close_only, seed = _ids(3)
    assert _row(
        seed, named=[named], close=[close_only, named], votes={named: 1, close_only: 10_000_000}
    ) == [named]


def test_close_raises_a_title_another_list_found() -> None:
    filler, low, high, seed = _ids(4)
    # Without Close, `low` outranks `high` in Merged and takes position 2.
    assert _row(seed, named=[filler], recommended=[low, high]) == [filler, low, high]
    # With Close, `high` carries a second term and takes it.
    assert _row(seed, named=[filler], recommended=[low, high], close=[high]) == [
        filler,
        high,
        low,
    ]


def test_equal_scores_and_votes_break_by_id() -> None:
    lead, other, x1, y1, x2, y2, x3, y3, seed = _ids(9)
    # Each x sits at (Named n+1, Recommended n) and its y at (n, n+1): the same two fractions.
    # Three such pairs, so that no accident of set or insertion order sorts all three.
    assert x1 < y1 and x2 < y2 and x3 < y3
    row = _row(
        seed,
        named=[lead, y1, x1, y2, x2, y3, x3],
        recommended=[other, x1, y1, x2, y2, x3, y3],
        votes=dict.fromkeys((x1, y1, x2, y2, x3, y3), 10),
    )
    # Even positions take Merged's head, the smaller id of the next pair; odd ones take Named's.
    assert row == [lead, x1, y1, x2, y2, x3, y3, other]


def test_votes_break_a_tie_in_score_before_the_id_does() -> None:
    first, second, lead, other, seed = _ids(5)
    assert first < second
    row = _row(
        seed,
        named=[lead, second, first],
        recommended=[other, first, second],
        votes={first: 10, second: 11},
    )
    assert row[:3] == [lead, second, first]


def test_a_true_tie_is_a_tie_in_exact_arithmetic() -> None:
    f, a, g1, g2, g3, g4, b, r2, r3, r4, r5, r6, seed = _ids(13)
    # a is at (Named 2, Recommended 7, Close 1) and b at (7, 1, 2): the same three
    # fractions, which floats sum to values an ulp apart.
    assert 1 / 62 + 1 / 67 + 1 / 61 != 1 / 67 + 1 / 61 + 1 / 62
    assert a < b
    row = _row(
        seed,
        named=[f, a, g1, g2, g3, g4, b],
        recommended=[b, r2, r3, r4, r5, r6, a],
        close=[a, b],
        votes={a: 1, b: 2},
    )
    # The votes decide, so b is Merged's head at position 2 and a is Named's next at 3.
    assert row[:3] == [f, b, a]


def test_a_row_stops_at_twenty_cards() -> None:
    named = _ids(30)
    seed = _ids(1, block=0x41)[0]
    assert len(named) > SEED_ROW_CARDS
    row = _row(seed, named=named)
    assert len(row) == SEED_ROW_CARDS == 20
    assert row == named[:20]


def test_the_limit_can_be_lowered() -> None:
    named = _ids(6)
    assert _row(_ids(1, block=0x41)[0], named=named, limit=3) == named[:3]


def test_when_named_runs_out_merged_fills_the_rest_and_the_reverse() -> None:
    n1, r1, n2, r2, r3, r4, seed = _ids(7)
    # n1 and r1 tie at 1/61, n2 and r2 at 1/62, and with no votes the smaller id leads.
    assert n1 < r1 and n2 < r2
    assert _row(seed, named=[n1, n2], recommended=[r1, r2, r3, r4]) == [n1, r1, n2, r2, r3, r4]
    # Merged holds every Named title, so it cannot run dry first: with one Recommended
    # title it hands position 4 and 6 to the next Named ones.
    m1, m2, m3, m4, m5, only, seed = _ids(7, block=0x42)
    assert m1 < only
    assert _row(seed, named=[m1, m2, m3, m4, m5], recommended=[only]) == [m1, only, m2, m3, m4, m5]


def test_a_row_with_no_named_titles_is_the_recommendations_in_order() -> None:
    *recs, seed = _ids(6)
    cards = merge_seed_row(seed_id=seed, named=[], recommended=recs, close=[], votes={})
    assert [x.title_id for x in cards] == recs
    assert all(x.sources == {CardSource.RECOMMENDED} for x in cards)


def test_a_row_with_only_named_titles_is_the_named_order() -> None:
    *named, seed = _ids(6)
    cards = merge_seed_row(seed_id=seed, named=named, recommended=[], close=[], votes={})
    assert [x.title_id for x in cards] == named
    assert all(x.sources == {CardSource.NAMED} for x in cards)


def test_the_seed_is_never_a_card() -> None:
    a, b, seed = _ids(3)
    cards = merge_seed_row(
        seed_id=seed,
        named=[seed, a],
        recommended=[b, seed],
        close=[seed, a],
        votes={seed: 10_000_000},
    )
    assert [x.title_id for x in cards] == [a, b]
    assert [x.sources for x in cards] == [
        {CardSource.NAMED, CardSource.CLOSE},
        {CardSource.RECOMMENDED},
    ]


def test_a_title_named_twice_is_one_card() -> None:
    a, b, c, seed = _ids(4)
    assert _row(seed, named=[a, b, a, c]) == [a, b, c]


def test_a_title_repeated_in_a_list_scores_its_first_rank_only() -> None:
    lead, b, a, seed = _ids(4)
    # Counted twice, a would score 1/62 + 1/63 and pass b at 1/61.
    assert 1 / 62 + 1 / 63 > 1 / 61
    assert _row(seed, named=[lead], recommended=[b, a, a]) == [lead, b, a]
