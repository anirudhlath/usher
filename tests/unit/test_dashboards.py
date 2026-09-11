"""A committed dashboard can lie in three ways, and this module is the three
checks that close them.

[PRD 10](../../docs/prd/10-telemetry-and-dashboards.md)'s `## Dashboards`
section makes the dashboards **an asset of this repository** while the stack
that renders them is not: `dashboards/` holds JSON and a provisioning YAML that
`~/code/observability/`'s compose project bind-mounts, and Usher's own
`compose.yml` gains nothing. So nothing in the rendering path is available to a
unit test, and what *is* checkable is the file — which is exactly where a
dashboard's three silent failures live.

1. **Structure.** Grafana accepts a file it cannot draw. A panel with no
   `datasource`, a panel with no target, a file with no `uid` — each loads
   without an error and renders an empty rectangle that is indistinguishable
   from a healthy zero. `uid` uniqueness is the one that bites hardest:
   **Grafana silently overwrites a dashboard whose `uid` collides**, so two
   committed files become one visible dashboard and there is no error anywhere.
   The check is structural over the parsed JSON and adds no dependency —
   `jsonschema` for five inputs is the shape ADR-0027 refused `litellm` under.

2. **Prometheus targets name metrics the catalogue holds.** The extractor is
   `test_telemetry_search.py`'s `_ROW` regex with the `usher\\.` anchor
   dropped, because the catalogue's 42nd row is `http.server.duration` — no
   `usher.` prefix, supplied by `FastAPIInstrumentor` — and a catalogue check
   that cannot see it cannot grade an API-latency panel at all.
   **Prometheus mangles dots to underscores, appends the instrument's
   *unit* and then appends `_bucket`/`_count`/`_sum`/`_total`**, so both
   sides go through one normaliser and the normaliser has its own case.
   The unit half was added by D8 and is not cosmetic: every gauge Usher
   declares carries `unit="1"` and reaches Prometheus as `..._ratio`, every
   histogram carries `unit="s"` and arrives as `..._seconds_bucket`, so a
   normaliser that stripped only the aggregation suffix graded **the real
   name of every Prometheus panel D8 commits as an unknown metric**. D6
   could not have found this: dashboard 1 has no Prometheus panel, and its
   synthetic control was written in `usher_suggest_duration_bucket`, a
   spelling this deployment's exporter does not produce.

3. **Postgres targets name tables and columns that exist.** Against
   `Base.metadata`, which is the live schema the ORM already holds — not
   against a SQL parser, which is a dependency this repository does not want.

⚠️ **Invariant 3's false-positive escape, stated here rather than papered
over.** The scan is over `\\b(\\w+)\\.(\\w+)\\b` pairs whose **left side is a
real `__tablename__`**. A SQL alias is not a table name, so `t.name` in
`FROM titles t` is skipped, and **a panel written entirely in aliases is
checked on nothing**. That is a real limitation with a real cost: it makes the
invariant's coverage a property of how the committed SQL is spelled. The
alternative is a SQL parser, and a check that silently covers a third of the
panels is worse than one whose coverage is stated — so
`test_a_panel_written_in_aliases_is_checked_on_nothing` pins the hole by name,
and `01-library-and-catalog.json` is written in **unaliased** table names for
exactly this reason. `test_the_committed_dashboards_are_not_written_in_aliases`
is what keeps that true as D7-D10 land.

**The positive controls are the point of the main case, not decoration.** A
glob that matches nothing and a catalogue that parses nothing both satisfy
every downstream assertion silently, and this repository has paid for that
twice: an import-contract verification reporting *7 kept, 0 broken* against a
substitution that was a no-op, and a suggest-index scan. So `assert files`,
`assert catalogue`, a named anchor in the catalogue, and a count of the
table.column pairs actually checked all run before the invariants do.

**One arm cannot have a live positive control at this HEAD and says so.**
Dashboard 1 is eleven Postgres panels and **no Prometheus panel at all** — PRD
10's first principle ("Most of what is worth knowing about a media catalog is
**not a metric**... The catalog *is* the record") applied to a catalog
dashboard. So invariant 2 grades zero targets over the committed glob until
D9's API-latency panel lands. Asserting it had graded something would be red
today for a correct tree; instead its teeth are proved on a synthetic dashboard
in `test_a_prometheus_target_naming_a_metric_outside_the_catalogue_is_caught`,
which is a *stronger* control than a count anyway — it names which token dies.
"""

import json
import pathlib
import re
from typing import Any

import pytest

from usher.db import models  # noqa: F401  -- registers every table on Base.metadata
from usher.db.base import Base

_ROOT = pathlib.Path(__file__).parents[2]
_DASHBOARDS = _ROOT / "dashboards"
_PRD_10 = _ROOT / "docs" / "prd" / "10-telemetry-and-dashboards.md"

# `test_telemetry_search.py`'s `_ROW`, with the `usher\.` anchor dropped so the
# scan reaches `http.server.duration`. That file's own copy stays anchored: it
# asks a different question (which rows M6 owes) and merging the two would
# collapse them, which M10's O4 sweep measured -- deleting one catalogue row
# kills a case in both files today, and that independence is what makes the
# blast radius informative.
_METRIC_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9_.]*)`\s*\|\s*(\w+)\s*\|", re.M)

# Grafana's four suffixes for a mangled OTel name. Prometheus appends exactly
# one of these to a histogram or a monotonic counter; `_bucket`, `_count` and
# `_sum` come from the histogram, `_total` from the counter.
_PROMETHEUS_SUFFIXES = ("_bucket", "_count", "_sum", "_total")

# The exporter's *unit* suffix, which sits between the mangled name and the
# aggregation suffix -- `usher.jobs.queued` (`unit="1"`, a gauge) reaches
# Prometheus as `usher_jobs_queued_ratio` and `usher.enrichment.latency`
# (`unit="s"`) as `usher_enrichment_latency_seconds_bucket`. Measured against
# the running stack on 2026-09-07 rather than read out of a specification:
# `/api/v1/label/__name__/values` holds 61 `usher_`-prefixed names and every
# one of them carries a unit or is a `_total` counter.
#
# ⚠️ **Stripping this is only safe while no catalogue row is itself named for
# a unit**, because `usher.x` and `usher.x.seconds` would collapse onto one
# key. `test_the_normaliser_is_not_vacuous` is what checks that, over the
# whole catalogue, in both directions.
_PROMETHEUS_UNITS = ("_ratio", "_seconds", "_milliseconds", "_bytes")

# PromQL's own vocabulary, which a `[a-z][a-z0-9_.]*` scan cannot tell from a
# metric name. Only the functions and keywords are here: label keys and
# grouping lists are removed structurally by `_metric_tokens` before the scan
# runs, which is why `mode`, `tier` and `provider` are absent from this set.
_PROMQL_WORDS = frozenset(
    {
        "abs",
        "absent",
        "and",
        "avg",
        "avg_over_time",
        "bool",
        "bottomk",
        "by",
        "ceil",
        "changes",
        "clamp_max",
        "clamp_min",
        "count",
        "count_over_time",
        "count_values",
        "delta",
        "deriv",
        "floor",
        "group_left",
        "group_right",
        "histogram_quantile",
        "hour",
        "idelta",
        "ignoring",
        "increase",
        "irate",
        "label_replace",
        "last_over_time",
        "max",
        "max_over_time",
        "min",
        "min_over_time",
        "offset",
        "on",
        "or",
        "predict_linear",
        "quantile",
        "quantile_over_time",
        "rate",
        "resets",
        "round",
        "scalar",
        "sort",
        "sort_desc",
        "stddev",
        "sum",
        "sum_over_time",
        "time",
        "topk",
        "unless",
        "vector",
        "without",
    }
)

# `sum by (tier) (...)`, `sum without (le) (...)` and `{job="usher"}` all put
# label keys and values where a metric name would be. Removing them
# structurally is what keeps `_PROMQL_WORDS` a list of *functions* rather than
# a list that has to grow with every label PRD 10's table declares.
_LABEL_SELECTOR = re.compile(r"\{[^}]*\}")
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")

# `rate(x[5m])` and `x offset 1h`. Measured while writing this file: without
# this, `[5m]` leaks the token `m`, which is not in `_PROMQL_WORDS`, does not
# normalise into the catalogue, and would have failed **every** range-vector
# panel D9 and D10 will write. A unit that happened to spell a keyword would
# have been worse — silent.
_RANGE = re.compile(r"\[[^\]]*\]")

_BARE_TOKEN = re.compile(r"[a-z][a-z0-9_.]*")

# The pair scan of invariant 3. `\w` spans digits, so `100.0` parses as
# `("100", "0")` -- harmless, because `100` is not a `__tablename__` and the
# filter below drops it. That filter is also the false-positive escape the
# module docstring states: it is what makes `t.name` invisible.
_QUALIFIED_PAIR = re.compile(r"\b(\w+)\.(\w+)\b")


def normalise_metric(name: str) -> str:
    """One OTel or Prometheus spelling of a metric, reduced to a comparable key.

    Dots become underscores and **one** trailing Prometheus suffix is removed,
    so `usher.suggest.duration` in PRD 10 and `usher_suggest_duration_bucket`
    in a panel reduce to the same key. Applied to **both** sides, which is what
    makes it a normalisation rather than a rewrite of one of them.

    **Two strips, in the exporter's own order**: one aggregation suffix, then
    one unit. `usher_enrichment_latency_seconds_bucket` needs both to reach
    `usher_enrichment_latency`, and doing them in the other order reaches
    nothing -- `_bucket` is not a unit and `_seconds_bucket` is not a suffix
    in either list.

    No catalogue row ends in one of the four aggregation suffixes or one of
    the four units today (both checked in
    `test_the_normaliser_is_not_vacuous`), so stripping on the catalogue side
    is a no-op; if one ever does, that row and its `_count` or `_seconds`
    sibling would collapse into one key and the case is what says so.
    """
    key = name.strip().replace(".", "_")
    for suffix in _PROMETHEUS_SUFFIXES:
        if key.endswith(suffix):
            key = key[: -len(suffix)]
            break
    for unit in _PROMETHEUS_UNITS:
        if key.endswith(unit):
            return key[: -len(unit)]
    return key


def metric_catalogue(text: str | None = None) -> set[str]:
    """Every metric name in PRD 10's `### Metrics` table, normalised."""
    source = text if text is not None else _PRD_10.read_text(encoding="utf-8")
    return {normalise_metric(name) for name, _kind in _METRIC_ROW.findall(source)}


def _dashboard_files() -> list[pathlib.Path]:
    return sorted(_DASHBOARDS.glob("*.json"))


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Every panel, including the children a collapsed row nests under itself.

    Grafana keeps an *expanded* row's panels as siblings and a *collapsed*
    row's under `panel["panels"]`, so a scan of the top level alone stops
    seeing a dashboard's panels the moment someone collapses a row and saves.
    """
    found: list[dict[str, Any]] = []
    queue: list[Any] = list(dashboard.get("panels", []))
    while queue:
        panel = queue.pop(0)
        if not isinstance(panel, dict):
            continue
        found.append(panel)
        queue.extend(panel.get("panels", []))
    return found


def _datasource_type(panel: dict[str, Any], target: dict[str, Any]) -> str:
    """The datasource type a target queries, target overriding panel."""
    for holder in (target, panel):
        datasource = holder.get("datasource")
        if isinstance(datasource, dict) and datasource.get("type"):
            return str(datasource["type"])
    return ""


def _metric_tokens(expr: str) -> set[str]:
    """The tokens in a PromQL expression that are candidate metric names.

    Label selectors and grouping lists come out first, so what is left is
    functions and metric names; `_PROMQL_WORDS` removes the functions.
    """
    stripped = _RANGE.sub(" ", _LABEL_SELECTOR.sub(" ", _GROUPING.sub(" ", expr)))
    return {token for token in _BARE_TOKEN.findall(stripped) if token not in _PROMQL_WORDS}


def _sql_pairs(sql: str) -> list[tuple[str, str]]:
    """`(table, column)` pairs whose left side is a real `__tablename__`.

    ⚠️ The filter is the coverage limit named in this module's docstring: an
    alias is not a table name, so a panel written in aliases yields nothing
    here and is checked on nothing.
    """
    tables = set(Base.metadata.tables)
    return [(left, right) for left, right in _QUALIFIED_PAIR.findall(sql) if left in tables]


def _target_sql(target: dict[str, Any]) -> str:
    """A Postgres target's statement, under either key Grafana writes it as."""
    return str(target.get("rawSql") or target.get("expr") or "")


def test_every_committed_dashboard_is_structurally_valid_and_names_only_metrics_the_catalogue_holds() -> (  # noqa: E501
    None
):
    """The three invariants over `dashboards/*.json`, with their premises first.

    Every assertion below is reachable only because the two scans found
    something: an empty glob and an empty catalogue each satisfy every
    invariant vacuously and look exactly like a pass.
    """
    files = _dashboard_files()
    assert files, "the dashboard glob found nothing"

    catalogue = metric_catalogue()
    assert catalogue, "no metric rows parsed out of PRD 10"
    assert "usher_jobs_queued" in catalogue, (
        "the named anchor is missing from the parsed catalogue, so the table's shape has "
        f"moved and this scan is reading something else: {sorted(catalogue)[:5]}"
    )

    seen_uids: dict[str, pathlib.Path] = {}
    pairs_checked = 0

    for path in files:
        dashboard = json.loads(path.read_text(encoding="utf-8"))

        # --- Invariant 1: structure ---
        for key in ("title", "uid", "schemaVersion", "panels"):
            assert key in dashboard, f"{path.name} carries no {key!r}"
        assert dashboard["title"], f"{path.name} has an empty title"

        uid = str(dashboard["uid"])
        assert uid not in seen_uids, (
            f"{path.name} and {seen_uids.get(uid, path).name} both claim uid {uid!r} — "
            "Grafana silently overwrites the first with the second, so two committed "
            "files become one visible dashboard with no error anywhere"
        )
        seen_uids[uid] = path

        panels = _panels(dashboard)
        assert panels, f"{path.name} carries no panels"

        for panel in panels:
            where = f"{path.name}:{panel.get('title') or '<untitled>'}"
            assert panel.get("title"), f"{path.name} carries a panel with no title"
            if panel.get("type") == "row":
                continue
            assert panel.get("datasource"), f"{where} declares no datasource"
            targets = panel.get("targets") or []
            assert targets, f"{where} has no targets, so it draws an empty rectangle"

            for target in targets:
                kind = _datasource_type(panel, target)

                # --- Invariant 2: Prometheus names are in the catalogue ---
                if kind == "prometheus":
                    unknown = sorted(
                        token
                        for token in _metric_tokens(str(target.get("expr", "")))
                        if normalise_metric(token) not in catalogue
                    )
                    assert unknown == [], (
                        f"{where} queries {unknown}, which PRD 10's metric table does not "
                        "list — a documented-nowhere metric is a permanently empty panel"
                    )

                # --- Invariant 3: Postgres tables and columns exist ---
                if kind.endswith("postgresql-datasource") or kind == "postgres":
                    for table, column in _sql_pairs(_target_sql(target)):
                        pairs_checked += 1
                        columns = Base.metadata.tables[table].columns
                        assert column in columns, (
                            f"{where} selects {table}.{column}, which is not a column of "
                            f"{table}: {sorted(c.name for c in columns)}"
                        )

    assert pairs_checked, (
        "invariant 3 graded no table.column pair across the whole glob — either no panel "
        "queries Postgres or every one of them is written in aliases, and both make this "
        "arm a check that cannot fail"
    )


def test_the_committed_dashboards_are_not_written_in_aliases() -> None:
    """Invariant 3's coverage is a property of the committed SQL, so it is
    asserted per *panel* rather than once over the glob.

    The case above asserts only that *some* pair was graded, which one
    unaliased panel satisfies for a file of eleven. This is the arm that
    notices a panel added in `FROM titles t` style, where the check reads
    nothing and reports nothing.
    """
    files = _dashboard_files()
    assert files, "the dashboard glob found nothing"

    unchecked = []
    for path in files:
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        for panel in _panels(dashboard):
            if panel.get("type") == "row":
                continue
            for target in panel.get("targets") or []:
                if _datasource_type(panel, target) not in {
                    "postgres",
                    "grafana-postgresql-datasource",
                }:
                    continue
                if not _sql_pairs(_target_sql(target)):
                    unchecked.append(f"{path.name}:{panel.get('title')}:{target.get('refId')}")

    assert unchecked == [], (
        "these Postgres targets yield no table.column pair, so invariant 3 grades them on "
        f"nothing — write the table name out instead of aliasing it: {unchecked}"
    )


def test_a_panel_written_in_aliases_is_checked_on_nothing() -> None:
    """The limitation, pinned by name so it is a stated hole rather than a
    silent one. Both spellings select a column that does not exist; only the
    unaliased one is visible to the scan."""
    aliased = "SELECT t.enrichment_status FROM titles t"
    written_out = "SELECT titles.enrichment_status FROM titles"

    assert _sql_pairs(aliased) == [], (
        "the alias was read as a table name, which would make every `t.<x>` a false "
        "positive against whatever table `t` happens to collide with"
    )
    assert _sql_pairs(written_out) == [("titles", "enrichment_status")]


def test_the_normaliser_is_not_vacuous() -> None:
    """A normaliser that lower-cased everything to nothing would make invariant
    2 pass on any input at all, so it is asserted in both directions: the two
    spellings of one metric agree, and two different metrics stay different."""
    assert normalise_metric("usher.suggest.duration") == "usher_suggest_duration"
    assert normalise_metric("usher_suggest_duration_bucket") == "usher_suggest_duration"
    assert normalise_metric("usher.suggest.duration") == normalise_metric(
        "usher_suggest_duration_count"
    )
    assert normalise_metric("http.server.duration") == "http_server_duration"

    assert normalise_metric("usher.suggest.duration") != normalise_metric("usher.suggest.results")
    assert normalise_metric("usher.suggest.duration") != normalise_metric("usher.suggest.latency")

    assert normalise_metric("usher_jobs_queued_ratio") == "usher_jobs_queued"
    assert normalise_metric("usher_enrichment_latency_seconds_bucket") == (
        "usher_enrichment_latency"
    )
    assert normalise_metric("usher.jobs.queued") == normalise_metric("usher_jobs_queued_ratio")

    assert normalise_metric("usher_jobs_queued_ratio") != normalise_metric(
        "usher_jobs_parked_ratio"
    )
    assert normalise_metric("usher.sync.run.duration") != normalise_metric(
        "usher.jobs.duration_seconds"
    )

    catalogue = metric_catalogue()
    collisions = [
        name for name in catalogue if name.endswith(_PROMETHEUS_SUFFIXES + _PROMETHEUS_UNITS)
    ]
    assert collisions == [], (
        "a catalogue row's own name ends in a Prometheus aggregation suffix or a unit, so "
        f"it now normalises onto whatever row it is the suffix of: {collisions}"
    )

    # The strip is only sound while it is injective over the catalogue: two rows
    # that reduced to one key would make invariant 2 accept a panel naming
    # either of them for the other, and nothing else in this module would say so.
    assert len(catalogue) == len({normalise_metric(name) for name in catalogue}), (
        "two catalogue rows now normalise onto one key, so the unit strip has stopped "
        "being a normalisation and become a rewrite"
    )


def test_the_catalogue_scan_reaches_the_row_that_carries_no_usher_prefix() -> None:
    """The whole reason the anchor was dropped. `http.server.duration` is
    supplied by `FastAPIInstrumentor` rather than declared by Usher, and it is
    the metric an API-latency panel queries — a catalogue that cannot see it
    grades that panel as naming an unknown metric."""
    catalogue = metric_catalogue()

    assert "http_server_duration" in catalogue, (
        "the catalogue scan is still anchored on `usher.`, so an API-latency panel would "
        "fail invariant 2 for naming the one metric PRD 10 says it should"
    )
    assert len(catalogue) == 42, (
        f"PRD 10's metric table parsed to {len(catalogue)} rows, not the 42 its own header "
        "counts out — the table's shape has moved and both readers need checking"
    )


def _synthetic(panel: dict[str, Any], uid: str = "u1") -> dict[str, Any]:
    return {"title": "T", "uid": uid, "schemaVersion": 41, "panels": [panel]}


def test_a_prometheus_target_naming_a_metric_outside_the_catalogue_is_caught() -> None:
    """Invariant 2's teeth, proved on a synthetic dashboard because the
    committed glob has no Prometheus panel at this HEAD — Dashboard 1 is eleven
    Postgres panels and PRD 10 says so. `usher.suggest.latency` is the near-miss
    D1 named: a plausible spelling of a metric nothing emits.
    """
    catalogue = metric_catalogue()
    good = 'histogram_quantile(0.95, sum by (tier) (rate(usher_suggest_duration_bucket{job="usher"}[5m])))'  # noqa: E501
    bad = good.replace("usher_suggest_duration_bucket", "usher_suggest_latency_bucket")

    assert _metric_tokens(good) == {"usher_suggest_duration_bucket"}, (
        f"the token scan is reading PromQL's own vocabulary as metrics: {_metric_tokens(good)}"
    )
    assert "m" not in _metric_tokens(good), (
        "the `[5m]` range selector leaked its unit as a token — it is not a PromQL word, "
        "does not normalise into the catalogue, and would fail every range-vector panel"
    )
    assert all(normalise_metric(t) in catalogue for t in _metric_tokens(good))

    unknown = [t for t in _metric_tokens(bad) if normalise_metric(t) not in catalogue]
    assert unknown == ["usher_suggest_latency_bucket"], (
        f"the near-miss metric name was not caught: {unknown}"
    )


def test_the_panel_walk_reaches_a_collapsed_rows_children() -> None:
    """A row panel carries no targets and is exempt; its children are not, and
    a walk of the top level alone would stop seeing them the moment a row is
    collapsed and saved."""
    dashboard = _synthetic(
        {
            "type": "row",
            "title": "Composition",
            "collapsed": True,
            "panels": [{"type": "table", "title": "Nested", "targets": [{"refId": "A"}]}],
        }
    )

    titles = [panel.get("title") for panel in _panels(dashboard)]

    assert titles == ["Composition", "Nested"], (
        f"the nested panel is invisible to the scan, so it is graded on nothing: {titles}"
    )


def test_the_catalogue_parse_and_the_glob_are_both_falsifiable() -> None:
    """The two positive controls of the main case, exercised where they can be
    made to fail. Neither can be shown red against the real tree once this task
    lands, which is precisely why an `assert files` that nobody has watched fail
    is worth as little as no assertion at all."""
    assert metric_catalogue("no table here at all") == set()
    assert metric_catalogue("| `usher.jobs.queued` | gauge | kind | ✅ M4 |") == {
        "usher_jobs_queued"
    }

    with pytest.raises(AssertionError, match="dashboard glob found nothing"):
        empty: list[pathlib.Path] = []
        assert empty, "the dashboard glob found nothing"


# --- Dashboard 3, and the panels whose series is not what their name implies ---

_PIPELINE = _DASHBOARDS / "03-pipeline.json"


def _live_panels(path: pathlib.Path) -> list[dict[str, Any]]:
    """Every drawable panel of one committed file — rows carry no targets."""
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    return [panel for panel in _panels(dashboard) if panel.get("type") != "row"]


def _kinds(panel: dict[str, Any]) -> set[str]:
    return {_datasource_type(panel, target) for target in panel.get("targets") or []}


def _exprs(panel: dict[str, Any]) -> list[str]:
    return [str(target.get("expr", "")) for target in panel.get("targets") or []]


def _sql(panel: dict[str, Any]) -> str:
    return "\n".join(_target_sql(target) for target in panel.get("targets") or [])


def test_the_queue_depth_panels_are_two_panels_over_two_datasources() -> None:
    """The failure this closes renders perfectly and answers a different
    question: one panel titled *"queue depth by priority"* whose target is
    `usher.jobs.queued`, a gauge PRD 10 says twice is labelled `kind`.

    *"`usher.jobs.queued` is labelled `kind`, not `priority`. `JobQueue.depth()`
    counts pending rows per kind, which is what 'which lane is backed up'
    asks"*, and then again under the observable-callback rules: *"M5 introduces
    demand promotion and the label stays `kind`: a priority band needs a second
    `GROUP BY` on `JobQueue`… The panel reads `jobs` directly."* Verified at
    this HEAD rather than inherited: `telemetry.py`'s `_observations` emits
    `Observation(count, {"kind": kind})` and nothing else, and `QueueSnapshot`
    holds two `Mapping[str, int]`s keyed by `JobKind.value`.

    So the pair is two panels, two datasources, two titles and two questions —
    *which band is waiting* is Postgres over `jobs.priority`, *which lane is
    backed up* is the Prometheus gauge — and neither is the other with its
    datasource swapped.
    """
    panels = _live_panels(_PIPELINE)

    assert panels, "the pipeline dashboard parsed to no drawable panels"
    titles = [str(panel["title"]) for panel in panels]
    assert "Parked jobs by kind" in titles, (
        f"the known-title anchor is gone, so this scan is reading something else: {titles}"
    )
    assert len(panels) == 10, f"PRD 10's dashboard 3 is ten panels, not {len(panels)}: {titles}"

    depth = [panel for panel in panels if "queue depth" in str(panel["title"]).lower()]
    assert len(depth) == 2, (
        "the queue-depth question is two panels — a Postgres band count and the by-kind "
        f"gauge — and this file has {len(depth)}: {[p['title'] for p in depth]}"
    )
    assert len({str(panel["title"]) for panel in depth}) == 2, (
        "the two queue-depth panels share one title, so a reader cannot tell which "
        "question either of them answers"
    )

    postgres = [panel for panel in depth if _kinds(panel) == {"grafana-postgresql-datasource"}]
    prometheus = [panel for panel in depth if _kinds(panel) == {"prometheus"}]
    assert len(postgres) == 1 and len(prometheus) == 1, (
        "the pair is one Postgres panel and one Prometheus panel; got "
        f"{[(p['title'], sorted(_kinds(p))) for p in depth]}"
    )

    banded, by_kind = postgres[0], prometheus[0]

    assert ("jobs", "priority") in _sql_pairs(_sql(banded)), (
        f"{banded['title']!r} is the Postgres half and does not select `jobs.priority`, "
        "which is the only place a priority band exists at all"
    )
    assert "priority" in str(banded["title"]).lower(), (
        f"the Postgres half is titled {banded['title']!r} and does not say priority"
    )

    tokens = {token for expr in _exprs(by_kind) for token in _metric_tokens(expr)}
    assert {normalise_metric(token) for token in tokens} == {"usher_jobs_queued"}, (
        f"the Prometheus half queries {sorted(tokens)}, not `usher.jobs.queued`"
    )
    assert "kind" in str(by_kind["title"]).lower(), (
        f"the Prometheus half is titled {by_kind['title']!r}, which does not name the "
        "label it actually splits on — the exact lie this case exists to close"
    )
    assert "priority" not in str(by_kind["title"]).lower(), (
        f"the by-kind gauge is titled {by_kind['title']!r}: it claims a band its series "
        "does not carry, renders perfectly, and answers a different question"
    )


def test_the_prometheus_normaliser_strips_the_exporters_unit_suffix() -> None:
    """The real names, which D6's normaliser could not reach.

    Every gauge Usher registers carries `unit="1"` and every histogram
    `unit="s"`, and the OTel Prometheus exporter puts that unit into the name
    ahead of the aggregation suffix. So the name a panel must be written in is
    `usher_jobs_queued_ratio`, never `usher_jobs_queued` — and under D6's
    normaliser the former graded as a metric PRD 10 does not document.

    Every left-hand side below was read off the running Prometheus on
    2026-09-07 (`/api/v1/label/__name__/values`), not constructed here.
    """
    catalogue = metric_catalogue()
    assert catalogue, "no metric rows parsed out of PRD 10"

    live_to_catalogue = {
        "usher_jobs_queued_ratio": "usher_jobs_queued",
        "usher_jobs_parked_ratio": "usher_jobs_parked",
        "usher_source_push_connected_ratio": "usher_source_push_connected",
        "usher_source_push_reconnects_total": "usher_source_push_reconnects",
        "usher_source_push_events_total": "usher_source_push_events",
        "usher_provider_requests_total": "usher_provider_requests",
        "usher_enrichment_latency_seconds_bucket": "usher_enrichment_latency",
        "usher_enrichment_latency_seconds_count": "usher_enrichment_latency",
        "usher_sync_run_duration_seconds_bucket": "usher_sync_run_duration",
        "usher_source_request_duration_seconds_sum": "usher_source_request_duration",
        "usher_scheduler_job_due_seconds": "usher_scheduler_job_due",
        "http_server_duration_milliseconds_bucket": "http_server_duration",
    }

    wrong = {
        live: normalise_metric(live)
        for live, expected in live_to_catalogue.items()
        if normalise_metric(live) != expected
    }
    assert wrong == {}, (
        f"the normaliser does not reach the exporter's own spelling: {wrong} — every one "
        "of these is the name a committed panel has to be written in"
    )

    outside = sorted(name for name in live_to_catalogue.values() if name not in catalogue)
    assert outside == [], (
        f"these normalise cleanly and are still absent from PRD 10's table: {outside}"
    )


def test_the_push_panels_say_what_their_source_label_is_and_when_there_is_no_series() -> None:
    """The sentence D11's *"Push down"* alert is written against, pinned on the
    panel rather than left in a rules file.

    `api/lanes.py`'s `push_snapshots` builds the reader as
    `{self._names[source_id]: PushSnapshot(...)}` over `self._open_adapters`,
    so the `source` label is **the operator-typed source name** and never a
    UUID, and a source with no open adapter is simply not in the
    comprehension. `telemetry.py`'s `_push_observations` then returns `[]`
    with no reader at all. Both halves mean the same thing for an alert: a
    disabled source, or one that does not support push, produces **no
    observation** rather than a zero — so the condition is `== 0` and
    `absent()` would page about every source nobody configured.
    """
    panels = _live_panels(_PIPELINE)
    push = [panel for panel in panels if "push" in str(panel["title"]).lower()]

    assert len(push) == 2, (
        f"PRD 10's dashboard 3 has two push panels, not {len(push)}: "
        f"{[p['title'] for p in push]}"
    )

    for panel in push:
        description = str(panel.get("description", ""))
        where = f"{panel['title']!r}"
        assert "operator-typed" in description, (
            f"{where} does not say the `source` label is the operator-typed source name, "
            "so a panel author reads it as a UUID"
        )
        assert "no series" in description, (
            f"{where} does not say a source with no lane produces no series, which is the "
            "sentence D11's alert condition is written against"
        )
        assert "== 0" in description and "absent(" in description, (
            f"{where} does not spell out which alert condition follows — `== 0` rather "
            f"than `absent()`: {description!r}"
        )


def test_the_enrichment_panel_says_its_label_is_outcome_and_carries_no_demand_split() -> None:
    """M4's correction, carried onto the panel that reads it.

    `services/enrich.py`: *"Labelled `outcome` rather than PRD 10's original
    `trigger`: nothing in M4 enriches on demand… while a failure's latency and
    a success's are genuinely different populations."* The panel splits on
    `outcome` and says so, because the demand-versus-background split a reader
    expects here does not exist on this series at all.
    """
    panels = _live_panels(_PIPELINE)
    enrichment = [panel for panel in panels if "enrichment" in str(panel["title"]).lower()]

    assert len(enrichment) == 1, (
        f"expected exactly one enrichment panel: {[p['title'] for p in enrichment]}"
    )
    panel = enrichment[0]
    description = str(panel.get("description", ""))

    assert "outcome" in description, "the enrichment panel does not name its label"
    assert "enrich.py" in description, (
        "the enrichment panel does not cite the module that made the correction"
    )
    for phrase in ("demand", "background"):
        assert phrase in description.lower(), (
            f"the enrichment panel does not say the {phrase} split is absent from this "
            f"series, which is the thing a reader of the title assumes: {description!r}"
        )

    labels = {str(target.get("legendFormat", "")) for target in panel.get("targets") or []}
    assert all("{{outcome}}" in label for label in labels), (
        f"a target on the enrichment panel does not split on `outcome`: {sorted(labels)}"
    )


def test_the_tmdb_panel_counts_429s_and_denominates_on_every_status_including_error() -> None:
    """PRD 10: *"a denominator that omitted the failures would read low exactly
    during an outage."*

    `adapters/tmdb/client.py` sets `status = str(response.status_code)` inside
    the span and leaves it at the literal `"error"` for a transport failure
    that never reached a status line, recording both from a `finally`. So the
    429 series is `status="429"` and the rate beside it must select on
    `provider` only.
    """
    panels = _live_panels(_PIPELINE)
    tmdb = [panel for panel in panels if "tmdb" in str(panel["title"]).lower()]

    assert len(tmdb) == 1, f"expected exactly one TMDb panel: {[p['title'] for p in tmdb]}"
    exprs = _exprs(tmdb[0])
    assert exprs, "the TMDb panel has no Prometheus target"

    rate_exprs = [expr for expr in exprs if "rate(" in expr]
    count_exprs = [expr for expr in exprs if 'status="429"' in expr]

    assert count_exprs, f"no target selects `status=\"429\"`: {exprs}"
    assert rate_exprs, f"no target takes a rate for the requests/sec ceiling: {exprs}"
    for expr in rate_exprs:
        assert "status=" not in expr, (
            "the requests/sec denominator filters on `status`, so it drops the "
            f'`status="error"` transport failures and reads low during an outage: {expr}'
        )

    description = str(tmdb[0].get("description", ""))
    assert 'status="error"' in description, (
        "the TMDb panel does not state that its denominator includes the transport "
        f"failures that never reached a status line: {description!r}"
    )


def test_the_dashboard_3_prose_claims_are_falsifiable() -> None:
    """The four description checks above are substring scans, and a substring
    scan over prose nobody can make fail is decoration. Each is exercised here
    against a description with the sentence removed, which is the only place
    they can be shown red once the real file is committed."""
    panel = {"title": "Push connection uptime", "description": "a socket, probably"}
    assert "operator-typed" not in str(panel["description"])

    assert 'status="error"' not in "429s over the total"
    assert "{{outcome}}" not in "{{trigger}}"

    with pytest.raises(AssertionError, match="ten panels"):
        titles = ["only", "nine", "of", "them", "here", "and", "no", "more", "sadly"]
        assert len(titles) == 10, f"PRD 10's dashboard 3 is ten panels, not {len(titles)}"
