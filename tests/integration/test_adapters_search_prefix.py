"""`PostgresPrefixSuggestIndex` -- tier 1 of the suggest, a btree prefix probe."""

import ast
import inspect
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.suggest_index_contract import SuggestIndexContract
from tests.integration.conftest import A_DECISIVE_MARGIN, Analyze, index_suspended
from usher.adapters.search import prefix as prefix_module
from usher.adapters.search.postgres import _MAX_DISTANCE, _SUGGEST, _TRIGRAM_THRESHOLD
from usher.adapters.search.prefix import _PREFIX, PostgresPrefixSuggestIndex
from usher.domain.enums import EnrichmentState, SearchNameKind, TitleKind
from usher.domain.ids import new_id
from usher.ports.search import SuggestIndex

# The tier-1 index `m09a` builds on `titles`, and the near-miss beside it.
_TIER_ONE_INDEX = "ix_titles_name_lower_prefix"
_NEAR_MISS_INDEX = "ix_titles_name_lower_year"
_TIER_TWO_INDEX = "ix_titles_name_trgm"

_ENOUGH_TO_PLAN_AGAINST = 2_000
"""Filler rows behind the tier-1 plan assertion.

One row is a table the planner sizes off an empty `pg_class`, where every
candidate index costs the same -- see `_plan_tree`. Two thousand is the same
order the other plan assertions in this suite seed.
"""


async def _given_title(
    session: AsyncSession,
    *,
    name: str,
    popularity: float | None = None,
    vote_count: int | None = None,
) -> uuid.UUID:
    """One `titles` row.

    Every name in this file is invented (see `tests/unit/test_no_third_party_data.py`).

    A raw `INSERT` rather than `PostgresTitleRepository.add`, for the reason
    the neighbouring file gives: a `Title` has 31 fields nothing here has an
    opinion about, and what this path reads is `name`, `tmdb_popularity`,
    `tmdb_vote_count` and `id`.
    """
    title_id = new_id()
    await session.execute(
        text(
            # The bind names are not the column names: `:popularity` and
            # `:vote_count` are this helper's own keyword arguments, which are
            # test-local vocabulary, while the columns are
            # `tmdb_popularity`/`tmdb_vote_count`.
            "INSERT INTO titles (id, kind, name, sort_name, tmdb_popularity, "
            "tmdb_vote_count, enrichment_state) VALUES (CAST(:id AS uuid), :kind, :name, "
            ":sort_name, :popularity, :vote_count, :state)"
        ),
        {
            "id": title_id,
            "kind": TitleKind.MOVIE.value,
            "name": name,
            "sort_name": name,
            "popularity": popularity,
            "vote_count": vote_count,
            "state": EnrichmentState.ENRICHED.value,
        },
    )
    return title_id


async def _given_search_name(
    session: AsyncSession, *, title_id: uuid.UUID, name: str, kind: SearchNameKind
) -> None:
    """One `title_search_names` row -- an alias or a person -- for a title.

    `m09a` ships this table empty and with no writer in `src/`; Track 2's
    `title.akas` loader and the people half of the two-tier suggest are the
    two emitters it was built for. Until one of them lands, an insert here is
    the only way to make the union's second arm observable at all.
    """
    await session.execute(
        text(
            "INSERT INTO title_search_names (id, title_id, name, kind) "
            "VALUES (CAST(:id AS uuid), CAST(:title_id AS uuid), :name, :kind)"
        ),
        {"id": new_id(), "title_id": title_id, "name": name, "kind": kind.value},
    )


def _index_names(node: dict[str, Any]) -> list[str]:
    """Every `Index Name` in a plan tree, in no particular order.

    A small recursive walk rather than a dependency, the same call
    `_actual_rows` makes one file over: the tree is a handful of dicts and a
    `Plans` list, and a library that parsed it would be a second thing to keep
    current with PostgreSQL's own JSON.
    """
    found = [node["Index Name"]] if "Index Name" in node else []
    for child in node.get("Plans", ()):
        found.extend(_index_names(child))
    return found


def _index_conditions(node: dict[str, Any]) -> list[str]:
    """Every `Index Cond` in a plan tree, the same walk as `_index_names`.

    Naming the index is the weaker half of the claim: an index scan that walks
    the whole index and puts the predicate in a `Filter` reaches the index by
    name while doing none of the work the index exists for, which is what a
    plan reaching `titles` through `pk_titles` is. `Index Cond` is Postgres
    saying it used the predicate to *position* the scan.
    """
    found = [node["Index Cond"]] if "Index Cond" in node else []
    for child in node.get("Plans", ()):
        found.extend(_index_conditions(child))
    return found


async def _plan_tree(
    session: AsyncSession, statement: str, parameters: dict[str, Any]
) -> dict[str, Any]:
    """The plan the planner takes for `statement`, with sequential scans disabled."""
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = await session.execute(text(f"EXPLAIN (FORMAT JSON) {statement}"), parameters)
    return cast("dict[str, Any]", plan.scalar_one()[0]["Plan"])


async def _plan(session: AsyncSession, statement: str, parameters: dict[str, Any]) -> list[str]:
    """The index names in `_plan_tree`'s plan."""
    return _index_names(await _plan_tree(session, statement, parameters))


async def _given_a_catalog_to_plan_against(session: AsyncSession, rows: int) -> None:
    """Enough of a catalog that the planner can tell one index from another.

    Bulk `INSERT ... SELECT` rather than `rows` calls to `_given_title`: this
    is scenery, and the small hand-written helpers above are for the rows a
    case actually asserts on. **None of these names begins with the prefix the
    cases probe** -- the point is a population the probe is *selective*
    against, because an index that returns the whole table is not doing
    anything a scan would not.
    """
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, enrichment_state) "
            "SELECT gen_random_uuid(), :kind, 'Filler ' || i, 'Filler ' || i, :state "
            "FROM generate_series(1, :rows) AS i"
        ),
        {"kind": TitleKind.MOVIE.value, "state": EnrichmentState.ENRICHED.value, "rows": rows},
    )
    await session.execute(
        text(
            "INSERT INTO title_search_names (id, title_id, name, kind) "
            "SELECT gen_random_uuid(), t.id, 'Filler Alias ' || t.sort_name, :kind "
            "FROM titles AS t WHERE t.name LIKE 'Filler %'"
        ),
        {"kind": SearchNameKind.ALIAS.value},
    )


@pytest.mark.integration
async def test_the_prefix_tier_answers_a_prefix_and_finds_no_typo(session: AsyncSession) -> None:
    """The tier's whole shape in one case, positive control first.

    The positive arm runs before the absence arm on purpose: an assertion that
    a misspelt prefix returns nothing is satisfied by an implementation that
    returns nothing for *everything* -- a wrong table name, a pattern that can
    never match, a session that was never given a row. The distractor is far
    more popular and shares no prefix, so an implementation whose predicate
    matches everything puts it first and fails the positive arm. The absence is
    the tier's design, not its defect: tier 2 carries the typo tolerance, and
    tier 1 is what a keystroke can afford.
    """
    await _given_title(session, name="Harbour Lights", popularity=900.0)
    wanted = await _given_title(session, name="Vane", popularity=1.0)
    index = PostgresPrefixSuggestIndex(session)

    assert [hit.title_id for hit in await index.suggest("van")] == [wanted]
    assert await index.suggest("vame") == []


@pytest.mark.integration
class TestPostgresPrefixSuggestIndex(SuggestIndexContract):
    """The base contract's second arm.

    `SuggestIndexContract` split when this class arrived: the prefix and
    ordering cases are what *every* `SuggestIndex` owes and stay on the base,
    while the two typo cases and the candidate cap moved to
    `TypoTolerantSuggestIndexContract`, which this implementation deliberately
    does not subclass. A contract suite that ran here and skipped everything
    would read as coverage and measure nothing.
    """

    @pytest_asyncio.fixture
    async def index(self, session: AsyncSession) -> AsyncIterator[PostgresPrefixSuggestIndex]:
        yield PostgresPrefixSuggestIndex(session)

    @pytest.fixture(autouse=True)
    def _bind_session(self, session: AsyncSession) -> None:
        self._session = session

    async def given_title(self, index: SuggestIndex, *, name: str, popularity: float) -> uuid.UUID:
        """The port has no write method, and this implementation writes nothing.

        It reads two tables somebody else owns, so the arrangement is an
        insert -- the honest shape of a read-only port, and the reason this is
        a hook rather than a convenience.
        """
        return await _given_title(self._session, name=name, popularity=popularity)


@pytest.mark.integration
async def test_a_person_name_reaches_their_film_from_the_first_keystroke(
    session: AsyncSession,
) -> None:
    """The union's second arm deleted -- the statement reading `titles` alone.

    That is the plausible simplification: `title_search_names` ships empty in
    `m09a`, so on a fresh install dropping the arm changes no answer at all
    and every case that only seeds `titles` stays green. The damage arrives
    with the loaders -- typing a director's name finds nothing, which is one
    of the two things PRD 05 says the narrow table exists for.

    The distractor is 900x more popular and matches nothing, so an
    implementation returning its whole table ordered by popularity puts it
    first rather than answering correctly by accident.
    """
    await _given_title(session, name="Zenith Parade", popularity=900.0)
    film = await _given_title(session, name="Harbour Lights", popularity=1.0)
    await _given_search_name(
        session, title_id=film, name="Vane Ashgrove", kind=SearchNameKind.PERSON
    )

    hits = await PostgresPrefixSuggestIndex(session).suggest("vane")

    assert [hit.title_id for hit in hits] == [film]


@pytest.mark.integration
async def test_a_title_matched_by_both_arms_is_returned_once(session: AsyncSession) -> None:
    """`UNION ALL` in place of `UNION`.

    A film whose canonical name *and* whose alias both start with the typed
    prefix is one row of the type-ahead box, not two. `UNION ALL` is the
    cheaper operator and the one a reader reaches for when the arms look
    disjoint; here they are not, and the duplicate is invisible to any case
    that seeds only one arm.

    Asserted on the length as well as the membership, because
    `[hit.title_id for hit in hits] == [film]` alone is what a de-duplicating
    implementation and a truncating one both produce.
    """
    film = await _given_title(session, name="Vane Alpha", popularity=1.0)
    await _given_search_name(
        session, title_id=film, name="Vane Alternate", kind=SearchNameKind.ALIAS
    )

    hits = await PostgresPrefixSuggestIndex(session).suggest("vane")

    assert len(hits) == 1
    assert hits[0].title_id == film


@pytest.mark.integration
async def test_both_sides_of_the_comparison_are_lower_cased(session: AsyncSession) -> None:
    """Lower-casing one side of the comparison and not the other.

    Two spellings, one case, because a fixture whose name and query are both
    lower case cannot see either of them:

    - the column not lowered (`name LIKE 'van%'`) misses `Vane Alpha`, and
      also stops the `lower(name)` index from being usable at all;
    - the prefix not lowered (`lower(name) LIKE 'VaN%'`) misses it too.

    So the name carries capitals and the typed prefix carries a *different*
    pattern of capitals, which is the ordinary state of a type-ahead box
    (nobody holds shift for the second letter). The negative arm is the same
    query with the case swapped again, so a correct implementation is
    case-blind rather than merely lucky.
    """
    film = await _given_title(session, name="Vane Alpha", popularity=1.0)
    index = PostgresPrefixSuggestIndex(session)

    assert [hit.title_id for hit in await index.suggest("VaN")] == [film]
    assert [hit.title_id for hit in await index.suggest("vAn")] == [film]


@pytest.mark.integration
async def test_the_cap_is_ordered_so_the_top_of_the_list_is_not_arbitrary(
    session: AsyncSession,
) -> None:
    """The `ORDER BY` deleted from the statement's `LIMIT`."""
    popularity = {number: float(number) for number in range(20)}
    seeded = [
        await _given_title(session, name=f"Vane {number:04d}", popularity=popularity[number])
        for number in range(20)
    ]
    ranked = sorted(popularity, key=popularity.__getitem__, reverse=True)
    wanted = [seeded[number] for number in ranked[:3]]
    assert set(wanted).isdisjoint(seeded[:3]), (
        "the premise: the three most popular are not the three a scan reaches first, so a "
        "cap that truncates in physical order cannot return them by luck"
    )

    hits = await PostgresPrefixSuggestIndex(session).suggest("vane", limit=3)

    assert [hit.title_id for hit in hits] == wanted


@pytest.mark.integration
async def test_a_wildcard_typed_into_the_box_is_not_a_wildcard(session: AsyncSession) -> None:
    """`LIKE`'s own metacharacters reaching the pattern, and the ways escaping fails.

    Four arms, because each kills a different spelling and no one of them kills
    the others.
    """
    await _given_title(session, name="Vane Alpha", popularity=1.0)
    await _given_title(session, name="Harbour Lights", popularity=900.0)
    percentage = await _given_title(session, name="100% Vane", popularity=1.0)
    backslash = await _given_title(session, name="\\Vane", popularity=1.0)
    index = PostgresPrefixSuggestIndex(session)

    assert await index.suggest("%") == []
    assert await index.suggest("_") == []
    assert [hit.title_id for hit in await index.suggest("100%")] == [percentage]
    assert [hit.title_id for hit in await index.suggest("\\")] == [backslash]


@pytest.mark.integration
async def test_an_empty_prefix_reads_nothing(session: AsyncSession) -> None:
    """The guard removed, so an empty box sorts the whole catalog.

    A type-ahead box is empty on every page load and after every backspace to
    zero. Without the guard that keystroke is `LIKE '%'`: 1,271,138 rows
    unioned, deduplicated and sorted by popularity to answer a question nobody
    asked. The trigram tier answers nothing for an empty prefix too, by
    accident of `similarity(name, '') = 0`; here it is a decision, so it is a
    case.

    The second arm is whitespace, which is what a space bar produces and what
    `''` does not cover.
    """
    await _given_title(session, name="Vane Alpha", popularity=1.0)
    index = PostgresPrefixSuggestIndex(session)

    assert await index.suggest("") == []
    assert await index.suggest("   ") == []


@pytest.mark.integration
async def test_the_tier_one_statement_plans_to_the_prefix_index_and_not_the_near_miss(
    session: AsyncSession,
    analyze: Analyze,
) -> None:
    """The statement reaching `titles` any way other than through the `text_pattern_ops` btree."""
    await _given_a_catalog_to_plan_against(session, _ENOUGH_TO_PLAN_AGAINST)
    film = await _given_title(session, name="Vane Alpha", popularity=1.0)
    await _given_search_name(
        session, title_id=film, name="Vane Ashgrove", kind=SearchNameKind.PERSON
    )
    await analyze("titles", "title_search_names")

    tree = await _plan_tree(session, _PREFIX, {"pattern": "vane%", "limit": 10})
    taken = _index_names(tree)

    assert _TIER_ONE_INDEX in taken, (
        f"the tier-1 statement did not reach {_TIER_ONE_INDEX}; only the `text_pattern_ops` "
        "operator class can serve `LIKE 'pre%'` under a non-C collation"
    )
    assert "ix_title_search_names_name_lower_prefix" in taken
    assert _NEAR_MISS_INDEX not in taken, (
        f"the plan reached {_NEAR_MISS_INDEX}, which is a default-opclass btree on "
        "(lower(name), year) and cannot answer a prefix -- see m09a's docstring"
    )
    positioned = [one for one in _index_conditions(tree) if "lower(name)" in one]
    assert len(positioned) == 2, (
        "both arms have to reach their index by an `Index Cond` on `lower(name)`, or the "
        f"absence assertion above is satisfied by a plan that indexes nothing: {tree}"
    )
    async with index_suspended(session, _TIER_ONE_INDEX):
        without = await _plan_tree(session, _PREFIX, {"pattern": "vane%", "limit": 10})
    assert _TIER_ONE_INDEX not in _index_names(without), (
        f"the index was not suspended, so the margin below measures nothing: {without}"
    )
    assert without["Total Cost"] > tree["Total Cost"] * A_DECISIVE_MARGIN, (
        f"the tier-1 index wins by too little for the choice to be a property of the "
        f"schema: {tree['Total Cost']} against {without['Total Cost']}"
    )


@pytest.mark.integration
async def test_the_trigram_index_is_still_gin_and_tier_two_still_plans_to_it(
    session: AsyncSession,
) -> None:
    """A GiST trigram index added beside the GIN one is what this refuses.

    With both present the planner takes GiST for `%`, and the shipped
    configuration gets several times slower for byte-identical recall. Tier 1
    adds a *btree*, which no `%` plan can take. Three assertions, and the first
    two are not redundant with the third: `pg_indexes` says the index exists
    and says `USING gin`, which no plan shape can say, because GiST serves `%`
    too; the third asserts the index the planner actually **takes**.
    """
    for number in range(20):
        await _given_title(session, name=f"Vane {number:04d}", popularity=1.0)

    definitions = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename = 'titles' AND indexdef LIKE '%trgm%'"
        )
    )
    trigram: dict[str, str] = {row.indexname: row.indexdef for row in definitions}
    assert set(trigram) == {_TIER_TWO_INDEX}, (
        "a second trigram index on `titles` -- with GiST beside GIN the planner takes GiST "
        "for `%` and the shipped configuration is 4.3x slower for identical recall"
    )
    assert "USING gin" in trigram[_TIER_TWO_INDEX]

    await session.execute(
        text(f"SET LOCAL pg_trgm.similarity_threshold = {_TRIGRAM_THRESHOLD:.6f}")
    )
    taken = await _plan(
        session,
        _SUGGEST,
        {"prefix": "vane", "candidates": 200, "max_distance": _MAX_DISTANCE, "limit": 10},
    )

    assert _TIER_TWO_INDEX in taken, "tier 2 no longer plans to the trigram index"


@pytest.mark.integration
async def test_the_two_tiers_order_their_answers_the_same_way(session: AsyncSession) -> None:
    """Tier 1 ordering on something tier 2 does not means the box reshuffles.

    Both statements sort `popularity DESC NULLS LAST, vote_count DESC NULLS
    LAST, id ASC`; tier 2 puts edit distance above all three and tier 1 has no
    distance to put there, so on rows both tiers return the orders agree. The
    fixture is what makes this a statement about **all three** keys: three
    exact prefix matches, so distance cannot decide anything; popularity on one
    and absent on the two below it, so `NULLS LAST` decides; and the low-vote
    row **inserted before** the high-vote one, so id order and vote-count order
    disagree -- seeded the other way round a UUIDv7 key makes `ORDER BY id` and
    `ORDER BY vote_count DESC` the same list.
    """
    popular = await _given_title(session, name="Vane Alpha", popularity=5.0, vote_count=1)
    quiet = await _given_title(session, name="Vane Cedar", popularity=None, vote_count=1)
    voted = await _given_title(session, name="Vane Bravo", popularity=None, vote_count=900)
    assert quiet < voted, (
        "the premise: the low-vote row is written first, so id order puts it above the "
        "high-vote one and the vote-count key is what has to move it"
    )

    tier_one = await PostgresPrefixSuggestIndex(session).suggest("vane")
    await session.execute(
        text(f"SET LOCAL pg_trgm.similarity_threshold = {_TRIGRAM_THRESHOLD:.6f}")
    )
    rows = await session.execute(
        text(_SUGGEST),
        {"prefix": "vane", "candidates": 200, "max_distance": _MAX_DISTANCE, "limit": 10},
    )
    tier_two = [row.id for row in rows]

    assert [hit.title_id for hit in tier_one] == [popular, voted, quiet]
    assert tier_two == [popular, voted, quiet]


@pytest.mark.integration
def test_the_plan_walk_finds_an_index_name_that_is_there() -> None:
    """`_index_names` globbing nothing.

    A helper that walks a nested structure and returns `[]` makes every
    `in taken` assertion above fail loudly and every `not in taken` assertion
    pass silently -- and the silent half is the one guarding the near-miss
    index. So the walk is exercised against a tree with a name at depth two,
    which is where a real plan keeps them.
    """
    tree: dict[str, Any] = {
        "Node Type": "Limit",
        "Plans": [{"Node Type": "Sort", "Plans": [{"Index Name": "ix_somewhere_deep"}]}],
    }
    assert _index_names(tree) == ["ix_somewhere_deep"]
    assert _index_names({"Node Type": "Result"}) == []


@pytest.mark.integration
def test_the_prefix_module_borrows_nothing_from_the_trigram_module() -> None:
    """Tier 1 reaching into `adapters/search/postgres.py` for a constant.

    Tier 1 lives in its own module so that *"`postgres.py` is not edited at
    all"* is a claim `git diff --stat` can settle -- and an import back into
    that module is how the two grow together again without a diff: the next
    reader who wants a shared `_ORDER_BY` fragment or a shared cap moves it
    there, and then the tier-2 statement's constants have a second consumer
    with different measurements behind it. The dependency is deliberately
    one-directional and it currently does not exist in either direction.

    Needs no container and lives here anyway, beside the claim it guards -- a
    guard that lives away from the thing it guards is one the next edit leaves
    behind. Asserted over the parsed module rather than over its text, so a
    sentence of prose naming the other module cannot answer it.
    """
    source = Path(inspect.getsourcefile(prefix_module) or "").read_text()
    imported = {
        node.module for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "usher.ports.search" in imported, (
        "the walk found no import at all; a scan that globs nothing passes exactly like "
        "a scan that passes"
    )
    assert "usher.adapters.search.postgres" not in imported
