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
   **Prometheus mangles dots to underscores and appends
   `_bucket`/`_count`/`_sum`/`_total`**, so both sides go through one
   normaliser and the normaliser has its own case.

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

# 🔴 **And a *unit* segment before them, which this file did not know about
# until a Prometheus panel was committed against it.** The OTel collector's
# Prometheus translation appends the instrument's unit to the name: `s` ->
# `_seconds`, `ms` -> `_milliseconds`, `By` -> `_bytes`, and a gauge's `1` ->
# `_ratio`. Read off this host's Prometheus on 2026-09-07, PRD 10's
# `usher.home.compose.duration` is stored as
# `usher_home_compose_duration_seconds_bucket` and `http.server.duration` as
# `http_server_duration_milliseconds_bucket`.
#
# Stripping only the four above left `usher_home_compose_duration_seconds`,
# which is in no catalogue -- so invariant 2 **rejected the spelling that
# renders data and accepted the spelling that renders none**, which is the
# exact failure it exists to catch.
# `test_the_catalogue_check_survives_the_unit_suffix_the_collector_appends`
# is the case; it lists the stored spellings this host actually holds.
_UNIT_SUFFIXES = ("_seconds", "_milliseconds", "_bytes", "_ratio")

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

    Dots become underscores, then **one** trailing Prometheus suffix and
    **one** unit segment are removed, in that order — so PRD 10's
    `usher.suggest.duration` and the `usher_suggest_duration_seconds_bucket`
    Prometheus actually stores reduce to the same key. Applied to **both**
    sides, which is what makes it a normalisation rather than a rewrite of one
    of them.

    The order is the storage order and is not interchangeable: the exporter
    writes `<name>_<unit>_<suffix>`, so stripping the unit first would find
    nothing to strip on `..._seconds_bucket` and leave the row unmatched.

    No catalogue row ends in one of these eight segments today (checked in
    `test_the_normaliser_is_not_vacuous`), so stripping on the catalogue side
    is a no-op; if one ever does, that row and its `_count` sibling would
    collapse into one key and the case is what says so.
    """
    key = name.strip().replace(".", "_")
    for suffix in _PROMETHEUS_SUFFIXES:
        if key.endswith(suffix):
            key = key[: -len(suffix)]
            break
    for suffix in _UNIT_SUFFIXES:
        if key.endswith(suffix):
            return key[: -len(suffix)]
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

    catalogue = metric_catalogue()
    collisions = [
        name for name in catalogue if name.endswith(_PROMETHEUS_SUFFIXES + _UNIT_SUFFIXES)
    ]
    assert collisions == [], (
        "a catalogue row's own name ends in a Prometheus or unit suffix, so it now "
        f"normalises onto whatever row it is the suffix of: {collisions}"
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


# ---------------------------------------------------------------------------
# D9's three additions. The first is the panel invariant Dashboard 4's
# hit-rate panel exists to keep; the second and third are two ways a committed
# panel renders nothing-that-looks-like-something, and neither was reachable
# before a Prometheus panel was committed.
# ---------------------------------------------------------------------------

_AGGREGATION = re.compile(r"\b(?:sum|avg|min|max|count|topk|bottomk|stddev|quantile)\b")
_GROUPING_CLAUSE = re.compile(r"\s*(?:by|without)\s*\(([^)]*)\)")


def _balanced(text: str, opening: int) -> int:
    """The index of the `)` closing the `(` at `opening`."""
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError(f"unbalanced parentheses in {text!r}")


def _aggregations(expr: str) -> list[tuple[set[str], str]]:
    """`(grouping labels, operand)` for every aggregation call in a PromQL
    expression, in both spellings PromQL allows — `sum by (x) (…)` and
    `sum(…) by (x)`.

    A substring test for `by (cache` would pass on an expression that carries
    the grouping on some *other* aggregation than the one wrapping the
    counter, which is exactly the mistake this pair of panels invites: the
    numerator grouped and the denominator summed flat renders a hit rate that
    is a ratio of two different populations and still draws a plausible line.
    """
    found: list[tuple[set[str], str]] = []
    for match in _AGGREGATION.finditer(expr):
        position = match.end()
        labels: set[str] = set()
        leading = _GROUPING_CLAUSE.match(expr, position)
        if leading:
            labels = {label.strip() for label in leading.group(1).split(",") if label.strip()}
            position = leading.end()
        opening = expr.find("(", position)
        if opening == -1:
            continue
        closing = _balanced(expr, opening)
        trailing = _GROUPING_CLAUSE.match(expr, closing + 1)
        if trailing:
            labels = {label.strip() for label in trailing.group(1).split(",") if label.strip()}
        found.append((labels, expr[opening + 1 : closing]))
    return found


def _prometheus_targets() -> list[tuple[str, str]]:
    """`(where, expr)` for every committed Prometheus target."""
    targets: list[tuple[str, str]] = []
    for path in _dashboard_files():
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        for panel in _panels(dashboard):
            if panel.get("type") == "row":
                continue
            for target in panel.get("targets") or []:
                if _datasource_type(panel, target) == "prometheus":
                    where = f"{path.name}:{panel.get('title')}:{target.get('refId')}"
                    targets.append((where, str(target.get("expr", ""))))
    return targets


def test_the_cache_panel_groups_by_cache_and_never_sums_across_it() -> None:
    """PRD 10 declares `usher.cache.hits` with `cache` **and** `freshness`
    while `usher.cache.misses` carries `cache` alone — *"a miss served nothing,
    so it has no freshness to report"* — and the pair is declared once in
    `telemetry.py` for three callers precisely because a second stream under
    one name makes *"a dashboard's hit rate silently stop covering a cache"*.

    A hit rate summed across `cache` is that failure arriving from the panel
    end instead: the row cache, the screen cache and the image proxy have
    different populations and different hit rates, and one pooled number is
    dominated by whichever is busiest. It renders as a healthy line either way.
    """
    aggregations = [
        (where, labels, operand)
        for where, expr in _prometheus_targets()
        for labels, operand in _aggregations(expr)
    ]
    naming_hits = [
        (where, labels, operand)
        for where, labels, operand in aggregations
        if "usher_cache_hits_total" in operand
    ]

    assert aggregations, (
        "no committed Prometheus target carries an aggregation at all, so every assertion "
        "below is vacuous"
    )
    assert naming_hits, (
        "no committed aggregation names `usher_cache_hits_total`, so this case grades "
        "nothing — Dashboard 4's hit-rate panel is missing or spells the counter otherwise"
    )

    ungrouped = [
        f"{where}: {operand.strip()}"
        for where, labels, operand in naming_hits
        if "cache" not in labels
    ]

    assert ungrouped == [], (
        "these aggregations sum the hit counter without a `by (cache)`, so three caches "
        f"with three populations render as one number: {ungrouped}"
    )


_CREATE_HISTOGRAM = "create_histogram("


def _declared_histograms() -> dict[str, str]:
    """Every `create_histogram` in `src/usher/`, name → its keyword body.

    A source scan rather than an import, because the question is what the
    *declaration* says: importing the modules would give instruments whose
    advisory the SDK has already folded into an aggregation, and
    `explicit_bucket_boundaries_advisory` is not readable back off an
    `opentelemetry.metrics.Histogram`.
    """
    declared: dict[str, str] = {}
    for path in sorted((_ROOT / "src" / "usher").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        cursor = 0
        while (found := source.find(_CREATE_HISTOGRAM, cursor)) != -1:
            opening = found + len(_CREATE_HISTOGRAM) - 1
            closing = _balanced(source, opening)
            body = source[opening + 1 : closing]
            cursor = closing
            name = re.search(r"[\"']([a-z][a-z0-9_.]*)[\"']", body)
            if name:
                declared[name.group(1)] = body
    return declared


def test_no_committed_panel_takes_a_quantile_over_a_histogram_still_on_the_sdk_defaults() -> None:
    """D1's finding, made a property of the committed JSON.

    `configure_metrics` installs no `View`, so a seconds-unit histogram with
    no `explicit_bucket_boundaries_advisory` of its own takes the SDK's
    `(0.0, 5.0, 10.0, 25.0, …)` — **in seconds** — and every observation under
    five seconds lands in one bucket. `histogram_quantile` over that does not
    fail and does not empty: it interpolates inside `le="5"` and answers a
    flat plausible number. D1 measured 2.5000 s and 4.75 s against a sample
    whose true p50/p95 were 35.20 ms and 225.07 ms.

    So a quantile panel over an unfixed instrument is the one dashboard defect
    the other two invariants cannot see — the metric name is real, the
    structure is valid, and the panel draws a line.
    """
    declared = _declared_histograms()
    seconds = {name for name, body in declared.items() if 'unit="s"' in body}
    with_advisory = {
        name for name, body in declared.items() if "explicit_bucket_boundaries_advisory" in body
    }

    assert len(declared) >= 15, (
        f"the source scan found only {len(declared)} `create_histogram` calls, so it has "
        "stopped reading the declarations and every assertion below is vacuous"
    )
    assert "usher.suggest.duration" in with_advisory, (
        "the scan cannot see D1's advisory on `usher.suggest.duration`, so it would grade "
        f"every instrument as unfixed: {sorted(with_advisory)}"
    )
    assert seconds - with_advisory, (
        "every seconds-unit histogram now carries an advisory — the repo-wide fix has "
        "landed, and this case has nothing left to protect. Delete it and say so."
    )

    unfixed = {normalise_metric(name) for name in seconds - with_advisory}
    offending = [
        f"{where}: {operand.strip()}"
        for where, expr in _prometheus_targets()
        if "histogram_quantile" in expr
        for _labels, operand in _aggregations(expr)
        for token in _metric_tokens(operand)
        if normalise_metric(token) in unfixed
    ]

    assert offending == [], (
        "these panels take a quantile over a histogram still on the SDK's second-scale "
        "default boundaries, which answers a flat number rather than failing — plot "
        f"`_sum / _count` or fix the instrument's buckets: {offending}"
    )


def test_the_catalogue_check_survives_the_unit_suffix_the_collector_appends() -> None:
    """🔴 The normaliser was measured against OTel's *name* mangling and not
    against what the collector actually stores, and the two differ by a
    segment.

    Read out of this host's Prometheus on 2026-09-07, the committed
    instruments are `usher_home_compose_duration_seconds_bucket`,
    `http_server_duration_milliseconds_bucket` and `usher_jobs_queued_ratio` —
    the exporter appends the *unit* (`s` → `_seconds`, `ms` →
    `_milliseconds`, `By` → `_bytes`, a gauge's `1` → `_ratio`) before the
    `_bucket`/`_total` suffix. A normaliser that strips only the latter leaves
    `usher_home_compose_duration_seconds`, which is in no catalogue.

    The consequence is the worst available ordering. Invariant 2 **rejects the
    spelling that renders data** and **accepts the spelling that renders
    none** — a panel written `usher_home_compose_duration_bucket` passes this
    file and draws an empty rectangle in Grafana forever, which is precisely
    the failure the invariant exists to catch.
    """
    catalogue = metric_catalogue()

    stored = {
        "usher_home_compose_duration_seconds_bucket": "usher.home.compose.duration",
        "usher_row_build_duration_seconds_sum": "usher.row.build.duration",
        "usher_search_duration_seconds_count": "usher.search.duration",
        "usher_suggest_duration_seconds_bucket": "usher.suggest.duration",
        "http_server_duration_milliseconds_bucket": "http.server.duration",
        "usher_jobs_queued_ratio": "usher.jobs.queued",
        "usher_scheduler_job_due_seconds": "usher.scheduler.job.due",
        "usher_cache_hits_total": "usher.cache.hits",
        "usher_search_results_bucket": "usher.search.results",
    }

    for spelling, row in stored.items():
        assert normalise_metric(spelling) == normalise_metric(row), (
            f"{spelling!r} — the spelling Prometheus actually holds — does not reduce onto "
            f"PRD 10's {row!r}, so a panel that renders data fails invariant 2"
        )
        assert normalise_metric(spelling) in catalogue, (
            f"{spelling!r} normalises to {normalise_metric(spelling)!r}, which PRD 10's "
            "table does not hold"
        )

    assert normalise_metric("usher_suggest_results") != normalise_metric("usher_suggest_duration"), (
        "the unit strip has eaten a name segment and collapsed two instruments onto one key"
    )
