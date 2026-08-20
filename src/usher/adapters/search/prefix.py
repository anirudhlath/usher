"""Tier 1 of the two-tier suggest: a btree prefix probe over names.

**This module exists because a gate failed.** ADR-0002 wrote a typo-tolerance
bar down before the numbers were known -- recall@5 >= 0.75 on 2-4-character
names, >= 0.85 on 5-7, and p95 <= 50 ms -- and ran it on 2026-08-03 against
1,271,138 real catalog names over 2,993 single-edit typo cases. It **failed
both halves**: the shipped trigram type-ahead scores **27.8%** on the 2-4 band
and **68.3%** on 5-7, transposition on a short name is **0.0%**, and **no**
configuration measured -- no threshold, no cap, neither index type -- comes
within 6x of the keystroke budget. Above 8 characters it is 95-100% and needs
nothing, which is 91% of that catalog by row count.

The one configuration that fits a keystroke is the btree
`lower(name) text_pattern_ops` prefix probe: **p50 0.6 ms, p95 1.0 ms, max
10 ms, 44 MB, 0.559 s to build** over those same 1,271,138 rows -- 200-330x
faster than any fuzzy configuration -- with **no typo tolerance at all
(1.9%)**. That is not a shortcoming to be tuned away, it is the division of
labour: this tier answers every keystroke and the trigram +
`levenshtein_less_equal` path is debounced behind it. They are complements,
and neither is a replacement for the other. Full result table in
`.claude/rules/search-and-embeddings.md`.

**A second implementation of `SuggestIndex`, which `ports/search.py` named in
advance** -- *"The day a second implementation needs them is the day that cost
becomes real and gets paid for on purpose."* It needs no write method either:
like `PostgresSuggestIndex` it reads tables somebody else owns and maintains
no artefact, so ADR-0021's dual-write cost stays unpaid.

**Its own module rather than a class added to `postgres.py`, and that is a
decision.** The tier-2 statement, its trigram floor and its distance ceiling
each carry a real-catalog measurement, and this task adds an implementation
rather than editing one -- a promise `git diff --stat` can settle only if the
file is untouched. Group F edits `_COVERAGE` in that same file concurrently,
so a new module also removes a collision.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.ports.search import SearchHit, SuggestIndex

# `LIKE`'s three metacharacters, and the escape they are prefixed with.
# Postgres reads a backslash as `LIKE`'s escape character by default,
# independently of `standard_conforming_strings` -- that GUC governs string
# *literals*, and every pattern here crosses as a bind parameter.
#
# The order matters and is not stylistic: the backslash has to be doubled
# *first*, or the escapes introduced for `%` and `_` are themselves escaped on
# the next pass and the pattern matches a literal backslash instead.
_LIKE_ESCAPE = "\\"
_LIKE_SPECIALS = (_LIKE_ESCAPE, "%", "_")

# **Both tiers order their answers the same way, minus the key tier 1 does not
# have.** Tier 2 sorts `dist ASC, tmdb_popularity DESC NULLS LAST,
# tmdb_vote_count DESC NULLS LAST, id ASC`; every row here is an exact prefix
# match, so there is no distance to lead with and the remaining three are
# identical. A client that paints tier 1 and then replaces it with tier 2 sees
# the same list rather than one that jumps under the cursor.
#
# `NULLS LAST` on both keys because a descending sort puts NULLs first by
# default, which would hand the box to whichever skeleton the scan reached
# first. Measured on a `--phase all` catalog of 1,271,570 titles: **291,584
# (22.9%) carry a popularity**, so on the other ~77% this degenerates to
# `tmdb_vote_count DESC, id ASC`, and that column -- written by the bootstrap
# on 539,350 rows -- is what orders them. The id makes the order total, so a
# tie cannot come back differently on two runs.
#
# ⚠️ **That 539,350 is dated 2026-08-05 and ADR-0040's Task 2 moved the writer
# it names (2026-08-19).** `apply_ratings` now fills `imdb_num_votes`, so
# nothing but TMDb enrichment reaches `tmdb_vote_count` and a bootstrap-only
# catalog leaves it NULL on **every** row -- at which point, on the rows where
# `tmdb_popularity` is absent too (all of a `--phase imdb` catalog, ~77% of a
# `--phase all` one), this `ORDER BY` is `id ASC` alone: insertion order over a
# UUIDv7 key, which is the state ADR-0002 measured costing 4.2 points of
# recall@5 overall and 8.3 on the 2-4-character band. The measurement
# stands for the catalog it was taken on. **Not repaired here**: pointing the
# key at `imdb_num_votes` is a ranking change owing its own measurement, and it
# is issue #39. `adapters/search/postgres.py` carries the long form of this
# note, and `ports/repository/title.py` the third copy.
#
# **The union reads `titles` and `title_search_names` as one set, so a
# director's name reaches their films from the first keystroke.** That is the
# second of the two things PRD 05 says the narrow table exists for, and the arm
# was built while `m09a` still shipped that table with no writer in `src/` at
# all: a tier that has to be re-plumbed the day a loader lands is a tier whose
# union was never measured.
#
# **The `person` half has a writer now** -- `CreditRepository.replace_for_titles`
# writes it from the same mapping it writes `titles.credit_names` from, so on a
# derived catalog this arm answers over real rows rather than over an empty
# relation. The `alias` half is still owed, and a catalog derived before that
# writer landed holds nothing here until `usher derive` re-runs. **B3 is the
# task authorised to narrow this**, on a measurement rather than on a reading
# of the SQL.
#
# `UNION`, not `UNION ALL`: a film whose canonical name and whose alias both
# start with the typed prefix is one row of the box. The arms project one
# column so the de-duplication is on `title_id` exactly, and the ordering keys
# are joined back afterwards -- rank on a narrow projection, then join the
# entity back, which is the shape `list_unwatched_candidates` was rewritten
# into for the same reason.
#
# **No inner per-arm cap, deliberately.** An unordered `LIMIT` inside an arm
# would truncate arbitrarily -- the defect that makes a *lower* trigram floor
# score *worse* recall (66.2% -> 48.5% -> 2.6%, measured) -- and an *ordered*
# one costs the same sort as the outer statement, so it buys nothing. The
# residual exposure is real and is named rather than hidden: a one-character
# keystroke over a 1.27M-row catalog matches a large set and Postgres has no
# `LIMIT` pushdown through a sort, so the 0.6 ms figure above is a probe
# measurement and not an end-to-end one. **That is exactly what B3 measures**,
# against a bar written before the run.
_PREFIX = """
WITH matched AS (
    SELECT t.id AS title_id
    FROM titles AS t
    WHERE lower(t.name) LIKE :pattern
    UNION
    SELECT n.title_id
    FROM title_search_names AS n
    WHERE lower(n.name) LIKE :pattern
)
SELECT m.title_id AS id
FROM matched AS m
JOIN titles AS t ON t.id = m.title_id
ORDER BY t.tmdb_popularity DESC NULLS LAST, t.tmdb_vote_count DESC NULLS LAST, m.title_id ASC
LIMIT :limit
"""


def _pattern(prefix: str) -> str:
    """`prefix` as a `LIKE` pattern anchored at the start of the name.

    **Escaped, because `%` and `_` are two keys on the keyboard of a box that
    runs a query per keystroke.** Unescaped, a typed `%` is `LIKE '%%'` -- the
    whole catalog collected, de-duplicated and sorted to answer a keystroke --
    and `_` matches every single-character name. Both are ordinary characters
    in a film title, so refusing them is not an option either.

    Escaping leaves the index usable: PostgreSQL extracts a prefix from a
    `LIKE` pattern by stopping at the first *unescaped* metacharacter, so an
    escaped one is part of the literal prefix and the range condition simply
    starts one character later.
    """
    for special in _LIKE_SPECIALS:
        prefix = prefix.replace(special, _LIKE_ESCAPE + special)
    return f"{prefix}%"


class PostgresPrefixSuggestIndex(SuggestIndex):
    """Prefix-only type-ahead over `titles.name` and `title_search_names.name`.
    **Writes nothing.**

    **It has no caller in `src/` yet, and that is deliberate rather than
    dead.** B5 wires it into `SearchService` and `composition.build_pipeline`
    behind the route that serves both tiers; B3 measures it at catalog scale
    against a bar written before the run. Deleting it as unreachable is the
    reading this paragraph exists to prevent.

    **The index it needs is `ix_titles_name_lower_prefix`, and the index that
    looks like it is `ix_titles_name_lower_year`.** The second is a plain btree
    on `(lower(name), year)` carrying the *default* operator class, which under
    this database's collation cannot answer `LIKE 'pre%'` at all: measured on
    `pgvector/pgvector:pg17` at the pre-`m09a` schema, the plan is `Seq Scan on
    titles` at cost 1e10 even with `enable_seqscan = off` -- not merely
    not-chosen, **not choosable**. `m09a` adds the `text_pattern_ops` pair, one
    per table, and with them the same query is `Index Cond: ((lower(name) ~>=~
    'pre') AND (lower(name) ~<~ 'prf'))`.

    **Nothing here touches the trigram index and nothing here may.** Tier 2's
    GIN index stays exactly as M6 shipped it: with a GiST trigram index present
    beside it the planner takes GiST for `%` and the identical configuration
    goes 33.3 ms -> 141.5 ms p50 for byte-identical recall. A btree is safe
    precisely because no `%` plan can take one.

    **`lower()` on both sides, and it is Postgres's `lower()` rather than
    Python's.** `FakeSuggestIndex` casefolds in Python, which agrees with this
    on ASCII and diverges on the handful of code points where the two
    algorithms differ -- that divergence is recorded in the fake, and no case
    in this repository tests a non-Latin name.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def suggest(self, prefix: str, limit: int = 10) -> list[SearchHit]:
        # An empty box is the state of every page load and of every backspace
        # to zero, and `LIKE '%'` over 1.27M rows is a whole-catalog sort for a
        # question nobody asked. The trigram tier answers nothing here too, by
        # accident of `similarity(name, '') = 0`; this is the same answer
        # arrived at on purpose. Whitespace is included because a space bar is
        # a keystroke.
        if not prefix.strip():
            return []
        rows = await self._session.execute(
            text(_PREFIX),
            {"pattern": _pattern(prefix.lower()), "limit": max(limit, 0)},
        )
        # **Every hit scores 1.0, and that is the honest number rather than a
        # placeholder.** `SearchHit.score` is a rank-shaped value for a caller
        # to render, and on this tier every row is an exact prefix match -- the
        # distance tier 2 varies its score with is zero for all of them. The
        # *ordering* is the database's, and nothing here re-sorts it: a Python
        # re-sort would silently drop the two `NULLS LAST` clauses and the id
        # tiebreak the statement is careful about.
        return [SearchHit(title_id=row.id, score=1.0) for row in rows]


__all__ = ["PostgresPrefixSuggestIndex"]
