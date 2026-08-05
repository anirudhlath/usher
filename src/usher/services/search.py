"""PRD 03 stage 4, application side: what gets embedded, and what gets ranked.

Two halves, and they face opposite directions. `compose_document` is the
**write** side -- the text one title embeds as, plus the hash of exactly that
-- and `SearchService` is the **read** side: retrieve candidates through the
port, then rank them here, because PRD 05 separates those two stages
deliberately and ADR-0002 makes the same split from the other direction ("the
engine is a candidate generator"). They share a module because they share the
one decision that ties them together: nobody applies a model prefix, on either
side. The composer applies none to a document, the service applies none to a
query, and ADR-0022 measured why (-0.0028 MRR for the documented prefix on one
side; -0.0663 applied to both).

`services/` may import only `domain/` and `ports/`
([ADR-0009](../../../docs/prd/decisions/0009-repositories-are-ports.md)), which
is exactly right here: composing a document out of a `Title` is a decision about
*meaning* and must not be able to reach a `tsvector`, a `halfvec`, or a model.

**The fingerprint is over the assembled string and nothing else.** It is what
turns "is this vector stale?" into one SQL predicate, and that predicate has
three consumers -- the backfill's cursor, the `usher.search.embeddings.stale`
gauge, and the test that proves the enqueue-on-enrichment path closes. Hash
anything but the exact bytes handed to `Embedder.embed` and all three go on
answering, wrongly.

**This assembly is a second implementation of
`usher.db.repositories.search._FINGERPRINT_SQL`, and it is permitted only
because a test pins the two together.** The predicate cannot call this function
-- the assembly is per-title, so it cannot be a bound parameter, and `db/` may
not import `services/` anyway -- so the fingerprint is spelled once in Python
and once in SQL. `tests/integration/test_search_repository.py` runs both over
the same seeded rows and compares. **Three shapes of the obvious Python
composer are unreproducible in SQL and all three are refused here**, each with
its own case in `tests/unit/test_services_search_document.py`:

1. *Appending a section only when the field is populated.* `_FINGERPRINT_SQL`
   is `coalesce(..., '')` on every nullable column with no conditionals, so it
   emits seven segments for every title. The assembly below is positional for
   that reason: a missing overview is an empty line, never an absent one.
2. *Joining array elements on `", "`.* The predicate uses `usher_array_text`,
   which is `array_to_string($1, ' ')` -- the same `IMMUTABLE` wrapper the
   generated column uses, so this schema has one definition of "an array as
   text" rather than two.
3. *Including `year`.* It is genuinely useful text and the predicate has no
   `year` column, so it is left out. Adding it means adding it to both sides in
   one commit; adding it to one is failure mode (a) above.

**Changing the assembly invalidates every stored vector, on purpose.** Add a
field, change a separator, reorder a section -- every fingerprint moves, every
row matches the stale predicate, and the backfill re-embeds the enriched tier in
the 25 s to 2 min it costs (~8,000-10,700 tokens/s on CPU, ~100-130 tokens a
document). That is the scheme working, not a migration to write. The same
mechanism covers a model swap, which is why `model_name` records the runtime as
well as the checkpoint (`fastembed:BAAI/bge-small-en-v1.5`): the measured
ST-vs-fastembed difference is 6x the halfvec quantisation error, so the two are
not interchangeable without a re-embed.
"""

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass

from opentelemetry import metrics

from usher.domain.search import SearchResult
from usher.domain.title import Title
from usher.ports.embedding import Embedder
from usher.ports.repository import MediaItemRepository, TitleRepository
from usher.ports.search import (
    SearchFilters,
    SearchHit,
    SearchIndex,
    SearchMode,
    SearchRequest,
    SuggestIndex,
)

_meter = metrics.get_meter("usher.search")

# PRD 10's names, byte for byte, and neither is shortened or pluralised by
# analogy with anything. **A metric under a near-miss name is a dashboard
# panel that is permanently empty and nothing distinguishes it from a healthy
# zero** -- PRD 10 says so at the head of its own table, and M4 found three
# instances of it there. The near misses this pair invites are
# `usher.search.result` (singular, matching `usher.enrich.result` two rows up
# that table) and `usher.search.hits`; neither raises, neither fails a case
# asserting "a histogram was recorded".
_search_duration = _meter.create_histogram(
    "usher.search.duration", unit="s", description="Wall time per search, by mode"
)
# A histogram rather than a counter, and the label says why: the useful
# question is the *distribution* of result-set sizes per mode -- how often
# FUSED comes back empty, how often it saturates the limit -- not a running
# total nobody plots. A different instrument type under a documented name is
# the same class of failure as a near-miss name, which is what
# `register_push_gauges` records for its own pair.
_search_results = _meter.create_histogram(
    "usher.search.results", unit="1", description="Results returned per search, by mode"
)

# The two separators, named because they are load-bearing rather than
# cosmetic. `_SECTION` is `_FINGERPRINT_SQL`'s `CHR(10)` and `_ITEM` is
# `usher_array_text`'s `array_to_string($1, ' ')`. A change to either is a
# change to every fingerprint in the catalog.
_SECTION = "\n"
_ITEM = " "


@dataclass(frozen=True, slots=True)
class EmbeddingDocument:
    """One title as an embedder sees it, plus the hash of exactly that.

    **Deliberately not `ports.search.SearchDocument`**, which is a retrieval
    document with weight classes aimed at `index_many`. Sharing a type would
    invite the fingerprint being computed over the weighted form, which is
    the one way to get this wrong that nothing downstream can detect.

    `is_degenerate` is a flag on a fully-formed document, **never an
    absence**. A refused title still gets a `title_embeddings` row carrying
    this `fingerprint` and a `NULL` embedding, so it stops matching the stale
    predicate and starts matching the `embedding IS NULL` one a diagnostic
    counts. Returning `None` here would leave the caller nothing to write and
    the title re-claimed by every backfill pass forever -- the failure this
    repository has already shipped once, one lane over, when the
    watch-history repair carried the walk's instant and was refused by the
    very row it existed to repair.
    """

    text: str
    fingerprint: str
    is_degenerate: bool


def compose_document(title: Title, *, credits: Sequence[str] = ()) -> EmbeddingDocument:
    """The text this title embeds as, and the `md5` of that text.

    Pure: same `Title` in, same bytes out, in any process. Determinism is not
    a nicety -- a non-deterministic assembly makes `source_fingerprint`
    meaningless and the backfill non-terminating. Everything below iterates a
    tuple in the order a provider supplied it; nothing iterates a `set`.

    **The seven segments, their order and their separators are
    `_FINGERPRINT_SQL`'s**, not a choice made here, for the reason the module
    docstring gives. Read that before editing this function.

    **`credits` is weight class B's text and it is `titles.credit_names`, not
    a `Title` field.** `credit_names` is in `DERIVED_COLUMNS` -- it is
    `credits` projected to names and truncated to a ranking constant -- so a
    `Title` cannot supply it and the caller reads it through
    `TitleRepository.credit_names_for`. That read is **site three** of the
    three this document has, and it is the one that gets missed: sites one and
    two can both move correctly and the pair still disagrees on every credited
    title, forever, because `IndexService` was still calling this with the
    default.

    **The seventh segment is unconditional and sits at position three.** The
    M6 shim appended it only `if credits:`, which is the first of the three
    shapes this module's docstring refuses as unreproducible in SQL --
    harmlessly then, because `credits` was always `()` so the branch was never
    taken. Position three matches the generated column's concatenation order,
    so all three spellings read in the same sequence.

    A caller that passes nothing gets an **empty segment**, never an absent
    one: `usher_array_text(ARRAY[]::text[])` is `''` and
    `md5(usher_array_text(ARRAY[]::text[])) = md5('')`, verified on pg17.10,
    so an uncredited title produces the identical string on both sides.
    """
    text = _SECTION.join(
        (
            title.name,
            title.original_name or "",
            _ITEM.join(credits),
            title.overview or "",
            title.tagline or "",
            _ITEM.join(title.genres),
            _ITEM.join(title.keywords),
        )
    )
    return EmbeddingDocument(
        text=text,
        # `usedforsecurity=False` is required, not decorative: ruff's `S`
        # rules flag `hashlib.md5` as S324, and the flag is the honest
        # statement -- this is a content hash for change detection and nothing
        # about it is a security boundary. `md5` because the predicate spells
        # `md5(...)` in SQL, the column is sized for 32 hex characters, and a
        # collision costs one un-refreshed vector.
        fingerprint=hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest(),
        # `not text.strip()`, and nothing more elaborate. Every whitespace-only
        # input embeds to the identical vector (cos = 1.0000 exactly).
        # Measured just as directly: a name-only skeleton is fine -- 0.5867
        # pairwise, 0.7638 self-retrieval against a 0.4751 cross-title mean.
        # The rule is about *empty*, not *thin*; a minimum word count would
        # exclude the majority tier from semantic results while every gauge
        # read zero. `strip()` and not `== ""`: the positional assembly means
        # an empty title is five newlines rather than the empty string.
        is_degenerate=not text.strip(),
    )


class SemanticSearchUnavailable(Exception):
    """This deployment has no `Embedder`, so a semantic query cannot be served.

    **Deliberately not a `UsherPortError`**: that family's docstring says
    "every error a port implementation may raise", and nothing failed here --
    the deployment is configured without a model and said so once, at startup.
    Filed there it would land in the `except UsherPortError` arms that mean "an
    upstream is broken", where the response is a retry and no retry can help.
    **And not a `ValueError`**, which a caller wrapping a search would catch
    alongside every argument failure in the call. M9 gives it a `code` in PRD
    07's vocabulary; M6 invents no status code for a route that does not exist.
    """


# `k / (k + rank)`, and **k is 1 rather than `search_rrf_k`'s 60**. At 60 the
# relevance term is nearly flat across the whole candidate set while popularity
# spans [0, 1), so the blend is popularity-ordered wearing RRF's name --
# ADR-0002's prohibition committed one layer up, where nothing else here would
# catch it. RRF's k has a different job (flattening two *candidate lists*
# against each other) and sharing it would be a number that looks shared and is
# not.
_RELEVANCE_K = 1.0

# Popularity squashed to [0, 1) by `p / (p + midpoint)`: bounded, monotone, and
# -- unlike a min-max over the candidate set -- independent of which other rows
# came back. `log1p` is unbounded (6.9 at p=1000 against 0.69 at p=1) so it
# would need a set-dependent normaliser, which is the same problem again. 10.0
# is **chosen, not measured**; what makes that tolerable is that the term is
# bounded, so a wrong midpoint moves a score by at most its weight.
_POPULARITY_MIDPOINT = 10.0

# PRD 05's ranking terms M6 has data for. Watch state, recency and taste
# centroid are M7's and are *absent* rather than zeroed -- a term with no data
# is a weight that reads like a signal. Relevance dominates because a search is
# a request for a specific thing; the other two are tie-breakers among things
# that already matched. The 0.15 owned boost is bounded on purpose: against
# `1 / (1 + rank)` at 0.70 it cannot displace the rank-0 hit (0.70 against
# 0.35 + 0.15) and moves a title roughly five positions mid-list -- PRD 05's
# "boosted but not exclusive" as arithmetic rather than as a promise.
_WEIGHTS: dict[str, float] = {"relevance": 0.70, "popularity": 0.15, "owned": 0.15}


@dataclass(frozen=True, slots=True)
class SearchAnswer:
    """Ranked results, plus what actually ran.

    Lives here rather than in `usher.domain.search` because it carries a
    `SearchMode`, which is a port type, and `domain/` imports nothing. The
    precedent is `TitleDetail` beside `TitleReadService`.

    **`requested_mode` beside `mode` is the degradation made visible.** A
    `FUSED` request on a deployment with no embedder is served as full-text and
    every row of that answer is correct -- so without these two fields the only
    signal is `semantic_coverage == 0.0`, which is *also* what a healthy FUSED
    search over a catalog with no embeddings reports. Two different problems
    with two different fixes (install the extra; run `usher index`) that would
    otherwise present identically.
    """

    results: tuple[SearchResult, ...] = ()
    requested_mode: SearchMode = SearchMode.FULL_TEXT
    mode: SearchMode = SearchMode.FULL_TEXT
    semantic_coverage: float = 0.0

    @property
    def degraded(self) -> bool:
        """The request was served in a narrower mode than it asked for."""
        return self.mode is not self.requested_mode


class SearchService:
    """PRD 05's two stages, in order: retrieve, then rank.

    **Holds the `Embedder`, and the port DTO makes that structural.**
    `SearchRequest.__post_init__` refuses a `SEMANTIC` or `FUSED` request with
    no `query_vector`, so the only object that can construct one is the object
    holding the model. That is why the method below takes primitives: the port
    DTO is this service's *output* to the index, never its input from a caller.

    **Applies no instruction prefix, ever** (ADR-0022). This checkpoint needs
    none: the documented BGE query prefix moves MRR -0.0028 and applying it to
    both sides is -0.0663, against a power control of -0.2497.

    `result_limit` rather than a `Settings`: `services/` may import only
    `domain/` and `ports/` (ADR-0009). `composition.build_pipeline` passes
    `settings.search_result_limit`, which is also what satisfies
    `test_every_setting_is_read_by_something`.
    """

    def __init__(
        self,
        index: SearchIndex,
        suggestions: SuggestIndex,
        titles: TitleRepository,
        media_items: MediaItemRepository,
        *,
        result_limit: int,
        embedder: Embedder | None = None,
    ) -> None:
        self._index = index
        self._suggestions = suggestions
        self._titles = titles
        self._media_items = media_items
        self._result_limit = result_limit
        # Optional, and a deployment without it still has search: full-text and
        # trigram are PRD 05's catalog-lookup tier and serve all 1,271,138
        # titles with no model at all.
        self._embedder = embedder

    async def search(
        self,
        query: str,
        *,
        mode: SearchMode = SearchMode.FULL_TEXT,
        limit: int = 20,
        # `SearchFilters()` as a default would be ruff B008 (a call in a
        # default) even though the value is frozen. The sentinel is the
        # spelling, not the reason.
        filters: SearchFilters | None = None,
    ) -> SearchAnswer:
        """Retrieve, then rank. Raises `SemanticSearchUnavailable`.

        **PRD 10's two search series are recorded here, labelled with the mode
        that *ran*.** A `FUSED` request on a deployment with no embedder is
        served as full-text, and labelling it `fused` would attribute
        full-text latency and full-text result counts to a lane that did not
        run -- ADR-0002's "never a confident blended score that is really one
        lane", arriving in the panel an operator would use to check for it.
        The degradation is carried by `SearchAnswer.requested_mode`, which is
        what `usher search` prints; PRD 10 documents one label and this is it.
        """
        requested = mode
        # Refused before the model, not after. Every whitespace-only input
        # embeds to the identical vector at cos 1.0000 exactly, so a blank
        # semantic query would return a confident list of whatever sits nearest
        # a degenerate point -- `compose_document`'s trap, on the query side.
        # Empty rather than a raise: a search box sends this between keystrokes.
        #
        # **Deliberately before the measurement**, so a blank query is not a
        # data point. A keystroke-driven client sends one between every
        # character, and counted they would dominate both histograms and turn
        # dashboard 1's search latency into a measure of how fast this
        # declines. The series is about retrieval.
        if not query.strip():
            return SearchAnswer(requested_mode=requested, mode=requested)

        started = time.perf_counter()
        vector: tuple[float, ...] | None = None
        if mode is not SearchMode.FULL_TEXT:
            if self._embedder is None:
                if mode is SearchMode.SEMANTIC:
                    # Narrowing this to full-text is not narrowing. The caller
                    # asked the one question full-text cannot answer and would
                    # get a plausible answer to a different one.
                    raise SemanticSearchUnavailable(
                        "semantic search needs an embedding model; this deployment has none"
                    )
                # FUSED, on the other hand, still has a whole lane left, and
                # PRD 08 says a degraded subsystem narrows rather than fails.
                # The narrowing is carried in the answer, not hidden in it.
                mode = SearchMode.FULL_TEXT
            else:
                vector = tuple((await self._embedder.embed([query]))[0])

        outcome = await self._index.search(
            SearchRequest(
                query=query,
                # A ceiling, not a default: every candidate becomes a hydrated
                # row in application code, so an unclamped limit is a scan.
                limit=min(limit, self._result_limit),
                mode=mode,
                filters=filters or SearchFilters(),
                query_vector=vector,
            )
        )
        answer = SearchAnswer(
            results=await self._rank(outcome.hits),
            requested_mode=requested,
            mode=mode,
            # Passed through, never recomputed. It is the fraction of the
            # *filtered population* that had a vector; derived from the hits it
            # would read 1.0 whenever every returned hit had one, which is
            # exactly the case a green test seeds.
            semantic_coverage=outcome.semantic_coverage,
        )
        # After the rank, not around the retrieval alone: PRD 05 splits the two
        # stages and an operator asking "why is search slow" is asking about
        # the answer, not about half of it.
        labels = {"mode": mode.value}
        _search_duration.record(time.perf_counter() - started, labels)
        _search_results.record(len(answer.results), labels)
        return answer

    async def suggest(self, prefix: str, limit: int = 10) -> tuple[SearchResult, ...]:
        """Type-ahead candidates, hydrated and **not re-ranked**.

        `PostgresSuggestIndex` already ordered by edit distance and then by
        popularity inside its capped candidate set. Applying the search blend
        here would count popularity twice -- once inside the cap and once
        outside it -- and reorder the box away from the ordering the narrow
        path exists to produce. Which is also the practical half of why
        `SuggestIndex` is its own port.
        """
        if not prefix.strip():
            return ()
        hits = await self._suggestions.suggest(prefix, limit=min(limit, self._result_limit))
        by_id = {
            title.id: title
            for title in await self._titles.list_by_ids([hit.title_id for hit in hits])
        }
        owned = await self._media_items.owned_title_ids(list(by_id))
        return tuple(
            _result(by_id[hit.title_id], owned=hit.title_id in owned, score=hit.score)
            for hit in hits
            if hit.title_id in by_id
        )

    async def _rank(self, hits: Sequence[SearchHit]) -> tuple[SearchResult, ...]:
        """PRD 05 stage 2, over one already-retrieved candidate set.

        Two reads regardless of hit count, which is the whole reason
        `list_by_ids` and `owned_title_ids` exist.
        """
        if not hits:
            return ()
        titles = {
            title.id: title
            for title in await self._titles.list_by_ids([hit.title_id for hit in hits])
        }
        owned = await self._media_items.owned_title_ids(list(titles))
        ranks = _dense_ranks(hits)
        results = [
            _result(
                titles[hit.title_id],
                owned=hit.title_id in owned,
                score=_blend(
                    relevance=_RELEVANCE_K / (_RELEVANCE_K + rank),
                    popularity=_popularity_term(titles[hit.title_id].popularity),
                    owned=1.0 if hit.title_id in owned else 0.0,
                ),
            )
            for hit, rank in zip(hits, ranks, strict=True)
            # Dropped, not raised: a title deleted between the index write and
            # this read is ordinary, and `titles[hit.title_id]` is a KeyError
            # -- a 500 on a search because one row went away.
            if hit.title_id in titles
        ]
        # Ties broken by id. Falling back to the index's own order is not an
        # order: this repository has measured `UPDATE ... RETURNING` handing
        # rows back in heap order on a small table, and a search that reorders
        # equal-scoring rows between two identical calls cannot be paginated.
        results.sort(key=lambda result: (-result.score, result.title_id))
        return tuple(results)


def _dense_ranks(hits: Sequence[SearchHit]) -> list[int]:
    """Positions, with equal index scores sharing a position.

    **Dense rather than strict, and the difference is load-bearing.** Under a
    strict positional rank no two candidates ever tie on relevance, so the
    owned boost could only ever be a tie-break that no case could construct --
    `test_an_owned_title_outranks_an_unowned_one_at_equal_relevance` would be
    unassertable and a boost of zero would pass the suite.

    Scores are compared for **equality only**, never for magnitude, so this is
    indifferent to whether a backend reports a similarity or a distance. One
    fewer cross-backend convention to get right, and a reason to prefer a
    rank-derived relevance term over the obvious score-derived one.
    """
    ranks: list[int] = []
    rank = 0
    previous: float | None = None
    for hit in hits:
        if previous is not None and hit.score != previous:
            rank += 1
        ranks.append(rank)
        previous = hit.score
    return ranks


def _popularity_term(popularity: float | None) -> float | None:
    """`p / (p + midpoint)`, or `None` when nobody has measured it.

    **`None` is not 0.0** -- ADR-0014, in a fourth place. `titles.popularity`
    is null for every title TMDb's daily export has never described: **all**
    of a `--phase imdb` catalog and **~77%** of a `--phase all` one (Task 36
    measured 291,584 of 1,271,570 titles carrying a popularity, 2026-08-05).
    `popularity or 0.0` would rank a title nobody measured identically to one
    measured as unpopular, burying the whole un-enriched catalog beneath the
    enriched tier while looking like arithmetic and raising nothing. `_blend`
    below was re-checked against the populated catalog and is unchanged: it
    drops an absent signal from numerator and denominator, so a partially
    populated catalog scores each title on what is known about it, not on a
    zero it never measured.
    """
    if popularity is None:
        return None
    return popularity / (popularity + _POPULARITY_MIDPOINT)


def _blend(**signals: float | None) -> float:
    """A weighted mean over the signals that are actually present.

    An absent signal leaves **both** the numerator and the denominator, so a
    title with no popularity is scored on what is known about it rather than
    penalised for what is not. The observable consequence: at equal relevance,
    unknown popularity ranks above a measured zero.

    Written as a sum over an explicit signal list -- the same skeleton
    `SimilarityService` uses -- so that landing watch state, recency or a taste
    centroid in M7 is adding a term and a weight in both places rather than
    rewriting two scorers.
    """
    total = 0.0
    applied = 0.0
    for name, value in signals.items():
        if value is None:
            continue
        total += _WEIGHTS[name] * value
        applied += _WEIGHTS[name]
    return total / applied if applied else 0.0


def _result(title: Title, *, owned: bool, score: float) -> SearchResult:
    return SearchResult(
        title_id=title.id,
        kind=title.kind,
        name=title.name,
        year=title.year,
        popularity=title.popularity,
        owned=owned,
        score=score,
    )


__all__ = [
    "EmbeddingDocument",
    "SearchAnswer",
    "SearchService",
    "SemanticSearchUnavailable",
    "compose_document",
]
