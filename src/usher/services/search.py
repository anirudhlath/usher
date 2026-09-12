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
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from loguru import logger
from opentelemetry import metrics
from pydantic import AwareDatetime

from usher.domain.ids import new_id
from usher.domain.search import SearchResult
from usher.domain.title import Title
from usher.ports.embedding import Embedder
from usher.ports.errors import UsherPortError
from usher.ports.repository import (
    MediaItemRepository,
    SearchQueryRecord,
    SearchQueryRepository,
    TasteRepository,
    TitleEmbeddingRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.search import (
    SearchFilters,
    SearchHit,
    SearchIndex,
    SearchMode,
    SearchRequest,
    SuggestIndex,
    SuggestTier,
)
from usher.services.query_expansion import QueryExpansionService

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

# **The two-tier suggest boundary is a bucket problem before it is a naming
# problem**, and this is the only histogram in the project that says so.
# `configure_metrics` installs no `View`, so every other seconds-unit
# instrument here takes the SDK's default explicit boundaries -- `(0.0, 5.0,
# 10.0, 25.0, ...)`, in seconds -- and every observation under five seconds
# falls in the first of them. Measured rather than reasoned, 2026-09-07: 2,000
# points (seed 20260907) drawn on ADR-0002's shipped tier-2 shape and exported
# over real OTLP into this host's collector and Prometheus came back as
# `histogram_quantile(0.5, ...)` = **2.5000 s** and `(0.95, ...)` = **4.75 s**
# against the sample's own **35.20 ms** and **225.07 ms** -- 71x and 21x wrong
# -- against **38.12 ms** and **240.70 ms** on the ladder below. PRD 10 carries
# the run. A keystroke path is where the default stops being a loss of
# resolution and becomes a loss of the measurement.
#
# **An instrument advisory rather than a `View`, and the choice is a scope
# rather than a preference.** The repo-wide fix PRD 10 asks for is every
# seconds-unit histogram at once and is owned there; an advisory travels with
# the instrument, so it holds under whichever `MeterProvider` a caller
# installed -- including the `InMemoryMetricReader` every unit case installs,
# which is what makes it assertable at all.
#
# **The ladder spans both tiers by construction**, because one histogram over
# two populations is what the `tier` label exists to refuse: ADR-0002's
# shipped-configuration row measures tier 2's whole-name p50/p95/max at
# 33.6/211/730 ms, and ADR-0031's p95-by-prefix-length table measures tier 1's
# shipped union from 2,707 ms at one character to 2.3 ms at seven. `0.05` is a
# boundary exactly rather than incidentally -- ADR-0002's as-you-type budget
# is 50 ms, so the fraction of keystrokes inside it is a bucket ratio and
# never an interpolation between two bounds.
_SUGGEST_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

# PRD 10's names for the type-ahead path, byte for byte and on the same terms
# as the pair above. The near misses this pair invites are
# `usher.suggest.latency` (by analogy with `usher.enrichment.latency`, which is
# a real row of that table), `usher.search.suggest.duration` -- which would put
# the type-ahead box inside every `usher.search.*` panel, the exact objection
# PRD 10 raises against reusing `SearchMode` for a tier (ADR-0031 argues the
# split and never mentions `SearchMode`) -- and `usher.suggest.result`
# singular.
#
# **`tier` is a label rather than two instrument names**, for the reason
# `usher.row.build.duration` carries `provider`: a group-by is one query and
# two names are two panels that cannot be summed. Its vocabulary is
# `SuggestTier`'s own two lower-case members, `prefix` and `fuzzy`, and it is
# written into PRD 10 beside `mode`'s because a label whose vocabulary is
# undocumented is a label two call sites spell differently.
_suggest_duration = _meter.create_histogram(
    "usher.suggest.duration",
    unit="s",
    description="Wall time per answered keystroke, by suggest tier",
    explicit_bucket_boundaries_advisory=_SUGGEST_BUCKETS,
)
# **Results and not hits**, which is a distinction this path has and `search`
# does not: `suggest` drops a hit whose title `list_by_ids` did not return, so
# the two numbers differ exactly when the index and the catalog disagree. The
# hydrated count is the one a client painted, so it is the one plotted.
_suggest_results = _meter.create_histogram(
    "usher.suggest.results", unit="1", description="Results returned per keystroke, by tier"
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


# `SuggestTier` lived here until `m10c` and now lives in
# `usher.ports.search`, beside `SearchMode`. It moved because
# `search_queries.tier` made a port method take one -- as a record of which
# implementation ran, never as an instruction -- and a `SearchQueryRecord`
# field typed on a services-layer enum would make `usher.ports` import
# `usher.services`, which contract 1 reports BROKEN. **No alias is left here
# on purpose**: a second name for one vocabulary is the
# two-vocabularies-under-one-column hazard, one layer up. `mypy` is strict
# with `no_implicit_reexport`, so a re-export would have had to be deliberate
# anyway.


@dataclass(frozen=True, slots=True)
class SearchAnalytics:
    """`search_queries`' retrieval half: the repository, and the commit that
    makes what it wrote durable.

    **One collaborator rather than two parameters, and the pairing is the
    point.** A `SearchQueryRepository` on its own is a write nobody sees --
    every repository in this project flushes and never commits, and a search
    writes nothing else, so an uncommitted row is rolled back when the read's
    session closes and the search is recorded nowhere.
    `api/deps.py:get_session` happens to commit when a handler returns, but
    `cli._session_for` yields a session and disposes the engine **without ever
    committing**, so on the CLI path the row would be lost and nothing would
    say so. Spelled as two optional parameters, "repository without commit" is
    a state a caller can construct and a guard has to have two arms; spelled as
    one frozen pair it is unreachable.

    **`commit` is a callable and not a session** because `services/` may depend
    only on `domain/` and `ports/` (ADR-0009) — verbatim the reason
    `QueryExpansionService` already has one, and the sweep that makes it worth
    a case rather than a convention is recorded there: **a deleted `commit()`
    survived 42 cases.**
    """

    queries: SearchQueryRepository
    commit: Callable[[], Awaitable[None]]


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

# Age squashed to (0, 1] by `1 / (1 + age / midpoint)` -- `_popularity_term`'s
# shape exactly, and for its reasons: bounded, monotone, and independent of
# which other rows came back, so a wrong constant moves a score by at most its
# weight rather than reshuffling a list.
#
# **25 years is chosen with an argument, not measured**, in the same words
# `_POPULARITY_MIDPOINT`'s comment uses one constant up. The argument is about
# where the curve should be steepest: at 25 the term separates "this decade"
# from "the films your parents saw", which is a distinction a viewer would
# recognise, where a small constant separates last year from three years ago,
# which is a distinction nothing in this project has evidence for.
#
# **And PRD 05's double-counting caveat is recorded here rather than
# resolved**: TMDb's `popularity` is a rolling engagement figure that already
# leans recent, so this term and `_popularity_term` are not independent. What
# would settle both the constant and the overlap is `search_queries` (PRD 10),
# and that table has no rows until after M9 ships -- so this ships as a term
# M10 re-measures, which is a smaller cost than PRD 05's "three ranking terms"
# being two.
_RECENCY_MIDPOINT_YEARS = 25.0

# Julian years, so a leap year is not a discontinuity in an age. The term is
# monotone in days and nothing downstream reads the age itself, so the third
# decimal place of this divisor cannot reach a result.
_DAYS_IN_YEAR = 365.25

# PRD 05's six ranking terms, all of them, as of M9.
#
# Relevance dominates because a search is a request for a specific thing; the
# other five are tie-breakers among things that already matched. **The
# arithmetic bound, stated over all six rather than one at a time:** the rank-0
# hit scores `0.70` with every other signal against it, and a rank-1 hit with
# every other signal maximally for it scores `0.35 + 0.15 + 0.15 + 0.02 + 0.02
# + 0.005 = 0.695` -- the denominators are equal because the present-signal set
# is the same -- so no combination of ownership, popularity, watch state,
# recency and taste can displace an exact match. That is PRD 05's "boosted but
# not exclusive" as arithmetic rather than as a promise, and it is the
# constraint every number here is chosen under: the non-relevance weights must
# sum **strictly** below half the relevance weight.
#
# **The three M6 weights keep their exact values, and that is a requirement
# rather than inertia.** `_blend` renormalises over the signals that are
# present, so it is scale-invariant: changing the *ratio* of relevance to
# popularity to owned would move every score this project has ever computed,
# while re-scaling all three together would move none of them. Holding the
# ratio is what makes "a hit with no popularity, no year and no household
# scores exactly what M6 scored it" true, which is the one thing a client
# upgrading across this commit can check. **F5 kept it**: the alternatives were
# to take weight from popularity or owned, or to raise relevance's share, and
# both end that claim to buy a larger weight for the *weakest-evidenced* term
# in the table.
#
# **Both M9 watch/recency weights are 0.02 and they are equal on purpose.**
# Each rests on an argument rather than on a measurement -- the direction of
# the watch-state term, the value of the recency midpoint -- so the weight is
# the bound on how wrong the argument can make a score. At 0.02 the played
# boost moves a title from about rank 10 to about rank 7 mid-list, which is a
# nudge; the owned boost at 0.15 moves the same title to about rank 2, which is
# the difference the two are meant to have.
#
# 🔴 **`taste` is 0.005 and the ceiling is *open*, which is measured rather
# than argued.** The remaining headroom is 0.35 - 0.34 = 0.01, and this comment
# said so -- but 0.01 is the bound, not a value available under it. Taken
# exactly, the challenger's numerator is `0.35 + 0.15 + 0.15 + 0.02 + 0.02 +
# 0.01`, which in IEEE-754 doubles is **0.7000000000000001** against the exact
# match's **0.7**: one ulp *above*, so the rank-1 hit with every signal
# maximally for it sorts **first** and the property this whole paragraph
# protects fails. Not a tie broken by id -- an inversion, and one that no
# ordering case away from that exact configuration can see.
#
# So the usable interval is the **open** `(0, 0.01)`: 0 excluded because a
# zero-weighted term is the "weight that reads like a signal" this table
# refuses, 0.01 excluded by the arithmetic above. Nothing measured
# distinguishes any point inside it, and 0.005 is the midpoint, which is the
# only choice in that interval that does not import a preference nobody has.
# ⚠️ This comment justified that with *"`title_embeddings` holds 0 rows on both
# surviving catalogs, so the effect size is unmeasurable today"* -- true on
# 2026-08-11 and false from 2026-08-12, when S4 embedded 130,647 titles
# (132,440 as of 2026-08-19). The effect size is **measurable and still
# unmeasured**, which is a different claim and a smaller excuse.
# It leaves 0.005 of headroom, so a seventh term is still expressible.
#
# **What 0.005 can actually move, stated rather than implied.** With all six
# present the denominator is 1.045, so the term spans 0.0048 of score. It
# cannot overturn `owned` (0.15) or `played` (0.02) at any cosine gap, and it
# overturns one step of relevance only where `1/(1+k) - 1/(2+k)` falls below
# it: `0.005*dcos > 0.70/((1+k)(2+k))` needs **k >= 11** even at the impossible
# `dcos = 1.0`, and k >= 25 at a realistic 0.2. Where it decides is where every
# other term has already tied -- and `_dense_ranks` makes that the ordinary
# case rather than a corner, because equal index scores share a rank and the
# relevance term then cancels exactly. It is a tie-break, and the weight says
# so honestly: the magnitude was chosen by a full table, not by a measurement
# of what taste proximity is worth.
_WEIGHTS: dict[str, float] = {
    "relevance": 0.70,
    "popularity": 0.15,
    "owned": 0.15,
    "played": 0.02,
    "recency": 0.02,
    "taste": 0.005,
}


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

    **`expanded_query` is the substitution made visible**, and it is the same
    argument one field over. When an LLM rewrote the query, this is exactly the
    text the semantic lane embedded; `None` means the vector came from what the
    caller passed. Without it a viewer searches for one thing and gets results
    for another with nothing to say so -- and cannot tell a good expansion from
    a bad one, which is also the first thing an operator reading their bug
    report needs. It is `None` on every path that embedded the query **as
    typed**: the shipped default with expansion off, a `full_text` search, a
    blank query, a deployment with no embedder, **a filtered population with no
    vectors for the lane to rank** (#16), and an expansion that failed or came
    back unusable.

    **That last item bought a completion, which is why the framing is "embedded
    as typed" rather than "bought no completion".** A call that answered with
    the wrong key is billed in full -- real tokens, a real cost, one `llm_calls`
    row with `ok = false` -- and still leaves this field `None`. So the
    implication runs one way only: a populated `expanded_query` means a
    completion was bought, and an absent one means nothing about spend.

    **`search_id` is the `search_queries` row this answer was recorded as,
    and it is what makes the outcome half of that table fillable at all**
    (F3). A client hands it back on `GET /titles/{id}?search_id=…` to say
    which result it opened and on `POST /titles/{id}/play` to say it played
    one; nothing else may be read from it, which is why it is described on
    the wire as opaque.

    It is `None` on every search that wrote no row, and the three ways that
    happens are the three PRD 10 enumerates: a blank query (refused before
    the measurement), a search with no household, and a deployment whose
    `SearchService` was built with no `SearchAnalytics`. **`None` is also
    what a refused write leaves**, because a row that was not stored has no
    id worth handing out -- see `_record_search`.
    """

    results: tuple[SearchResult, ...] = ()
    requested_mode: SearchMode = SearchMode.FULL_TEXT
    mode: SearchMode = SearchMode.FULL_TEXT
    semantic_coverage: float = 0.0
    expanded_query: str | None = None
    search_id: uuid.UUID | None = None

    @property
    def degraded(self) -> bool:
        """The request was served in a narrower mode than it asked for."""
        return self.mode is not self.requested_mode


class SearchService:
    """PRD 05's two stages, in order: retrieve, then rank.

    **`search` takes a household, and that is a keyword rather than a
    `SearchFilters` field.** PRD 05 keeps `SearchFilters` a closed vocabulary
    with no user field, and the practical consequence is the one that matters:
    every filter this service has is reachable from a query string, so a user
    field there would let any caller name any household. It is also not a
    filter -- it narrows nothing and changes no candidate set; it changes how
    the same set is ordered.

    **The same query can now answer differently for two people, and nothing on
    the wire says so** -- deliberately, and it is not the omission
    `requested_mode` beside `mode` exists to prevent. A degraded mode is a
    *deployment* state a client cannot otherwise see: two clients of the same
    server get the same narrowing and neither can tell. A household is not a
    state at all here -- both shipped callers resolve one before they search
    (`GET /search` through `DefaultUserIdDep`, `usher search` through
    `ensure_default_user`), so there is no reachable request that is
    unpersonalised and therefore no difference for a field to report. The day
    authentication makes "search as nobody" reachable, that changes, and the
    field to add then is one saying which household answered.

    **Holds the `Embedder`, and the port DTO makes that structural.**
    `SearchRequest.__post_init__` refuses a `SEMANTIC` or `FUSED` request with
    no `query_vector`, so the only object that can construct one is the object
    holding the model. That is why the method below takes primitives: the port
    DTO is this service's *output* to the index, never its input from a caller.

    **Applies no instruction prefix, ever** (ADR-0022). This checkpoint needs
    none: the documented BGE query prefix moves MRR -0.0028 and applying it to
    both sides is -0.0663, against a power control of -0.2497. **An LLM
    rewrite is not a prefix and is not covered by that measurement** -- a
    prefix is a fixed string this project prepends to every query on one side
    of an asymmetric pair, and a rewrite is a different query. Whatever
    `expander` hands back is embedded on its own, still with no prefix.

    `result_limit` rather than a `Settings`: `services/` may import only
    `domain/` and `ports/` (ADR-0009). `composition.build_pipeline` passes
    `settings.search_result_limit`, which is also what satisfies
    `test_every_setting_is_read_by_something`.

    **Two optional collaborators, and the optionality is a fact about this
    class rather than a habit.** M8's shape everywhere else is that a caller
    holding an `LLMClient` is *built or not built* -- `CurationService` spells
    its client `LLMClient`, never `LLMClient | None`, because
    `composition.llm_client` answers `(None, no-op)` when the LLM is off and
    the composition root simply declines to construct the service. That works
    because a deployment with no LLM runs no curation at all.

    It does not transfer here: **a deployment with no LLM still searches**, so
    a `SearchService` is built on every deployment there is, and "built or not
    built" has no state left to express. The choice is therefore between an
    optional collaborator and a second `SearchService` class, and the second is
    the one this project has never needed for `embedder`, which is the same
    shape one parameter over -- which is the precedent that settles it.
    (ADR-0022 argues the embedder is optional; it does not consider a second
    class, so this is a precedent by absence rather than by refusal.)
    `expander` is the same kind of thing as
    `embedder`, one layer up: a capability an operator may not have installed,
    on a service that must work without it, checked in exactly one place. The
    `LLMClient | None` branch M8 argues against lives on `QueryExpansionService`
    and is absent there for M8's reason; what is optional here is the *service*,
    not the client.
    """

    def __init__(
        self,
        index: SearchIndex,
        prefix_suggestions: SuggestIndex,
        fuzzy_suggestions: SuggestIndex,
        titles: TitleRepository,
        media_items: MediaItemRepository,
        watch_states: WatchStateRepository,
        taste: TasteRepository,
        embeddings: TitleEmbeddingRepository,
        *,
        result_limit: int,
        embedder: Embedder | None = None,
        expander: QueryExpansionService | None = None,
        analytics: SearchAnalytics | None = None,
        suggest_analytics: bool = False,
        now: Callable[[], AwareDatetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._index = index
        # **Two `SuggestIndex` implementations, both required, and neither
        # optional.** The argument `embedder` and `expander` make one parameter
        # over -- a capability an operator may not have installed -- does not
        # transfer: both indexes are btree/GIN reads over tables `m09a` creates
        # unconditionally, so "built or not built" has no state left to
        # express. An optional prefix tier would mean a `?tier=prefix` request
        # with no honest answer on a deployment that has the index anyway.
        #
        # **Named for the tiers rather than positioned.** Two adjacent
        # parameters of one type is a swap nothing catches: swapped, tier 1
        # becomes the 33.6 ms typo-tolerant path a client drives per keystroke
        # and tier 2 becomes prefix-only, both answer plausibly, and only a case
        # asserting that a typo is *absent* from tier 1 can tell. The names are
        # the wire's own (`?tier=prefix|fuzzy`) so the three surfaces read
        # alike.
        self._tiers: dict[SuggestTier, SuggestIndex] = {
            SuggestTier.PREFIX: prefix_suggestions,
            SuggestTier.FUZZY: fuzzy_suggestions,
        }
        self._titles = titles
        self._media_items = media_items
        # Not optional, unlike the two collaborators below it: a deployment
        # without a household is not a state this project has -- PRD 01's
        # authentication seam is a singleton row, and both callers resolve one
        # before they search. What is optional is the *argument* to `search`,
        # because a caller may legitimately have no household to speak for.
        self._watch_states = watch_states
        # **Not an `Embedder` and not a `TasteService`, and both absences are
        # the point.** The taste term needs a centroid; computing one is a walk
        # over ~50 titles and an embed, which is a job rather than a request,
        # so what reaches the blend is a centroid some *other* process wrote --
        # `TasteRepository.latest`, one indexed single-row probe. Routed
        # through `TasteService.centroid` instead, the term would be
        # structurally `None` on every request the shipped default serves: a
        # weight that reads like a signal and moves nothing, which is the
        # `GenreAffinityProvider` failure PRD 06 has corrected once already and
        # the direction hardest to notice.
        self._taste = taste
        # Read only when a centroid was found, and scoped by the model that
        # wrote it. See `_rank`.
        self._embeddings = embeddings
        self._result_limit = result_limit
        # Injected for the reason every clock in `services/` is: the recency
        # term is a function of the instant it is scored at, and a term read
        # off the wall clock is one no case can pin an age against.
        self._now = now
        # **The interval clock, and it is a second callable rather than a
        # second reading of `now`.** `_now` is a wall clock and answers an
        # `AwareDatetime` because `search_queries.at` is a timestamp somebody
        # will join against; this one is a monotone counter whose epoch is
        # unspecified, and it exists because `latency_ms` and
        # `usher.search.duration` are a *duration*. Injected rather than called
        # directly so a case can move it -- the clamp below defends a promise
        # `time.perf_counter` never breaks, and an injected clock is the only
        # thing that can break it.
        self._clock = clock
        # Optional, and a deployment without it still has search: full-text and
        # trigram are PRD 05's catalog-lookup tier and serve all 1,271,138
        # titles with no model at all.
        self._embedder = embedder
        # Optional on the same terms and off by default **twice**:
        # `USHER_LLM_ENABLED` is `false`, so `composition.build_pipeline` is
        # handed no client; and `USHER_QUERY_EXPANSION_ENABLED` is `false` even
        # when it is handed one, because PRD 05's 2026-08-07 measurement put
        # expansion's effect on retrieval the wrong way round. With this absent
        # every line below is M6's.
        self._expander = expander
        # **Optional, and the reason is a *caller* state rather than a
        # deployment state -- which is the difference from the two suggest
        # indexes above.** Those are required because `m09a` creates their
        # tables unconditionally, so "built or not built" had no state left to
        # express. `search_queries` is created just as unconditionally, and
        # every one of the three shipped roots supplies this. What is genuinely
        # variable is the *commit*: this collaborator ends the caller's
        # transaction, and a caller that is inside a larger unit of work it does
        # not own has no honest way to write the row. Absent, every line below
        # is exactly what it was before F2 and the search is unrecorded rather
        # than wrong.
        self._analytics = analytics
        # **A second switch beside it, and it narrows one surface rather than
        # the collaborator.** `analytics=None` is a *caller* state -- a caller
        # inside a larger unit of work it does not own -- and turns off both
        # writers; this is an *operator* state, `USHER_SEARCH_SUGGEST_ANALYTICS`,
        # and turns off only the keystroke one. Two different questions:
        # collapsing them would make "do not record keystrokes" also mean "do
        # not record searches", which is a setting nobody asked for and PRD 10
        # would refuse.
        #
        # 🔴 **Defaulting `False` is a measurement rather than an opinion, and
        # the bar was registered predicting the opposite.** Tier 1 end to end
        # through the shipped route against a clone of the real catalog: p50
        # **2.53 ms** without the row, **6.29 ms** with it. The refutation
        # condition was *under 5 ms with the writer on*, so the position that
        # both tiers should write unconditionally is refuted and the default
        # flips rather than the bar bending. `.claude/rules/
        # search-and-embeddings.md` carries the arms and the caveats.
        #
        # **It defaults the same way here as in `Settings`, deliberately.** Two
        # defaults for one decision is how they come to disagree, and the one
        # that would have been wrong here is this one: every shipped
        # construction goes through `composition.build_search_service`, so a
        # `True` left in this signature would be invisible until somebody built
        # a `SearchService` by hand -- which is what every unit case does.
        #
        # A `bool` and never a rate -- `_record_suggest` has the denominator
        # argument.
        self._suggest_analytics = suggest_analytics

    @property
    def records_suggestions(self) -> bool:
        """Whether an answered keystroke produces a `search_queries` row.

        Published so a request boundary can decide whether to resolve a
        household: the row is the only thing on the suggest path that needs
        one, and resolving it is a `users` read per keystroke.
        """
        return self._analytics is not None and self._suggest_analytics

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
        # **A keyword here and deliberately not a `SearchFilters` field.**
        # PRD 05 says `SearchFilters` is a closed vocabulary with no user
        # field, and the practical half of that is `usher search`'s
        # `--filter`-shaped flags and `GET /search`'s query string: a household
        # reachable from a query string is a household any caller can claim to
        # be. It is also not a filter -- it narrows nothing and returns no
        # different candidate set; it changes how the same set is ordered.
        user_id: uuid.UUID | None = None,
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

        **`user_id` is `None`-able and both shipped callers pass one.** With it
        absent the watch-state term is absent too and every other line here is
        M6's -- which is the state the numeric case pins, and the reason the
        parameter is optional rather than required: `SearchService` is
        constructed on every deployment there is and a caller with no household
        to speak for must still be able to search. **It is also the second
        thing that decides whether a `search_queries` row is written**, because
        `search_queries.user_id` is `NOT NULL` behind a real foreign key: a
        search nobody is speaking for has no row to write rather than a row
        with a hole in it, which is the same refusal PRD 10 spends a paragraph
        making about `clicked_title_id`.

        **`search_queries` gets exactly one row per *answered* search, and it
        is written after the answer is composed and after the measurement.**
        A write inside the measured window inflates the number it is recording,
        and `latency_ms` is that same window read once, so the table and
        `usher.search.duration` agree by construction rather than by two
        readings that drift. Failing to write it never fails a search -- see
        `_record_search`.

        **A row is a search, not a page.** Nothing here takes a cursor, an
        offset or an `after`, so a second page of one search cannot exist to be
        counted twice and the zero-result rate PRD 10 exists to compute is not
        diluted by scrolling. That is a property of this signature rather than
        a guard, and it is asserted structurally in
        `tests/unit/test_services_search.py` so the day `GET /search` grows
        pagination the decision has to be made again rather than defaulted.
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

        started = self._clock()
        # Resolved once and used twice -- by the coverage probe below and by
        # the request -- so the population the guard measured is the population
        # the search runs over. Two spellings of the same default would be two
        # populations the day either grew a branch.
        applied = filters or SearchFilters()
        vector: tuple[float, ...] | None = None
        expanded: str | None = None
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
                # **One completion, immediately in front of the embed, and its
                # position is the cost argument.** Inside this `else` it is
                # bought only by a search that was going to embed something --
                # so `full_text` pays nothing, a deployment with no model pays
                # nothing, a blank query pays nothing (the guard above returned
                # already) and `suggest`, which a client drives per keystroke,
                # has no embed at all and so has no call in front of one. The
                # unit of spend is one search, exactly as curation's is one
                # generation. `expand` never raises: PRD 08 says a degraded
                # subsystem narrows, and a search with no rewrite is a complete,
                # correct search.
                #
                # 🔴 **Position was not enough, and issue #16 is the gap it
                # left.** "This deployment has a model" and "this population
                # has vectors" are different facts, and the second is the one
                # an expansion can possibly help: with `title_embeddings` empty
                # the lane returns nothing however the query is worded, so a
                # rewrite was billed on every semantic and fused search of
                # every not-yet-backfilled deployment and the *"no title in the
                # filtered population has an embedding"* line arrived after the
                # money.
                #
                # **The probe is the number the answer already reports**, asked
                # a statement earlier: `SearchIndex.semantic_coverage` takes
                # filters and no vector. PRD 09 recorded the filtered predicate
                # as unanswerable before the vector existed -- it is not; the
                # only thing that was true is that nothing had spelled it
                # anywhere but inside `search`.
                #
                # **Ordered so the read is bought by the deployments that
                # expand and by nobody else.** `self._expander is not None`
                # comes first, and it is `None` on every shipped deployment
                # (`USHER_QUERY_EXPANSION_ENABLED` is `false` even where
                # `USHER_LLM_ENABLED` is `true`, PRD 05) -- which is the whole
                # of PRD 09's *"a read on every fused search"* objection,
                # answered by an `and` rather than by an argument. Hoisted
                # above it, the count runs for every deployment there is; the
                # ordering is pinned by
                # `test_the_shipped_default_probes_nothing_before_embedding`.
                #
                # `> 0.0` and not a tuned floor: this asks whether the lane can
                # answer *at all*, and any other threshold is a retrieval-
                # quality claim nothing in this project has measured.
                if (
                    self._expander is not None
                    and await self._index.semantic_coverage(applied) > 0.0
                ):
                    expanded = await self._expander.expand(query)
                vector = tuple(
                    (await self._embedder.embed([query if expanded is None else expanded]))[0]
                )

        outcome = await self._index.search(
            SearchRequest(
                # **The typed words, never the rewrite.** Only the vector is
                # computed from an expansion, so under RRF the lexical lane
                # goes on matching what the viewer actually wrote while the
                # semantic lane matches the paraphrase. Substituting here too
                # would leave no lane holding the original, and a rewrite that
                # drifted would turn an exact-title search into a search for
                # something else with nothing left to notice.
                query=query,
                # A ceiling, not a default: every candidate becomes a hydrated
                # row in application code, so an unclamped limit is a scan.
                limit=min(limit, self._result_limit),
                mode=mode,
                filters=applied,
                query_vector=vector,
            )
        )
        answer = SearchAnswer(
            results=await self._rank(outcome.hits, user_id=user_id),
            requested_mode=requested,
            mode=mode,
            # Passed through, never recomputed. It is the fraction of the
            # *filtered population* that had a vector; derived from the hits it
            # would read 1.0 whenever every returned hit had one, which is
            # exactly the case a green test seeds.
            semantic_coverage=outcome.semantic_coverage,
            # **Reported, never silently substituted.** This is the string that
            # was embedded whenever it is not `None`, so a caller can print it
            # beside the results; `usher search` does. A field that echoed the
            # typed query when nothing was expanded would put a line on every
            # search of every deployment and mean nothing.
            expanded_query=expanded,
        )
        # After the rank, not around the retrieval alone: PRD 05 splits the two
        # stages and an operator asking "why is search slow" is asking about
        # the answer, not about half of it.
        #
        # **One clock read, two consumers.** The histogram and
        # `search_queries.latency_ms` are the same interval by construction
        # rather than by two readings taken a few statements apart -- a second
        # read here would make the table and the panel disagree by whatever the
        # analytics write cost, which is exactly the quantity a reader would be
        # using the panel to look for.
        elapsed = self._clock() - started
        labels = {"mode": mode.value}
        _search_duration.record(elapsed, labels)
        _search_results.record(len(answer.results), labels)
        # **Outside the window, deliberately.** An INSERT inside it would be
        # counted as search latency by both the histogram and the row itself.
        #
        # The id comes back rather than being minted here so that the one
        # place that decides whether a row exists is also the one place that
        # decides whether there is an id to hand a client: an id echoed for
        # a row that was refused would send every outcome call to a
        # `WHERE id = …` that matches nothing, which is indistinguishable
        # from a client that never clicked (F3).
        return replace(
            answer,
            search_id=await self._record_search(
                query, mode=mode, user_id=user_id, results=len(answer.results), elapsed=elapsed
            ),
        )

    async def _record_search(
        self,
        query: str,
        *,
        mode: SearchMode,
        user_id: uuid.UUID | None,
        results: int,
        elapsed: float,
    ) -> uuid.UUID | None:
        """One `search_queries` row for one answered search, and the commit
        that makes it durable. **Answers the row's own id, or `None` when no
        row was written** -- which is the value `SearchAnswer.search_id`
        carries and therefore what `GET /search` echoes.

        **A refused write answers `None` rather than the id it minted**, and
        that is the whole reason the id is returned from here rather than
        minted by the caller. `record()` raising means there is no row, so
        an id handed out anyway would send F3's outcome calls to a
        `WHERE id = …` matching nothing -- and a no-op update is exactly
        what a search the household never clicked also produces, so the
        no-click rate PRD 10 exists to compute would silently absorb every
        refused row.

        **`mode` is the mode that ran**, the same value the histogram label
        carries, for the same reason: a degraded FUSED search stored as `fused`
        attributes full-text latency and a full-text result count to a lane
        that did not run, in the very panels PRD 10 builds this table for. The
        degradation is not stored at all -- it is on the wire as
        `SearchAnswer.requested_mode` and in the two histograms' `mode` label,
        and a tenth column for it is a PRD 10 amendment rather than a
        convenience (group F's third ruling).

        **`clicked_title_id` and `played` are not written here and are not
        omissions.** Neither is knowable at the instant a search answers;
        `SearchQueryRepository.record` writes `NULL` and `false` literally, and
        `record_outcome` fills them later (F3).

        **A failing analytics write must never fail a search, and the
        narrowness of the catch is the decision.** `except UsherPortError` and
        deliberately not `except Exception`: a `RepositoryConflict` means the
        row was refused -- a `latency_ms` past the `integer` column, a
        `user_id` naming no household -- and the household still gets the
        results it asked for, while a `TypeError` or a `ValidationError` out of
        this module is a bug in Usher, and a bug absorbed into a log line is
        billed as an outage. `QueryExpansionService.expand` pins the identical
        distinction in two cases of its own. **That catch lives in `_write_row`
        below rather than here since J2**, because `_record_suggest` needs the
        identical one and two copies of a measured guard are two chances to
        lose one -- `api/analytics.py` is the same decision for the outcome
        half, one layer out.

        **The commit is here rather than left to the caller**, and the reason
        is `cli._session_for`: it yields a session and disposes the engine
        **without ever committing**, so on the CLI path the row would be rolled
        back and the search would be recorded nowhere with nothing to say so.
        `api/deps.get_session` commits again when the handler returns and that
        second commit is a no-op over an already-committed transaction; what it
        costs on the route is that any read *after* this point begins a new
        transaction, which is why this is the last thing `search` does.

        **The query text reaches no log line.** PRD 08's rule is written about
        credentials (`docs/prd/08-operations.md:165`) and this extends it by
        analogy rather than by citation: what somebody typed into a search box
        is household state, `search_queries.query` is where it is meant to live
        -- durable, household-scoped and deletable with the household -- and a
        Loki record is none of the three. The failure is legible without it:
        the exception says what the store refused, and the row that was lost is
        one row.
        """
        analytics = self._analytics
        if analytics is None or user_id is None:
            return None
        return await _write_row(
            analytics,
            SearchQueryRecord(
                id=new_id(),
                at=self._now(),
                user_id=user_id,
                query=query,
                mode=mode,
                result_count=results,
                latency_ms=_ms(elapsed),
            ),
        )

    async def _record_suggest(
        self,
        prefix: str,
        *,
        tier: SuggestTier,
        user_id: uuid.UUID | None,
        results: int,
        elapsed: float,
    ) -> None:
        """One `search_queries` row for one answered keystroke -- PRD 10's
        amendment 2, and the writer `m10c` shipped the columns for.

        **It answers nothing, unlike `_record_search`.** `SearchAnswer` carries
        a `search_id` because F3's funnel attributes a click and a play against
        it; `SuggestResponse` carries no such field and no client can report an
        outcome against a keystroke, so an id published here would be a handle
        for a funnel that does not exist. The row is written for the *rate*
        questions PRD 10 wants -- whether real users type 2-4-character queries
        at all -- and those are counts over `surface = 'suggest'`.

        **`tier` is the parameter that selected the index, never a tier this
        service chose.** `suggest` passes through the argument it was given,
        which is the same rule the route already applies to its own echo
        (*"`tier=tier` and not a tier the service chose"*), so a response and a
        row can never disagree about which index answered.

        **`mode` is `FULL_TEXT`**: both tiers are btree/GIN reads with no embed
        and no fusion. The full argument, and the `WHERE surface = 'search'`
        every mode-split panel now owes, is on `SearchQueryRecord`.

        **No household means no row**, exactly as on the search path -- and
        that guard is load-bearing here in a way it never was there.
        `GET /search` and `usher search` both resolve one, so
        `_record_search`'s `user_id is None` arm is unreachable from a shipped
        caller. `suggest` has a third caller that resolves none:
        `usher.eval.surfaces.suggest` drives it once per probe and
        `usher eval suggest --full` drives thousands, so without this the
        evaluation harness would write its own traffic into the table as though
        a household had typed it. PRD 10's *"a search with no household"*
        exclusion is what makes that the right answer rather than a convenient
        one, and
        `test_a_suggest_with_no_household_writes_no_row_and_the_eval_harness_is_that_caller`
        is what says it stays true.

        **Whole or nothing, never sampled.** `self._suggest_analytics` is a
        `bool`, and PRD 10's *"which absence means what"* table is the reason:
        every absence in it is **exact**, so a sample rate would make every
        count over this surface an estimate and add a further absence -- *the
        row that was not written* -- indistinguishable in the data from the
        ones that are real. The volume is bounded by retention instead. ⚠️
        **Stated without a cardinality on purpose**: this sentence said *"all
        five of its rows"* while the table held six, having gone stale on the
        commit that added the row it is about. PRD 10 carries the correction.
        """
        analytics = self._analytics
        if analytics is None or user_id is None or not self._suggest_analytics:
            return
        await _write_row(
            analytics,
            SearchQueryRecord(
                id=new_id(),
                at=self._now(),
                user_id=user_id,
                query=prefix,
                mode=SearchMode.FULL_TEXT,
                result_count=results,
                latency_ms=_ms(elapsed),
                tier=tier,
            ),
        )

    async def suggest(
        self,
        prefix: str,
        limit: int = 10,
        *,
        tier: SuggestTier,
        # **`None`-able and mirroring `search`'s, and the default is the
        # decision.** A household is not a thing this path *uses* -- there is
        # no blend here, so no watch-state term and no taste term -- it is a
        # thing the row *needs*, because `search_queries.user_id` is `NOT NULL`
        # behind `ON DELETE RESTRICT`. Both request boundaries resolve one and
        # pass it; `usher.eval.surfaces.suggest` resolves none and does not,
        # so an evaluation probe writes nothing rather than writing evaluation
        # traffic wearing a household's clothes. Required-with-no-default here
        # would be the alternative and it is worse: it makes `usher eval`
        # *state* a household it does not have, which is a value it would then
        # have to invent.
        user_id: uuid.UUID | None = None,
    ) -> tuple[SearchResult, ...]:
        """Type-ahead candidates from one tier, hydrated and **not re-ranked**.

        Both tiers already ordered their own answer -- tier 2 by edit distance
        and then popularity inside its capped candidate set, tier 1 by
        popularity and vote count over an exact-prefix set where every distance
        is zero. Applying the search blend here would count popularity twice,
        once inside the tier and once outside it, and reorder the box away from
        the ordering the narrow path exists to produce. It would also make the
        two tiers *disagree* about a row they both matched, which is the one
        thing a client painting tier 1 and replacing it with tier 2 cannot
        absorb. Which is also the practical half of why `SuggestIndex` is its
        own port.

        **`tier` is a required keyword with no default, and the absence of a
        default is the decision.** The two callers want opposite ones --
        `GET /search/suggest` defaults to `prefix` because it is a keystroke
        path, `usher suggest` defaults to `fuzzy` because it is a command typed
        once and has been typo-tolerant since M6 -- so a default here would be
        one of the two silently serving the other. Each boundary states its own.

        **The hydration is written once and is the same two reads for both
        tiers**, `list_by_ids` then `owned_title_ids`, **regardless of hit
        count**. Spelled per tier it would answer identically today and drift
        the first time either tier grew a field, so
        `test_the_hydration_is_written_once_rather_than_once_per_tier` asserts
        it structurally as well as by count -- a duplicated body passes any
        count assertion.

        ✅ **This path can write one `search_queries` row per answered
        keystroke since M10's J2, on both tiers, and the vocabulary objection
        it used to carry is answered rather than absorbed.** That objection was
        two vocabularies
        under one name: `search_queries.mode` is a `SearchMode`, *"three
        reachable values"*, and a tier is a disjoint vocabulary
        (`prefix` | `fuzzy`). `m10c` took PRD 10's amendment 2 and gave the
        table `surface` and `tier`, so the two vocabularies are in two columns
        and every mode-split panel in dashboards 1 and 4 filters on `surface`
        -- which is a `WHERE surface = 'search'` those panels did not
        previously need, and is recorded in PRD 10 rather than discovered from
        a skewed panel.

        **What that bought is the question PRD 10 most wants this table for**
        -- *whether real users type 2-4-character queries at all* -- which is a
        question about this box and which M9 could not answer.

        🔴 **The volume argument did not go away with the vocabulary one, and
        the measurement it was answered with came back against the writer, so
        `USHER_SEARCH_SUGGEST_ANALYTICS` ships `false`.** Measured end to end
        through the shipped route against a clone of the real catalog under a
        bar written first: tier 1 is p50 **2.53 ms** without the row and
        **6.29 ms** with it -- the analytics write is 148% of the request on
        the path ADR-0031 exists to make cheap, against a refutation condition
        of 5 ms. Tier 2 pays the same ~3.3 ms as 7.8%, inside the 11.9% PRD 10
        already accepted for full text, and one switch governs both tiers for
        the reason `_record_suggest` gives, so the tier that cannot afford it
        decides. `.claude/rules/search-and-embeddings.md` carries the run.

        ✅ **It also emits `usher.suggest.duration` and
        `usher.suggest.results` since M10's D1, both labelled `tier`, and the
        histograms are unconditional where the row is not.** They answer a
        different question from the table, and only the table needed the
        schema change `m10c` made: a histogram answers *"is the box fast"* and
        `search_queries` answers *"what did people type"*. So they sit outside
        `USHER_SEARCH_SUGGEST_ANALYTICS` -- an operator who turned the row off
        to buy back the 148% would otherwise also have turned off the only
        series that could show whether the box was meeting its budget -- and
        they record on a deployment with no `analytics` collaborator at all,
        which is every unit case and `usher suggest`.

        **The measured interval is the whole method rather than the tier
        call**, because hydration is what a client waits for and it is the same
        two reads for both tiers by construction, so a window around the index
        probe alone would measure only the half that does not differ between
        the two things the label distinguishes.

        **The write is outside the measured window and after the hydration**,
        for `search`'s reason: an INSERT inside it would be counted as suggest
        latency by the very row recording it. It is also the last thing this
        method does, because the commit ends the caller's transaction. The two
        `record` calls are on the near side of it and share its one clock read,
        so the row and the histogram are the same interval.

        **A short `prefix` is refused before the measurement and therefore
        before the row and before either histogram point.** The route's own
        length bound returns even earlier (`_MIN_CHARS_FOR_TIER`), so neither a
        blank keystroke nor one below its tier's minimum is an answered query,
        and PRD 10's *"a blank or whitespace-only query"* exclusion covers
        both. Counted, a blank one would make the p50 a measure of how fast
        somebody types.
        """
        if not prefix.strip():
            return ()
        started = self._clock()
        hits = await self._tiers[tier].suggest(prefix, limit=min(limit, self._result_limit))
        by_id = {
            title.id: title
            for title in await self._titles.list_by_ids([hit.title_id for hit in hits])
        }
        owned = await self._media_items.owned_title_ids(list(by_id))
        results = tuple(
            _result(by_id[hit.title_id], owned=hit.title_id in owned, score=hit.score)
            for hit in hits
            if hit.title_id in by_id
        )
        elapsed = self._clock() - started
        # **The measured interval is the whole method and not the tier call**,
        # because hydration is what a client waits for and it is the same two
        # reads for both tiers by construction -- so a window around
        # `self._tiers[tier].suggest` alone would report the half of the cost
        # that does not differ between the two things the label distinguishes.
        # It is the same clock read `search_queries.latency_ms` gets, for the
        # reason `test_the_row_and_the_histogram_are_the_same_interval` gives
        # one method over: two intervals taken a few statements apart differ by
        # whatever ran between them, which is exactly the quantity an operator
        # reading the panel is looking for.
        #
        # **Recorded before the analytics write and after the hydration**, so
        # the row's own INSERT is outside the number the row is about.
        _suggest_duration.record(elapsed, {"tier": tier.value})
        _suggest_results.record(len(results), {"tier": tier.value})
        await self._record_suggest(
            prefix,
            # Passed through, never re-derived. The row has to name the index
            # that ran, and the only thing that knows which one that was is the
            # argument that selected it out of `self._tiers`.
            tier=tier,
            user_id=user_id,
            results=len(results),
            elapsed=elapsed,
        )
        return results

    async def _rank(
        self, hits: Sequence[SearchHit], *, user_id: uuid.UUID | None
    ) -> tuple[SearchResult, ...]:
        """PRD 05 stage 2, over one already-retrieved candidate set.

        **Three reads with a household and two without, regardless of hit
        count** -- which is the whole reason `list_by_ids`, `owned_title_ids`
        and `played_title_ids` exist in the batch shape they do. This docstring
        said "two reads" until the household arrived; the count is asserted
        against fakes rather than described, because a per-hit spelling answers
        identically and costs a statement a hit.

        `played_title_ids` rolls a watched episode up to its series through
        `COALESCE(ws.title_id, e.title_id)`, which is what keeps this from
        being a films-only answer for a television household -- the roll-up is
        the port's, and re-deriving it here would be a second definition of
        "seen".
        """
        if not hits:
            return ()
        titles = {
            title.id: title
            for title in await self._titles.list_by_ids([hit.title_id for hit in hits])
        }
        owned = await self._media_items.owned_title_ids(list(titles))
        # Not read at all without a household, rather than read with a
        # placeholder: `played_title_ids` is scoped by `user_id`, so an invented
        # one is a statement per search answering about a user nobody is.
        played = (
            frozenset[uuid.UUID]()
            if user_id is None
            else await self._watch_states.played_title_ids(user_id, list(titles))
        )
        stored = None if user_id is None else await self._taste.latest(user_id)
        centroid = None if stored is None else stored.centroid
        vectors: dict[uuid.UUID, tuple[float, ...]] = (
            {}
            if stored is None or centroid is None
            else await self._embeddings.list_for_titles(list(titles), model_name=stored.model_name)
        )
        today = self._now().date()
        ranks = _dense_ranks(hits)
        results = [
            _result(
                titles[hit.title_id],
                owned=hit.title_id in owned,
                score=_blend(
                    relevance=_RELEVANCE_K / (_RELEVANCE_K + rank),
                    popularity=_popularity_term(titles[hit.title_id].tmdb_popularity),
                    owned=1.0 if hit.title_id in owned else 0.0,
                    # **A small boost, never a demotion, and the direction is
                    # the decision PRD 05 leaves open.** A search is
                    # overwhelmingly a re-find intent -- somebody typing a
                    # title's name usually wants that title -- so demoting what
                    # the household has finished buries the exact film they
                    # just named. `RediscoverProvider` already treats a
                    # finished title as re-offerable, which is the same call
                    # one surface over. The opposite reading is defensible for
                    # *discovery* and renders identically, which is exactly why
                    # it is written down here rather than left in the sign of a
                    # constant.
                    #
                    # `None` rather than `0.0` without a household: nobody
                    # measured this title's watch state, and scoring the
                    # absence would rank an unknown identically to a known
                    # never-watched. ADR-0014, one signal over from popularity.
                    played=None if user_id is None else (1.0 if hit.title_id in played else 0.0),
                    recency=_recency_term(titles[hit.title_id], today=today),
                    # **`None` rather than 0.0 in both absent cases** --
                    # ADR-0014, in a sixth place. A zero cosine is a real
                    # orthogonality claim about two vectors; "this household
                    # has no stored centroid" and "this hit has no vector
                    # under the model that centroid names" are not claims
                    # about the title at all. Scored zero, the whole
                    # un-embedded catalog would sink beneath whatever the
                    # backfill reached first -- and `title_embeddings` is
                    # currently empty on every catalog this project has, so
                    # that is the *population* rather than a corner.
                    taste=_taste_term(centroid, vectors.get(hit.title_id)),
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
    """Positions, with equal index scores sharing a position -- and an exact
    name match in a group of its own.

    **Dense rather than strict, and the difference is load-bearing.** Under a
    strict positional rank no two candidates ever tie on relevance, so the
    owned boost could only ever be a tie-break that no case could construct --
    `test_an_owned_title_outranks_an_unowned_one_at_equal_relevance` would be
    unassertable and a boost of zero would pass the suite.

    Scores are compared for **equality only**, never for magnitude, so this is
    indifferent to whether a backend reports a similarity or a distance. One
    fewer cross-backend convention to get right, and a reason to prefer a
    rank-derived relevance term over the obvious score-derived one.

    🔴 **`SearchHit.exact_name` leads the key, and it is the whole of issue
    #25's fix on this side of the port.** The lane already orders exact matches
    first (`ports.search.SearchHit`), so this could look like a formality --
    it is not, and the reason is a measurement this repository already carries
    one module over: `ts_rank_cd` ties are *pervasive* rather than occasional
    (a tie group of 498 among the top 500 values for one query). Tied with the
    row behind it, an exact match **shares** dense rank 0, the relevance term
    cancels between them exactly, and popularity then decides between a film
    and a video essay named after it. Leading the key makes rank 0 a group of
    one, which is the state `_WEIGHTS`' arithmetic bound is stated over: no
    combination of the other five can displace it, margin `0.005 / 1.045` =
    0.004785 with all six signals present. **Carry the denominator with the
    margin** -- this docstring read 0.009615 until 2026-09-02, which is
    `0.01 / 1.04`, the bound with the *taste* term absent, and so twice the
    real headroom on any search that has one.

    Two exact matches with different index scores still take ranks 0 and 1 --
    that is two titles with one name, which is a real state of this catalog and
    an ordering the lexical score is entitled to decide.
    """
    ranks: list[int] = []
    rank = 0
    previous: tuple[bool, float] | None = None
    for hit in hits:
        key = (hit.exact_name, hit.score)
        if previous is not None and key != previous:
            rank += 1
        ranks.append(rank)
        previous = key
    return ranks


def _popularity_term(popularity: float | None) -> float | None:
    """`p / (p + midpoint)`, or `None` when nobody has measured it.

    **`None` is not 0.0** -- ADR-0014, in a fourth place.
    `titles.tmdb_popularity`
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


def _recency_term(title: Title, *, today: date) -> float | None:
    """`1 / (1 + age / midpoint)`, or `None` when nobody has dated it.

    **`None` is not 0.0** -- ADR-0014, in a fifth place, after
    `_popularity_term`'s fourth. `titles.year` is null for every row the IMDb
    dump gave no start year and for everything a source contributed without
    one, and `year or 0` would put those at maximum age: the un-enriched
    catalog buried beneath the enriched tier, looking like arithmetic and
    raising nothing. `_blend` drops an absent signal from numerator *and*
    denominator, so an undated title is scored on what is known about it. The
    observable consequence, and the case that pins it: at equal relevance, an
    undated title ranks above one with a measured old year.

    **`release_date` where the enriched tier has one, `year` otherwise.** The
    two are the same fact at two precisions and both are on `Title`; taking the
    coarse one when the fine one is present would throw away eleven months of
    the only signal this term has. A `year` alone is read as 1 January, which
    is a bias of at most half a year against a midpoint of twenty-five.

    A release in the future -- and TMDb dates unreleased films -- is clamped to
    age zero rather than allowed a term above 1.0, which would put an
    announcement above everything ever made.
    """
    released = title.release_date or (None if title.year is None else date(title.year, 1, 1))
    if released is None:
        return None
    age_years = max((today - released).days, 0) / _DAYS_IN_YEAR
    return 1.0 / (1.0 + age_years / _RECENCY_MIDPOINT_YEARS)


def _taste_term(
    centroid: tuple[float, ...] | None, vector: tuple[float, ...] | None
) -> float | None:
    """`max(0, cos)` held inside `[0, 1]`, or `None` when there is nothing to
    compare.

    **`None` is not 0.0** -- ADR-0014, in a sixth place after
    `_popularity_term`'s fourth and `_recency_term`'s fifth. There are three
    ways to get it and all three mean the same thing to `_blend`, *drop this
    signal for this row*:

    - **No stored centroid.** The household has none, or has a written refusal
      (`StoredTaste.centroid is None`, a household below `TasteService.
      _MIN_TITLES`). Both are the shipped default -- no worker has run -- and
      neither is a statement about any title.
    - **No vector under that centroid's model.** `list_for_titles` is scoped by
      `StoredTaste.model_name` here, so a row from another checkpoint is absent
      exactly as a missing row is. That collapse is the port's own and is
      deliberate: a caller that drops the term either way does not need to know
      which, and one that branched on it would be reading the backfill's
      progress out of a data row.
    - **A vector of another width**, which the model scope makes unreachable
      in principle and which is guarded anyway, for `CandidatePoolService.
      _cosine`'s reason one service over: `zip(strict=True)` across two widths
      raises, and a search request is not the place to discover that something
      else changed.

    **Clamped to `[0, 1]`, which is `similar.py`'s `_clamped` shape and its
    argument.** `_blend` is only a weighted *mean* if every term is in `[0, 1]`;
    a negative cosine outside it would make the taste weight a penalty of
    unbounded relative size on exactly the rows the term knows least about. A
    negative cosine is not "unlike your taste" in any sense this project has
    measured -- the corpus-level statistic that would license reading it that
    way does not exist -- so 0.0 loses nothing.

    **The lower clamp is load-bearing and the upper one is not, and saying so
    is the point.** Dividing by the norms makes the value a true cosine, so it
    cannot exceed 1.0 by more than float error and `min` is guarding rounding
    rather than data -- unlike `similar.py`'s `_clamped`, whose upper arm
    defends a `CHECK (score <= 1)` against a port implementation. It stays
    because `_blend`'s claim to be a weighted *mean* is a claim about the
    range, and a range assertion with one open end is not one.

    **A second spelling of `CandidatePoolService._cosine` and not a shared
    helper**, because the two answer different questions: that one returns the
    raw cosine, where a negative is a meaningful ordering inside a stratum;
    this one returns a bounded *term*, where a negative is out of range. One
    function serving both would have to grow a flag deciding which, which is
    the point at which the sharing costs more than the copy. The norms are
    recomputed rather than assumed for the reason recorded there: `Embedder`
    guarantees unit vectors and `TasteService._normalise` makes the centroid
    one, so both are 1.0 in every shipped configuration -- but a stored
    vector's norm is a property of whatever wrote it, and this is not the place
    to find out that something else did.
    """
    if centroid is None or vector is None or len(vector) != len(centroid):
        return None
    dot = sum(one * other for one, other in zip(centroid, vector, strict=True))
    norms = math.sqrt(sum(value * value for value in centroid)) * math.sqrt(
        sum(value * value for value in vector)
    )
    if norms == 0.0:
        # Unreachable through `list_for_titles`, which never hands back a zero
        # vector, and guarded for `_normalise`'s reason one module over: a
        # `ZeroDivisionError` in a ranking function is the kind of thing that
        # becomes reachable the day somebody relaxes a refusal, and it would
        # arrive as a 500 on a search.
        return None
    return min(1.0, max(0.0, dot / norms))


def _blend(**signals: float | None) -> float:
    """A weighted mean over the signals that are actually present.

    An absent signal leaves **both** the numerator and the denominator, so a
    title with no popularity is scored on what is known about it rather than
    penalised for what is not. The observable consequence: at equal relevance,
    unknown popularity ranks above a measured zero, and an undated title ranks
    above one with a measured old year.

    Written as a sum over an explicit signal list -- the same skeleton
    `SimilarityService` uses -- so that landing a term is adding a term and a
    weight in both places rather than rewriting two scorers. Watch state and
    recency arrived that way; the taste centroid is the one still to come.

    **It is scale-invariant, which is what lets new terms arrive without moving
    old scores.** Multiplying every weight by the same factor changes nothing,
    and adding a term changes only the rows where that term is *present* -- so
    a hit with no popularity, no year and no household scores exactly what M6
    scored it, and `_WEIGHTS`' comment says why the three M6 numbers therefore
    cannot be re-balanced against each other.
    """
    total = 0.0
    applied = 0.0
    for name, value in signals.items():
        if value is None:
            continue
        total += _WEIGHTS[name] * value
        applied += _WEIGHTS[name]
    return total / applied if applied else 0.0


def _ms(seconds: float) -> int:
    """`search_queries.latency_ms`, which is `>= 0` in the column
    (`ck_search_queries_latency_ms_non_negative`).

    **The clamp is `adapters/llm/openai_compatible.py:181`'s shape and it
    defends a promise the shipped clock never breaks.** `time.perf_counter` is
    non-decreasing by contract, so a negative delta is unreachable with it --
    the injected clock is the only thing that can produce one, which is exactly
    what makes a guard against a promise nobody breaks testable at all. Without
    it a backwards clock is a `RepositoryConflict` on the path that has just
    answered a search correctly.
    """
    return max(0, int(seconds * 1000))


async def _write_row(analytics: SearchAnalytics, record: SearchQueryRecord) -> uuid.UUID | None:
    """Write one `search_queries` row and make it durable, or say it did not.

    **One function for both retrieval writers**, which is the same call
    `api/analytics.py` makes for the outcome half one layer out: *"three call
    sites, one function, because the interesting part is the absorption rather
    than the call"*. `_record_search` and `_record_suggest` differ in which
    columns they know and in nothing else, and spelled inline the guard below
    would exist twice -- so a later writer would be the one that forgot it.

    **A refused write answers `None` rather than the id it minted.**
    `record()` raising means there is no row, so an id handed out anyway would
    send F3's outcome calls to a `WHERE id = …` matching nothing -- and a no-op
    update is exactly what a search the household never clicked also produces,
    so the no-click rate PRD 10 exists to compute would silently absorb every
    refused row. `_record_suggest` discards the answer because a keystroke has
    no outcome to attribute; `_record_search` publishes it as
    `SearchAnswer.search_id`.

    **`except UsherPortError` and deliberately not `except Exception`.** A
    `RepositoryConflict` means the store refused the row -- a `latency_ms` past
    the `integer` column, a `user_id` naming no household -- and the caller
    still gets the results it asked for; a `TypeError` or a `ValidationError`
    out of this module is a bug in Usher, and a bug absorbed into a log line is
    billed as an outage. The guard is defence in depth on both paths, so the
    only way to test it is to inject a repository that raises.

    ⚠️ **So a database that is *unwell* rather than refusing fails the request,
    and `analytics.commit` being inside the `try` is where.** It is
    `AsyncSession.commit` itself (`composition.py`), and
    `refusals_as_conflict` translates only a row refusal on purpose -- its own
    docstring declines to report *"a dropped connection, a statement timeout or
    a missing table"* as the row being wrong -- so both statements here can
    raise a raw `SQLAlchemyError`, which is not a `UsherPortError` and leaves
    this function as a 500 on a request whose results were already computed.
    That is F2's shipped shape on `GET /search` and it is unchanged; what M10
    changed is what it is reachable *from*, because a browser drives
    `GET /search/suggest` per keystroke. Widening the catch is not the answer
    -- the paragraph above is why -- and the bound is the switch:
    `USHER_SEARCH_SUGGEST_ANALYTICS` ships `false`, so the keystroke path pays
    this only where an operator has opted in.

    **Neither the query text nor the surface's own words reach the log line.**
    What somebody typed is household state whose home is
    `search_queries.query` -- durable, household-scoped, deletable with the
    household -- and a Loki record is none of the three. The failure is legible
    without it: the exception says what the store refused, and the row that was
    lost is one row.
    """
    try:
        await analytics.queries.record(record)
        await analytics.commit()
    except UsherPortError as exc:
        logger.error(
            "the {surface} analytics row was refused; this request is unrecorded: {error}",
            surface=record.surface.value,
            error=str(exc) or type(exc).__name__,
        )
        return None
    return record.id


def _result(title: Title, *, owned: bool, score: float) -> SearchResult:
    return SearchResult(
        title_id=title.id,
        kind=title.kind,
        name=title.name,
        year=title.year,
        popularity=title.tmdb_popularity,
        owned=owned,
        score=score,
    )


__all__ = [
    "EmbeddingDocument",
    "SearchAnalytics",
    "SearchAnswer",
    "SearchService",
    "SemanticSearchUnavailable",
    "compose_document",
]
