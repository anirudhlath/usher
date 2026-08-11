"""`PostgresPrefixSuggestIndex` against real Postgres: tier 1 of the two-tier
suggest, the btree `lower(name) text_pattern_ops` prefix probe.

**This file exists because ADR-0002's typo-tolerance gate failed.** Run
2026-08-03 against 1,271,138 real names, the shipped trigram type-ahead finds
the right title **27.8%** of the time for a 2-4-character name and **68.3%**
for 5-7, against bars of 0.75 and 0.85 written down before the numbers were
known -- and **no configuration under any threshold, cap or index type comes
within 6x of a 50 ms keystroke budget**. The one configuration that does fit is
the btree prefix probe: **p50 0.6 ms / p95 1.0 ms / max 10 ms, 44 MB, 0.559 s
to build**, with **no typo tolerance at all (1.9%)**. Tier 1 is that probe;
tier 2 is the path this file's neighbour tests, debounced behind it.

Three things are asserted here that no shared contract can express, because
they are properties of *this* backend and of the schema `m09a` built:

- **The near-miss index.** `ix_titles_name_lower_year` is a plain btree on
  `(lower(name), year)` with the default operator class. It reads as if it
  would serve `LIKE 'pre%'` and cannot under this database's collation --
  measured, the plan is a `Seq Scan` even with `enable_seqscan = off`. The
  plan case asserts which index the tier-1 statement *takes*.
- **The union.** Tier 1 reads `titles` and `title_search_names` as one
  deduplicated set, so a director's name reaches their films from the first
  keystroke. **B3 is the task authorised to narrow this**, on a measurement.
- **Tier 2 is untouched.** `ix_titles_name_trgm` is still GIN, still the only
  trigram index on `titles`, and the tier-2 statement still plans to it. That
  last clause is the whole assertion: **no plan-shape test can distinguish GIN
  from GiST for `%`** (GiST serves it too), and with a GiST index present
  beside the GIN one the planner takes GiST and the shipped configuration goes
  33.3 ms -> 141.5 ms p50 for byte-identical recall. So the retention case
  asserts the index the planner **takes** and the operator class `pg_indexes`
  reports, never merely that an index exists.

`tests/unit/test_suggest_index_contract.py` and the `TestPostgresSuggestIndex`
class in `test_adapters_search_postgres.py` run the *typo-tolerant* contract;
`TestPostgresPrefixSuggestIndex` below runs the base one, which is the half
both implementations owe.
"""

import ast
import inspect
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.suggest_index_contract import SuggestIndexContract
from usher.adapters.search import prefix as prefix_module
from usher.adapters.search.postgres import _MAX_DISTANCE, _SUGGEST, _TRIGRAM_THRESHOLD
from usher.adapters.search.prefix import _PREFIX, PostgresPrefixSuggestIndex
from usher.domain.enums import EnrichmentState, SearchNameKind, TitleKind
from usher.domain.ids import new_id
from usher.ports.search import SuggestIndex

# The tier-1 index `m09a` builds on `titles`, and the near-miss beside it.
# Both spelled once here so a case asserts the name rather than a substring
# that happens to appear in a plan.
#
# **`ix_titles_name_lower_prefix` is the shipped name, and the M9 plan's own
# acceptance for this task calls it `ix_titles_name_prefix`.** The drift is
# recorded here rather than left for the next reader to re-derive, because of
# what it sits next to: `ix_titles_name_lower_year` differs from the real index
# by a single token in `_SUSPENDABLE_INDEXES` (the opclass), and it is a plain
# btree that cannot answer a prefix at all. Three names one token apart, one of
# which appears only in a plan document -- a half-remembered spelling is one
# search-and-replace away from a green suite asserting over the wrong index.
_TIER_ONE_INDEX = "ix_titles_name_lower_prefix"
_NEAR_MISS_INDEX = "ix_titles_name_lower_year"
_TIER_TWO_INDEX = "ix_titles_name_trgm"


async def _given_title(
    session: AsyncSession,
    *,
    name: str,
    popularity: float | None = None,
    vote_count: int | None = None,
) -> uuid.UUID:
    """One `titles` row. Every name in this file is invented (see
    `tests/unit/test_no_third_party_data.py`).

    A raw `INSERT` rather than `PostgresTitleRepository.add`, for the reason
    the neighbouring file gives: a `Title` has 31 fields nothing here has an
    opinion about, and what this path reads is `name`, `popularity`,
    `vote_count` and `id`.
    """
    title_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name, popularity, vote_count, "
            "enrichment_state) VALUES (CAST(:id AS uuid), :kind, :name, :sort_name, "
            ":popularity, :vote_count, :state)"
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


async def _plan(session: AsyncSession, statement: str, parameters: dict[str, Any]) -> list[str]:
    """The indexes the planner takes for `statement`, with sequential scans
    disabled.

    **The lever is necessary and it is not a thumb on the scale.** At fixture
    scale a sequential scan really is cheaper and the planner is right to take
    it, so without `enable_seqscan = off` the plan says nothing about which
    index *could* serve the query. With it, an index that cannot serve the
    predicate still does not appear: the pre-`m09a` measurement of exactly
    this query against `ix_titles_name_lower_year` is `Seq Scan on titles` at
    cost 1e10 -- not merely not-chosen, **not choosable**. That is what makes
    "the near miss is absent from the plan" a claim about the operator class
    rather than about cost estimation.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = await session.execute(text(f"EXPLAIN (FORMAT JSON) {statement}"), parameters)
    return _index_names(plan.scalar_one()[0]["Plan"])


@pytest.mark.integration
async def test_the_prefix_tier_answers_a_prefix_and_finds_no_typo(session: AsyncSession) -> None:
    """**The tier's whole shape in one case, positive control first.**

    The positive arm runs before the absence arm on purpose: an assertion that
    a misspelt prefix returns nothing is satisfied by an implementation that
    returns nothing for *everything* -- a wrong table name, a pattern that can
    never match, a session that was never given a row. Proving the path
    answers a real prefix first is what makes the second assertion evidence
    about typo tolerance instead of evidence that nothing ran.

    The distractor is 900x more popular and shares no prefix, so an
    implementation whose predicate silently matches everything puts it first
    and fails the positive arm rather than passing it by luck.

    **The absence is the tier's design, not its defect.** Measured over 2,993
    single-edit typo cases at 1,271,138 names, this configuration scores
    **1.9%** -- and 0.6 ms p50 against the trigram path's 33.3 ms. Tier 2 is
    what carries the tolerance; tier 1 is what a keystroke can afford.
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
        """The port has no write method (ADR-0021) and this implementation
        writes nothing either -- it reads two tables somebody else owns. So
        the arrangement is an insert, which is the honest shape of a read-only
        port and the reason this is a hook rather than a convenience."""
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
    """The `ORDER BY` deleted from the statement's `LIMIT`.

    **An unordered cap is not a cheaper cap, it is a different answer**, and
    this project has the measurement: dropping the trigram floor with an
    unordered `LIMIT` in place took recall 66.2% -> 48.5% -> 2.6%, because the
    cap truncates whichever rows the scan reached first. The same statement
    here, with the same shape of defect, hands the type-ahead box its answer
    in `UNION`-hash order.

    Twenty candidates rather than two, with popularity **ascending** in
    insertion order, so the wanted rows are the *last* three written -- and a
    UUIDv7 primary key makes insertion order and id order one sequence, so
    "the last three written" is also "the three a scan reaches last".

    **The premise is derived from the fixture's own mapping rather than from a
    slice**, which is what makes it a guard instead of a comment: written as
    `seeded[-3:]` it states a fact about a literal and no fixture change can
    falsify it, and the obvious fixture change -- seeding popularity
    descending -- would then break the case's final assertion while the guard
    sat there passing.
    """
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
    """`LIKE`'s own metacharacters reaching the pattern, and the three ways
    the escaping can be wrong. **Four arms, because each kills a different
    spelling and no one of them kills the others.**

    - **`%` alone** -- unescaped it matches the entire catalog, which is
      1,271,138 rows collected, de-duplicated and sorted to answer one
      keystroke. Kills "no escaping at all".
    - **`_` alone** -- unescaped it matches every name of one character or
      more, i.e. all of them. Kills an escape list that forgot the underscore,
      which is the one a reader adds second.
    - **`100%`** -- an ordinary film title and an ordinary thing to type.
      Kills the escape list applied in the **wrong order**: doubling the
      backslash *after* introducing the escapes re-escapes the escapes, and
      the `%` becomes a wildcard again. Every other arm here survives that
      spelling, which is why this one exists.
    - **a lone backslash** -- kills an escape list that handles `%` and `_`
      and not the escape character itself, where `\\` + `%` reads as a
      *literal per cent* and the typed backslash is silently dropped.

    All four are ordinary characters in a film title, so refusing them is not
    an option either -- which is what makes this escaping rather than
    validation.
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
) -> None:
    """The statement reaching `titles` any way other than through the
    `text_pattern_ops` btree.

    **`ix_titles_name_lower_year` is the trap this case exists for.** It is
    `(lower(name), year)` with the *default* operator class, it is named as if
    it were a prefix index, it is the entry immediately above the real one in
    `_SUSPENDABLE_INDEXES`, and it **cannot serve `LIKE 'pre%'` at all** under
    this database's collation -- measured on `pgvector/pgvector:pg17` at the
    pre-`m09a` schema, the plan is `Seq Scan on titles` at cost 1e10 with
    `enable_seqscan = off`. With `(lower(name) text_pattern_ops)` present the
    same query is `Index Cond: ((lower(name) ~>=~ 'pre') AND (lower(name) ~<~
    'prf'))`.

    **The statement is imported, never transcribed.** `_PREFIX` is the literal
    constant the implementation issues, so this cannot drift from what ships;
    a hand-copied lookalike that drifts reads exactly like coverage, which is
    how two earlier tasks in this repository were replaced.

    Both arms of the union are asserted, because the second one's index is a
    different index on a different table and a statement that lost it would
    still plan the first correctly.
    """
    film = await _given_title(session, name="Vane Alpha", popularity=1.0)
    await _given_search_name(
        session, title_id=film, name="Vane Ashgrove", kind=SearchNameKind.PERSON
    )

    taken = await _plan(session, _PREFIX, {"pattern": "vane%", "limit": 10})

    assert _TIER_ONE_INDEX in taken, (
        f"the tier-1 statement did not reach {_TIER_ONE_INDEX}; only the `text_pattern_ops` "
        "operator class can serve `LIKE 'pre%'` under a non-C collation"
    )
    assert "ix_title_search_names_name_lower_prefix" in taken
    assert _NEAR_MISS_INDEX not in taken, (
        f"the plan reached {_NEAR_MISS_INDEX}, which is a default-opclass btree on "
        "(lower(name), year) and cannot answer a prefix -- see m09a's docstring"
    )


@pytest.mark.integration
async def test_the_trigram_index_is_still_gin_and_tier_two_still_plans_to_it(
    session: AsyncSession,
) -> None:
    """A GiST trigram index added beside the GIN one -- "add GiST for KNN and
    keep GIN for `%`", which is measured and is not available.

    With both present the planner takes GiST for `%` and the identical shipped
    configuration goes **33.3 ms -> 141.5 ms p50 (4.3x) for byte-identical
    recall**. Tier 1 adds a *btree*, which no `%` plan can take, so this case
    is what says so rather than assumes it.

    **Three assertions, and the first two are not redundant with the third.**
    `pg_indexes` says the index exists and says `USING gin`; a plan assertion
    cannot say either, because **no plan shape distinguishes GIN from GiST for
    `%`** -- GiST serves that operator too, which is exactly why the
    regression this guards against would be invisible to a green suite. The
    third asserts the index the planner actually **takes** for the tier-2
    statement, which is the only half a future GiST index would move.
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
    """Tier 1 ordering on something tier 2 does not, so the box reshuffles
    when the debounced tier arrives behind it.

    Both statements sort `popularity DESC NULLS LAST, vote_count DESC NULLS
    LAST, id ASC`; tier 2 puts edit distance above all three and tier 1 has no
    distance to put there, so on rows both tiers return the orders agree. A
    client that renders tier 1 and then replaces it with tier 2 shows the same
    list in the same order rather than a list that jumps under the cursor.

    The fixture is what makes this a statement about **all three** keys, and
    the seeding order is the load-bearing part of it. Three titles that are
    exact prefix matches for the same query, so distance cannot decide
    anything; popularity present on one and absent on the two below it, so
    `NULLS LAST` decides which end they go to; and the low-vote row **inserted
    before** the high-vote one, so id order and vote-count order disagree.
    Seeded the other way round, an implementation carrying only the popularity
    key answers correctly by accident -- a UUIDv7 primary key makes
    `ORDER BY id` and `ORDER BY vote_count DESC` the same list whenever the
    fixture happens to seed them in agreement.
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
