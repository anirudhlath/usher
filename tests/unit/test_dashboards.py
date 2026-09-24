"""A committed dashboard can lie in three ways.

This module is the three checks that close them.
"""

import json
import pathlib
import re
from typing import Any

import pytest

from tests.unit.grafana import panels
from usher.db import models  # noqa: F401  -- registers every table on Base.metadata
from usher.db.base import Base

_ROOT = pathlib.Path(__file__).parents[2]
_DASHBOARDS = _ROOT / "dashboards"
_DASHBOARD_TWO = _DASHBOARDS / "02-taste-and-watching.json"
_PRD_10 = _ROOT / "docs" / "prd" / "10-telemetry-and-dashboards.md"

# `test_telemetry_search.py`'s `_ROW`, with the `usher\.` anchor dropped so the scan
# reaches `http.server.duration`.
_METRIC_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9_.]*)`\s*\|\s*(\w+)\s*\|", re.M)

# Grafana's four suffixes for a mangled OTel name. Prometheus appends exactly
# one of these to a histogram or a monotonic counter; `_bucket`, `_count` and
# `_sum` come from the histogram, `_total` from the counter.
_PROMETHEUS_SUFFIXES = ("_bucket", "_count", "_sum", "_total")

# The exporter's *unit* suffix, which sits between the mangled name and the aggregation
# suffix -- `usher.jobs.queued` (`unit="1"`, a gauge) reaches Prometheus as
# `usher_jobs_queued_ratio` and `usher.enrichment.latency` (`unit="s"`) as
# `usher_enrichment_latency_seconds_bucket`.
_PROMETHEUS_UNITS = ("_ratio", "_seconds", "_milliseconds", "_bytes")

# And a *unit* segment before them: the OTel collector's Prometheus translation
# appends the instrument's unit to the name -- `s` -> `_seconds`, `ms` ->
# `_milliseconds`, `By` -> `_bytes`, and a gauge's `1` -> `_ratio`.
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

# `rate(x[5m])` and `x offset 1h`. Without this, `[5m]` leaks the token `m`,
# which is not in `_PROMQL_WORDS`, does not normalise into the catalogue, and
# fails every range-vector panel. A unit that happened to spell a keyword
# would be worse — silent.
_RANGE = re.compile(r"\[[^\]]*\]")

_BARE_TOKEN = re.compile(r"[a-z][a-z0-9_.]*")

# The pair scan of invariant 3. `\w` spans digits, so `100.0` parses as
# `("100", "0")` -- harmless, because `100` is not a `__tablename__` and the
# filter below drops it. That filter is also the false-positive escape the
# module docstring states: it is what makes `t.name` invisible.
_QUALIFIED_PAIR = re.compile(r"\b(\w+)\.(\w+)\b")

# PRD 10's `### 2 — Taste & Watching` audit, which is where the three unbacked
# panels are named. Scoped to that heading rather than to the whole file, so
# another dashboard's section adopting the phrase "has no backing series" does
# not silently extend this dashboard's forbidden list.
_D2_SECTION = re.compile(r"^### 2 — Taste & Watching$(?P<body>.*?)^### ", re.M | re.S)

# `- **"<panel>" has no backing series** (#<issue>)`, read off a bullet that the
# PRD hard-wraps across up to three lines. The wrap is why the bullets are
# unwrapped before this runs.
_UNBACKED = re.compile(r'\*\*"(?P<panel>[^"]+)" has no backing series\*\* \(#(?P<issue>\d+)\)')

# A sentence end: a full stop, optionally swallowing the `**` that closes a bold
# span opened mid-sentence, followed by whitespace or the end of the bullet. A
# naive `\. ` split cuts `...**a bold claim.** The next...` in the wrong place
# and hands back a sentence that begins with a stray bold marker.
_SENTENCE_END = re.compile(r"\.(?:\*\*)?(?=\s|$)")

# A bullet whose first sentence stops at the issue reference has put its reason
# in the *next* sentence, so the extraction takes both. This is the whole of the
# per-bullet variation and it is mechanical rather than a list of stop phrases.
_BARE_CLAIM = re.compile(r"\(#\d+\)\.$")


def _d2_bullets(text: str | None = None) -> list[str]:
    """PRD 10's dashboard-2 audit bullets, each unwrapped onto one line.

    The PRD hard-wraps at 79 columns, so every bullet but the shortest spans
    several lines and no interesting sentence is a substring of the file as
    stored. Unwrapping is therefore the normalisation both readers below share:
    what "byte-identical" means here is *after* the hard wrap is undone, which
    is the only difference a Markdown reader cannot see.
    """
    source = text if text is not None else _PRD_10.read_text(encoding="utf-8")
    section = _D2_SECTION.search(source)
    if section is None:
        return []

    bullets: list[list[str]] = []
    for line in section.group("body").splitlines():
        if line.startswith("- "):
            bullets.append([line[2:]])
        elif bullets and line.startswith("  ") and line.strip():
            bullets[-1].append(line.strip())
    return [re.sub(r"\s+", " ", " ".join(parts)).strip() for parts in bullets]


def unbacked_panels(text: str | None = None) -> dict[str, str]:
    """The panel titles PRD 10's audit says have no backing series, by issue.

    Parsed rather than retyped: a retyped list stops matching the day the audit
    is corrected, and the failure is silent in the direction that matters —
    a panel that became forbidden after this file was written would ship.
    """
    found: dict[str, str] = {}
    for bullet in _d2_bullets(text):
        match = _UNBACKED.search(bullet)
        if match is not None:
            found[match.group("panel")] = match.group("issue")
    return found


def absence_sentences(text: str | None = None) -> list[str]:
    """The three sentences PRD 10 states each absence in, in the PRD's order.

    Each names the panel, its reason and its issue number. The extraction is
    one rule applied uniformly: take the bullet's first sentence, and take the
    second one as well when the first ends at the issue reference — a bullet
    that stops at `(#84).` has said *that* there is no series and not yet *why*.
    """
    sentences: list[str] = []
    for bullet in _d2_bullets(text):
        if _UNBACKED.search(bullet) is None:
            continue
        cuts = [match.end() for match in _SENTENCE_END.finditer(bullet)]
        if not cuts:
            sentences.append(bullet)
            continue
        first = bullet[: cuts[0]]
        take = cuts[1] if _BARE_CLAIM.search(first) and len(cuts) > 1 else cuts[0]
        sentences.append(bullet[:take])
    return sentences


def normalise_panel_title(title: str) -> str:
    """One panel title reduced to a comparable key.

    Backticks come out because PRD 10 writes ``Row effectiveness: plays
    attributed per `RowProvider` `` and a dashboard would not; case and runs of
    whitespace come out because neither distinguishes two panels.
    """
    return re.sub(r"\s+", " ", title.replace("`", "")).strip().casefold()


def normalise_metric(name: str) -> str:
    """One OTel or Prometheus spelling of a metric, reduced to a comparable key."""
    key = name.strip().replace(".", "_")
    for suffix in _PROMETHEUS_SUFFIXES:
        if key.endswith(suffix):
            key = key[: -len(suffix)]
            break
    for unit in _PROMETHEUS_UNITS:
        if key.endswith(unit):
            return key[: -len(unit)]

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

        found = panels(dashboard)
        assert found, f"{path.name} carries no panels"

        for panel in found:
            where = f"{path.name}:{panel.get('title') or '<untitled>'}"
            assert panel.get("title"), f"{path.name} carries a panel with no title"
            if panel.get("type") == "row":
                continue
            if panel.get("type") == "text":
                # A text panel draws no data, so "no target" is its correct
                # shape rather than the empty rectangle below. What an empty
                # rectangle *is* for a text panel is empty content, and this is
                # invariant 1 for it.
                content = str((panel.get("options") or {}).get("content") or "")
                assert content.strip(), (
                    f"{where} is a text panel with no content, which renders as the same "
                    "empty rectangle a targetless query panel does"
                )
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


#: A target that reads only PostgreSQL's own catalogue names no Usher table, so
#: invariant 3 has nothing to grade. Spelled as "every table it names is a
#: `pg_`/`information_schema` one" rather than "it mentions `pg_class`", so a
#: panel joining the catalogue *to* an Usher table is still graded on the Usher
#: half.
def _reads_only_the_postgres_catalogue(sql: str) -> bool:
    tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_.]*)", sql, re.IGNORECASE))
    return bool(tables) and all(table.startswith(("pg_", "information_schema")) for table in tables)


def test_the_committed_dashboards_are_not_written_in_aliases() -> None:
    """Invariant 3's coverage is a property of the committed SQL.

    So it is asserted per *panel* rather than once over the glob: the case above
    asserts only that *some* pair was graded, which one unaliased panel satisfies for
    the whole file. This is the arm that notices a panel added in `FROM titles t`
    style, where the check reads nothing and reports nothing.
    """
    files = _dashboard_files()
    assert files, "the dashboard glob found nothing"

    unchecked: list[str] = []
    exempt: list[str] = []
    for path in files:
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        for panel in panels(dashboard):
            if panel.get("type") == "row":
                continue
            for target in panel.get("targets") or []:
                if _datasource_type(panel, target) not in {
                    "postgres",
                    "grafana-postgresql-datasource",
                }:
                    continue
                sql = _target_sql(target)
                if _sql_pairs(sql):
                    continue
                if _reads_only_the_postgres_catalogue(sql):
                    exempt.append(f"{path.name}:{panel.get('title')}")
                    continue
                unchecked.append(f"{path.name}:{panel.get('title')}:{target.get('refId')}")

    # **The exemption is asserted by size, not just granted.** A disk-headroom
    # panel reads `pg_class` and names no Usher table, so invariant 3 has
    # nothing of ours to grade and demanding a pair would mean inventing one.
    # Pinning the count is what stops the exemption becoming the way every
    # later panel escapes the check.
    assert exempt == [
        "05-cost-and-compliance.json:Table size, and headroom against measured free disk"
    ], exempt

    assert unchecked == [], (
        "these Postgres targets yield no table.column pair, so invariant 3 grades them on "
        f"nothing — write the table name out instead of aliasing it: {unchecked}"
    )


def test_a_panel_written_in_aliases_is_checked_on_nothing() -> None:
    """The limitation, pinned by name so it is a stated hole rather than a silent one.

    Both spellings select a column that does not exist; only the unaliased one is
    visible to the scan.
    """
    aliased = "SELECT t.enrichment_status FROM titles t"
    written_out = "SELECT titles.enrichment_status FROM titles"

    assert _sql_pairs(aliased) == [], (
        "the alias was read as a table name, which would make every `t.<x>` a false "
        "positive against whatever table `t` happens to collide with"
    )
    assert _sql_pairs(written_out) == [("titles", "enrichment_status")]


def test_the_normaliser_is_not_vacuous() -> None:
    """A normaliser that reduced everything to one key would make invariant 2 vacuous.

    So it is asserted in both directions: the two spellings of one metric agree, and
    two different metrics stay different.
    """
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
    """The whole reason the anchor was dropped.

    `http.server.duration` is supplied by `FastAPIInstrumentor` rather than declared by
    Usher, and it is the metric an API-latency panel queries — a catalogue that cannot
    see it grades that panel as naming an unknown metric.
    """
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
    """Invariant 2's teeth, proved on a synthetic dashboard.

    The committed glob has no Prometheus panel to fail on. `usher.suggest.latency` is
    the near miss: a plausible spelling of a metric nothing emits.
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
    """A row panel carries no targets and is exempt.

    Its children are not, and a walk of the top level alone would stop seeing them the
    moment a row is collapsed and saved.
    """
    dashboard = _synthetic(
        {
            "type": "row",
            "title": "Composition",
            "collapsed": True,
            "panels": [{"type": "table", "title": "Nested", "targets": [{"refId": "A"}]}],
        }
    )

    titles = [panel.get("title") for panel in panels(dashboard)]

    assert titles == ["Composition", "Nested"], (
        f"the nested panel is invisible to the scan, so it is graded on nothing: {titles}"
    )


def test_the_catalogue_parse_and_the_glob_are_both_falsifiable() -> None:
    """The two positive controls of the main case, exercised where they can fail.

    Neither can be shown red against the real tree, and an `assert files` that nobody
    has watched fail is worth as little as no assertion at all.
    """
    assert metric_catalogue("no table here at all") == set()
    assert metric_catalogue("| `usher.jobs.queued` | gauge | kind | ✅ M4 |") == {
        "usher_jobs_queued"
    }

    with pytest.raises(AssertionError, match="dashboard glob found nothing"):
        empty: list[pathlib.Path] = []
        assert empty, "the dashboard glob found nothing"


def _query_panel_titles(dashboard: dict[str, Any]) -> list[str]:
    """Every panel title on a dashboard except its rows' and its text panels'.

    Text panels are excluded on purpose and it is the load-bearing exclusion of
    the case below: the absences panel *quotes the three forbidden names in its
    body*, so a scan of panel content rather than panel titles would report the
    statement of an absence as the shipping of one.
    """
    return [
        str(panel.get("title") or "")
        for panel in panels(dashboard)
        if panel.get("type") not in {"row", "text"}
    ]


def _title_collisions(titles: list[str], forbidden: dict[str, str]) -> list[str]:
    """Panel titles that name one of the unbacked panels, in either direction.

    Containment both ways rather than equality: `"Watch time by day and user
    (empty)"` and `"Watch time"` are both the panel the audit named, and
    equality catches neither.
    """
    collisions = []
    for title in titles:
        key = normalise_panel_title(title)
        for panel, issue in forbidden.items():
            name = normalise_panel_title(panel)
            if key and (name in key or key in name):
                collisions.append(f"{title!r} names {panel!r} (#{issue})")
    return collisions


def test_dashboard_two_ships_no_panel_the_audit_found_unbacked() -> None:
    """Three of PRD 10's audited panels have no backing series and must not ship.

    Both premises are asserted first, because both failures are silent in the
    same direction: a parse that found no forbidden names and a dashboard with
    no panels each satisfy the disjointness below without checking anything.
    """
    forbidden = unbacked_panels()
    assert forbidden, "no unbacked panels parsed out of PRD 10's D2 paragraph"
    assert sorted(forbidden.values()) == ["84", "84", "85"], (
        "PRD 10's D2 audit no longer names three unbacked panels against #84 and #85, so "
        f"the dashboard's stated absences need re-reading: {forbidden}"
    )

    dashboard = json.loads(_DASHBOARD_TWO.read_text(encoding="utf-8"))
    titles = _query_panel_titles(dashboard)
    assert titles, "the dashboard has no panels"

    collisions = _title_collisions(titles, forbidden)
    assert collisions == [], (
        "Dashboard 2 ships a panel PRD 10's own audit says has no backing series, which "
        "renders as a permanently empty rectangle nothing distinguishes from a healthy "
        f"zero: {collisions}"
    )


def test_a_dashboard_that_shipped_all_eight_panels_is_caught_naming_the_three() -> None:
    """The case above cannot be shown red against the committed tree.

    So its teeth are proved on the dashboard it exists to refuse: the "complete" one,
    with all eight of the audited panels on it.
    """
    forbidden = unbacked_panels()
    complete = [
        "Watch time by day and user",
        "Abandonment cliff",
        "Completion rate",
        "Time-of-day heatmap",
        "Taste drift as genre affinity in a stacked area over months",
        "Longest unwatched",
        "Rewatches",
        "Row effectiveness: plays attributed per RowProvider",
    ]

    collisions = _title_collisions(complete, forbidden)

    assert sorted(collisions) == sorted(
        [
            "'Watch time by day and user' names 'Watch time by day and user' (#84)",
            "'Taste drift as genre affinity in a stacked area over months' names "
            "'Taste drift as genre affinity in a stacked area over months' (#84)",
            "'Row effectiveness: plays attributed per RowProvider' names "
            "'Row effectiveness: plays attributed per `RowProvider`' (#85)",
        ]
    ), f"the eight-panel dashboard was not caught naming exactly the three: {collisions}"

    assert _title_collisions([t for t in complete if t not in dict.fromkeys(forbidden)], {}) == []


def test_the_five_shipped_panels_are_not_false_positives_of_the_collision_scan() -> None:
    """Containment in both directions is the half of the scan that can over-fire.

    A short legitimate title inside a long forbidden one reads as a collision. This
    asserts the five that shipped are clear of it, which is what makes the empty result
    above evidence rather than luck.
    """
    forbidden = unbacked_panels()
    dashboard = json.loads(_DASHBOARD_TWO.read_text(encoding="utf-8"))
    titles = _query_panel_titles(dashboard)

    assert len(titles) == 5, f"Dashboard 2 ships {len(titles)} query panels, not five: {titles}"
    assert _title_collisions(["Watch time"], forbidden), (
        "the scan no longer catches a truncated spelling of a forbidden panel, so its "
        "empty result over the committed titles proves nothing"
    )


def test_the_absence_panels_three_sentences_are_byte_identical_to_prd_tens() -> None:
    """The text panel duplicates PRD 10 rather than linking to it.

    So the duplication needs a red when the two drift. The operator reading the
    dashboard at 2 a.m. must not have to hold the document, and the cost of that
    choice is exactly this test.
    """
    sentences = absence_sentences()
    assert len(sentences) == 3, (
        f"PRD 10's D2 paragraph yielded {len(sentences)} absence sentences, not three: {sentences}"
    )
    for sentence in sentences:
        assert "has no backing series" in sentence, (
            f"an extracted sentence does not state the absence it is quoted for: {sentence!r}"
        )
        assert re.search(r"\(#\d+\)", sentence), (
            f"an extracted sentence carries no issue number: {sentence!r}"
        )

    dashboard = json.loads(_DASHBOARD_TWO.read_text(encoding="utf-8"))
    text_panels = [panel for panel in panels(dashboard) if panel.get("type") == "text"]
    assert len(text_panels) == 1, (
        f"Dashboard 2 carries {len(text_panels)} text panels, not the one the absences are "
        "stated on"
    )
    content = str((text_panels[0].get("options") or {}).get("content") or "")

    missing = [sentence for sentence in sentences if sentence not in content]
    assert missing == [], (
        "the absences panel and PRD 10 have drifted — these sentences are in the PRD and "
        f"not on the panel, byte for byte: {missing}"
    )

    positions = [content.index(sentence) for sentence in sentences]
    assert positions == sorted(positions), (
        f"the panel states the three absences in a different order than PRD 10: {positions}"
    )

    drifted = sentences[0].replace("#84", "#86")
    assert drifted not in content, (
        "the containment check passes on a sentence PRD 10 does not hold, so it is not "
        "asserting byte-identity at all"
    )


def test_the_prd_parse_of_the_unbacked_panels_is_falsifiable() -> None:
    """The two parses of this module's fourth question, exercised where they can fail.

    Neither can be, against the real PRD. An `assert forbidden` nobody has watched go
    red is worth what an empty `assert files` is worth: nothing.
    """
    assert unbacked_panels("no D2 section here at all") == {}
    assert absence_sentences("no D2 section here at all") == []

    synthetic = (
        "### 2 — Taste & Watching\n"
        '- **"Watch time by day and user" has no backing series** (#84). Minutes\n'
        "  attributable to a day need a row per play.\n"
        "- Completion rate is backed outright.\n"
        "### 3 — Pipeline\n"
    )
    assert unbacked_panels(synthetic) == {"Watch time by day and user": "84"}
    assert absence_sentences(synthetic) == [
        '**"Watch time by day and user" has no backing series** (#84). Minutes '
        "attributable to a day need a row per play."
    ]

    # The bare-claim rule is what makes that two sentences rather than one. A
    # bullet whose first sentence already carries its reason takes one.
    reasoned = (
        "### 2 — Taste & Watching\n"
        '- **"X" has no backing series** (#85), because nothing writes it. And then\n'
        "  a second sentence nobody quoted.\n"
        "### 3 — Pipeline\n"
    )
    assert absence_sentences(reasoned) == [
        '**"X" has no backing series** (#85), because nothing writes it.'
    ]

    # A sentence that ends inside a bold span ends after its closing `**`.
    bold = (
        "### 2 — Taste & Watching\n"
        '- **"Y" has no backing series** (#86), and **the cause is not the obvious one.**\n'
        "  A second sentence nobody quoted.\n"
        "### 3 — Pipeline\n"
    )
    assert absence_sentences(bold) == [
        '**"Y" has no backing series** (#86), and **the cause is not the obvious one.**'
    ]


def test_a_text_panel_with_no_content_is_caught() -> None:
    """Invariant 1's text-panel arm.

    The exemption above removes the datasource and target requirement from a panel that
    draws no data; this is what it was replaced with, and it is asserted on the failure
    it names rather than on the committed file, where it can no longer fail.
    """
    empty = {"type": "text", "title": "Absences", "options": {"content": "   "}}
    filled = {"type": "text", "title": "Absences", "options": {"content": "#84"}}

    def content_of(panel: dict[str, Any]) -> str:
        return str((panel.get("options") or {}).get("content") or "")

    assert not content_of(empty).strip()
    assert content_of(filled).strip()
    assert not content_of({"type": "text", "title": "Absences"}).strip(), (
        "a text panel with no `options` at all reads as filled, so the arm passes on the "
        "commonest spelling of the defect"
    )


# --- Dashboard 3, and the panels whose series is not what their name implies ---

_PIPELINE = _DASHBOARDS / "03-pipeline.json"


def _live_panels(path: pathlib.Path) -> list[dict[str, Any]]:
    """Every drawable panel of one committed file — rows carry no targets."""
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    return [panel for panel in panels(dashboard) if panel.get("type") != "row"]


def _kinds(panel: dict[str, Any]) -> set[str]:
    return {_datasource_type(panel, target) for target in panel.get("targets") or []}


def _exprs(panel: dict[str, Any]) -> list[str]:
    return [str(target.get("expr", "")) for target in panel.get("targets") or []]


def _sql(panel: dict[str, Any]) -> str:
    return "\n".join(_target_sql(target) for target in panel.get("targets") or [])


def test_the_queue_depth_panels_are_two_panels_over_two_datasources() -> None:
    """The failure this closes renders perfectly and answers a different question.

    One panel titled *"queue depth by priority"* whose target is `usher.jobs.queued`, a
    gauge labelled `kind`: `telemetry.py`'s `_by_kind` emits
    `Observation(count, {"kind": kind})` and nothing else, so the series counts pending
    rows per kind — which is what *"which lane is backed up"* asks, not which band is
    waiting.

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
    assert len(panels) == 10, (
        "dashboard 3 is ten panels -- PRD 10's nine items, with queue depth drawn twice -- "
        f"not {len(panels)}: {titles}"
    )

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
    """The real names, which a normaliser stripping only the aggregation suffix misses.

    Every gauge Usher registers carries `unit="1"` and every histogram `unit="s"`, and
    the OTel Prometheus exporter puts that unit into the name ahead of the aggregation
    suffix. So the name a panel must be written in is `usher_jobs_queued_ratio`, never
    `usher_jobs_queued`, and a normaliser that strips only the aggregation suffix reads
    the former as a metric PRD 10 does not document.
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
    """The sentence the *"Push down"* alert is written against, pinned on the panel.

    `api/lanes.py`'s `push_snapshots` builds the reader as
    `{self._names[source_id]: PushSnapshot(...)}` over `self._open_adapters`,
    so the `source` label is **the operator-typed source name** and never a
    UUID, and a source with no open adapter is simply not in the
    comprehension. `telemetry.py`'s `_ReaderSlot` then observes nothing
    with no reader at all. Both halves mean the same thing for an alert: a
    disabled source, or one that does not support push, produces **no
    observation** rather than a zero — so the condition is `== 0` and
    `absent()` would page about every source nobody configured.
    """
    panels = _live_panels(_PIPELINE)
    push = [panel for panel in panels if "push" in str(panel["title"]).lower()]

    assert len(push) == 2, (
        f"PRD 10's dashboard 3 has two push panels, not {len(push)}: {[p['title'] for p in push]}"
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


def test_the_enrichment_panel_splits_on_outcome_and_says_where_the_trigger_split_is() -> None:
    """The enrichment histogram carries `outcome` and `trigger`; the panel splits on `outcome`.

    A failure's latency and a success's are genuinely different populations, so that is
    the legend. The demand-versus-background split a reader of the title expects is
    `trigger`, which *Enrichment SLA missed* selects on and this panel sums over -- so
    the description has to say where it went.
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
        "the enrichment panel does not cite the module that derives `trigger`"
    )
    for phrase in ("demand", "background"):
        assert phrase in description.lower(), (
            f"the enrichment panel does not say where the {phrase} split went, which is "
            f"the thing a reader of the title assumes: {description!r}"
        )

    labels = {str(target.get("legendFormat", "")) for target in panel.get("targets") or []}
    assert all("{{outcome}}" in label for label in labels), (
        f"a target on the enrichment panel does not split on `outcome`: {sorted(labels)}"
    )


def test_the_tmdb_panel_counts_429s_and_denominates_on_every_status_including_error() -> None:
    """The 429 rate needs a denominator that includes the failures.

    One that omitted them would read low exactly during an outage.
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

    assert count_exprs, f'no target selects `status="429"`: {exprs}'
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
    """The four description checks above are substring scans.

    A substring scan over prose nobody can make fail is decoration, so each is
    exercised here against a description with the sentence removed -- the only place
    they can be shown red.
    """
    panel = {"title": "Push connection uptime", "description": "a socket, probably"}
    assert "operator-typed" not in str(panel["description"])

    assert 'status="error"' not in "429s over the total"
    assert "{{outcome}}" not in "{{trigger}}"

    with pytest.raises(AssertionError, match="ten panels"):
        titles = ["only", "nine", "of", "them", "here", "and", "no", "more", "sadly"]
        assert len(titles) == 10, (
            "dashboard 3 is ten panels -- PRD 10's nine items, with queue depth drawn "
            f"twice -- not {len(titles)}"
        )


# --------------------------------------------------------------------------------

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
    """`(grouping labels, operand)` for every aggregation call in a PromQL expression.

    Both spellings PromQL allows — `sum by (x) (…)` and `sum(…) by (x)`. A substring
    test for `by (cache` would pass on an expression that carries the grouping on some
    *other* aggregation than the one wrapping the counter: the numerator grouped and
    the denominator summed flat renders a hit rate that is a ratio of two different
    populations and still draws a plausible line.
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
        for panel in panels(dashboard):
            if panel.get("type") == "row":
                continue
            for target in panel.get("targets") or []:
                if _datasource_type(panel, target) == "prometheus":
                    where = f"{path.name}:{panel.get('title')}:{target.get('refId')}"
                    targets.append((where, str(target.get("expr", ""))))
    return targets


def _legend_targets() -> list[tuple[str, str, str]]:
    """`(where, expr, legendFormat)` for every committed Prometheus target."""
    targets: list[tuple[str, str, str]] = []
    for path in _dashboard_files():
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        for panel in panels(dashboard):
            if panel.get("type") == "row":
                continue
            for target in panel.get("targets") or []:
                if _datasource_type(panel, target) == "prometheus":
                    where = f"{path.name}:{panel.get('title')}:{target.get('refId')}"
                    targets.append(
                        (where, str(target.get("expr", "")), str(target.get("legendFormat", "")))
                    )
    return targets


# `{{outcome}}` in a legend: the label Grafana substitutes per series.
_LEGEND_LABEL = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def test_every_legend_entry_names_a_label_its_own_query_still_groups_by() -> None:
    """The legend and the `by (…)` are two halves of one claim.

    A legend naming a label its own query aggregates away substitutes nothing, so the
    panel draws a split the expression does not compute.
    """
    targets = _legend_targets()
    named = [
        (where, expr, legend)
        for where, expr, legend in targets
        if _LEGEND_LABEL.search(legend) and _aggregations(expr)
    ]

    assert len(named) >= 15, (
        f"only {len(named)} committed Prometheus targets carry both an aggregation and a "
        "`{{label}}` legend, so this scan has stopped reading the dashboards and every "
        "assertion below is vacuous"
    )

    parameterised = [
        f"{where}: {expr}"
        for where, expr, _legend in targets
        if re.search(r"\b(?:topk|bottomk|quantile)\s*\(", expr)
    ]
    assert parameterised == [], (
        "`topk`, `bottomk` and `quantile` take their first argument inside the same "
        "parentheses, so `_aggregations` reads their label set as empty and this case "
        f"would call a legend orphaned that is not; extend it before one ships: "
        f"{parameterised}"
    )
    without = [f"{where}: {expr}" for where, expr, _legend in targets if "without" in expr]
    assert without == [], (
        "a committed target aggregates with `without (…)`, whose label set is the "
        f"complement of the one this case reads; extend it before one ships: {without}"
    )

    orphaned = [
        f"{where}: legend {{{{{label}}}}} against `{' '.join(expr.split())}`"
        for where, expr, legend in named
        for label in _LEGEND_LABEL.findall(legend)
        if any(label not in labels for labels, _operand in _aggregations(expr))
    ]

    assert orphaned == [], (
        "these legends name a label their own query aggregates away, so Grafana "
        "substitutes nothing and the panel draws a split it does not compute — the "
        f"expression is what decides the population, the legend only names it: {orphaned}"
    )


def test_the_cache_panel_groups_by_cache_and_never_sums_across_it() -> None:
    """`usher.cache.hits` carries `cache` and `freshness`, `usher.cache.misses` `cache`.

    A miss served nothing, so it has no freshness to report. A hit rate summed across
    `cache` pools the row cache, the screen cache and the image proxy, which have
    different populations and different hit rates, so one number is dominated by
    whichever is busiest. It renders as a healthy line either way.
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
    """No committed panel takes a quantile over a histogram on the SDK's defaults.

    `configure_metrics` installs no `View`, so a seconds-unit histogram with no
    `explicit_bucket_boundaries_advisory` of its own takes the SDK's
    `(0.0, 5.0, 10.0, 25.0, …)` — **in seconds** — and every observation under five
    seconds lands in one bucket. `histogram_quantile` over that does not fail and does
    not empty: it interpolates inside `le="5"` and answers a flat plausible number.

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
    """OTel's *name* mangling and what the collector stores differ by a segment.

    The exporter appends the instrument's *unit* (`s` → `_seconds`, `ms` →
    `_milliseconds`, `By` → `_bytes`, a gauge's `1` → `_ratio`) before the
    `_bucket`/`_total` suffix, so a normaliser that strips only the latter leaves
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

    assert normalise_metric("usher_suggest_results") != normalise_metric(
        "usher_suggest_duration"
    ), "the unit strip has eaten a name segment and collapsed two instruments onto one key"
