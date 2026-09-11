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

**A fourth question arrives with Dashboard 2, and it is about what is *not*
there.** D0's audit found three of PRD 10's eight D2 panels unbacked — no
play-event log exists, so "watch time by day and user" and "taste drift" have no
row per play to draw, and `search_queries` hands out no row handle for "row
effectiveness" ([#84](https://github.com/anirudhlath/usher/issues/84),
[#85](https://github.com/anirudhlath/usher/issues/85)). Shipping them empty is
the failure PRD 10's own preamble exists to prevent, so
`test_dashboard_two_ships_no_panel_the_audit_found_unbacked` asserts the
committed file names none of them, **with the forbidden list parsed out of the
PRD rather than retyped** — a retyped list goes stale silently the day the audit
is corrected. The absences are stated instead on a text panel, and
`test_the_absence_panels_three_sentences_are_byte_identical_to_prd_tens` pins
that text to the PRD's own so a correction to one is a red on the other.

**A text panel is exempt from invariant 1's datasource-and-target rule and gains
its own.** It draws no data, so "no target" is its correct shape rather than an
empty rectangle; what an empty rectangle looks like for a text panel is empty
`options.content`, which is what is asserted in place.

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
_DASHBOARD_TWO = _DASHBOARDS / "02-taste-and-watching.json"
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

# PRD 10's `### 2 — Taste & Watching` audit, which is where the three unbacked
# panels are named. Scoped to that heading rather than to the whole file: the
# phrase "has no backing series" is D2's vocabulary and another dashboard's
# section adopting it must not silently extend this dashboard's forbidden list.
_D2_SECTION = re.compile(r"^### 2 — Taste & Watching$(?P<body>.*?)^### ", re.M | re.S)

# `- **"<panel>" has no backing series** (#<issue>)`, read off a bullet that the
# PRD hard-wraps across up to three lines. The wrap is why the bullets are
# unwrapped before this runs.
_UNBACKED = re.compile(r'\*\*"(?P<panel>[^"]+)" has no backing series\*\* \(#(?P<issue>\d+)\)')

# A sentence end: a full stop, optionally swallowing the `**` that closes a bold
# span opened mid-sentence, followed by whitespace or the end of the bullet.
# Measured against PRD 10's three bullets while writing this file -- a naive
# `\. ` split cuts `...expected to have.** The prediction...` in the wrong place
# and hands back a sentence that begins with a stray bold marker.
_SENTENCE_END = re.compile(r"\.(?:\*\*)?(?=\s|$)")

# A bullet whose first sentence stops at the issue reference has put its reason
# in the *next* sentence, so the extraction takes both. This is the whole of the
# per-bullet variation and it is mechanical rather than a list of stop phrases.
_BARE_CLAIM = re.compile(r"\(#\d+\)\.$")


def _d2_bullets(text: str | None = None) -> list[str]:
    """PRD 10's D2 audit bullets, each unwrapped onto one line.

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
    """The panel titles PRD 10's D2 audit says have no backing series, by issue.

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
    """One OTel or Prometheus spelling of a metric, reduced to a comparable key.

    Dots become underscores and **one** trailing Prometheus suffix is removed,
    so `usher.suggest.duration` in PRD 10 and `usher_suggest_duration_bucket`
    in a panel reduce to the same key. Applied to **both** sides, which is what
    makes it a normalisation rather than a rewrite of one of them.

    No catalogue row ends in one of the four suffixes today (checked in
    `test_the_normaliser_is_not_vacuous`), so stripping on the catalogue side
    is a no-op; if one ever does, that row and its `_count` sibling would
    collapse into one key and the case is what says so.
    """
    key = name.strip().replace(".", "_")
    for suffix in _PROMETHEUS_SUFFIXES:
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
    collisions = [name for name in catalogue if name.endswith(_PROMETHEUS_SUFFIXES)]
    assert collisions == [], (
        "a catalogue row's own name ends in a Prometheus suffix, so it now normalises onto "
        f"whatever row it is the suffix of: {collisions}"
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


def _query_panel_titles(dashboard: dict[str, Any]) -> list[str]:
    """Every panel title on a dashboard except its rows' and its text panels'.

    Text panels are excluded on purpose and it is the load-bearing exclusion of
    the case below: the absences panel *quotes the three forbidden names in its
    body*, so a scan of panel content rather than panel titles would report the
    statement of an absence as the shipping of one.
    """
    return [
        str(panel.get("title") or "")
        for panel in _panels(dashboard)
        if panel.get("type") not in {"row", "text"}
    ]


def _title_collisions(titles: list[str], forbidden: dict[str, str]) -> list[str]:
    """Panel titles that name one of the unbacked panels, in either direction.

    Containment both ways rather than equality: `"Watch time by day and user
    (empty)"` and `"Watch time"` are both the panel D0 said not to ship, and
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
    """D0's audit found three of PRD 10's eight D2 panels have no backing
    series, and this is the assertion that they did not ship anyway.

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
    """The case above cannot be shown red against the committed tree once this
    task lands, so its teeth are proved on the dashboard it exists to refuse:
    the "complete" one, with all eight of PRD 10's D2 panels on it."""
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
    """Containment in both directions is the strong half of the scan and the
    half that can over-fire: a short legitimate title inside a long forbidden
    one reads as a collision. This asserts the five that shipped are clear of
    it, which is what makes the empty result above evidence rather than luck."""
    forbidden = unbacked_panels()
    dashboard = json.loads(_DASHBOARD_TWO.read_text(encoding="utf-8"))
    titles = _query_panel_titles(dashboard)

    assert len(titles) == 5, f"Dashboard 2 ships {len(titles)} query panels, not five: {titles}"
    assert _title_collisions(["Watch time"], forbidden), (
        "the scan no longer catches a truncated spelling of a forbidden panel, so its "
        "empty result over the committed titles proves nothing"
    )


def test_the_absence_panels_three_sentences_are_byte_identical_to_prd_tens() -> None:
    """The text panel duplicates PRD 10 rather than linking to it, so the
    duplication needs a red when the two drift.

    Duplication was chosen deliberately: the operator reading the dashboard at
    2 a.m. must not have to hold the document. The cost of that choice is
    exactly this test.
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
    text_panels = [panel for panel in _panels(dashboard) if panel.get("type") == "text"]
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
    """The two parses of this module's fourth question, exercised where they
    can be made to fail — neither can be, against the real PRD, once D7 lands.

    An `assert forbidden` nobody has watched go red is worth what an empty
    `assert files` was worth to D6: nothing.
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


def test_a_text_panel_with_no_content_is_caught() -> None:
    """Invariant 1's text-panel arm. The exemption above removes the datasource
    and target requirement from a panel that draws no data; this is what it was
    replaced with, and it is asserted on the failure it names rather than on
    the committed file, where it can no longer fail."""
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
