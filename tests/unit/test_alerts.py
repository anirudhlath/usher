"""An alert can be green, valid, loaded, and unable to fire, and this module is
the checks that close the ways it gets there.

[PRD 10](../../docs/prd/10-telemetry-and-dashboards.md)'s `## Alerts` table
names seven and opens *"Kept few, so they mean something."* `dashboards/alerts/
usher.yml` is that table as a Prometheus rule file, and it sits under
`dashboards/` for `provisioning/dashboards.yml`'s reason: the rules are written
against the instruments in `src/usher/` and version with them, while the
Prometheus that evaluates them is in `~/code/observability/`. So nothing in the
evaluation path is available to a unit test either, and what *is* checkable is
the file.

**A rule file has one failure mode a dashboard does not, and it is silent in
the opposite direction.** A panel that names a metric nobody stores draws an
empty rectangle, which somebody eventually looks at. A rule that names a metric
nobody stores evaluates to an empty vector, which is indistinguishable from
*healthy* -- it is not merely unhelpful, it is the alert saying "all clear"
forever. So the checks here are weighted toward "can this expression ever select
anything":

1. **Every metric token is in PRD 10's catalogue**, reusing D6's normaliser
   (`test_dashboards.normalise_metric`) rather than a second copy of it, so a
   correction to the catalogue parse is a correction to both files.

2. **Every metric token is written in the spelling Prometheus actually
   stores**, which invariant 1 cannot see: `usher_jobs_queued` normalises into
   the catalogue perfectly and selects nothing, because the collector appends
   the instrument's *unit* -- the real name is `usher_jobs_queued_ratio`. This
   is D8's finding turned on the rule file, and it is the check that matters
   most here for the reason above. The stored spelling is **derived from the
   declarations** (`create_observable_gauge(..., unit="1")`) rather than
   retyped, and the derivation is pinned against the names this host's
   Prometheus holds.

3. **The name sets agree with PRD 10's table in both directions**, across
   **both** rule files. A rule the PRD does not name falsifies *"kept few, so
   they mean something"* as surely as an alert with no rule. That case is
   `xfail(strict=True)` today and names which task owes which rule.

4. **The two spellings that are a decision get a case each.** "Ingest stalled"
   must reach a lane that has *never settled a job*, and "Push down" must fire
   on a zero and not on an absence. Both are one PromQL operator wide and both
   are invisible to every other check in this file.

5. **The Postgres rule's own three ways of reading healthy forever.** PRD 10's
   seventh alert is *Cost anomaly*, and it has no metric to name: the same
   first principle that puts spend on `llm_calls` refuses a `usher.llm.*`
   series, so the rule is SQL evaluated by Grafana and lives in
   `dashboards/alerts/grafana/usher.yml`. Its analogue of invariant 1 is that
   every `table.column` it names is one `Base.metadata` holds (D6's invariant
   3, turned on a rule); its analogue of invariant 2 is the **file's
   location** -- one directory below the Prometheus rule file, because
   `rule_files: [/etc/prometheus/rules/*.yml]` is not recursive and a sibling
   would take the other five rules down with it; and its own is that a
   Grafana table frame yields **one** numeric column, since every numeric
   column becomes a series the threshold judges.

⚠️ **What this module cannot check, stated rather than implied.** It does not
evaluate PromQL. Whether `increase()` over a gauge answers what the rule's
comment says it answers, and whether a `for:` window is reachable, were measured
with `promtool` and against this host's Prometheus; the measurements are written
down in `dashboards/README.md` beside the firing each rule was put through, and
nothing here re-derives them. What *is* here is the shape those measurements
justify, so that a later edit which quietly changes the shape is red.
"""

import ast
import pathlib
import re
from typing import Any

import yaml

from tests.unit.grafana import panel_titles
from tests.unit.test_dashboards import (
    _RANGE,
    _aggregations,
    _balanced,
    _declared_histograms,
    _metric_tokens,
    _sql_pairs,
    metric_catalogue,
    normalise_metric,
)
from usher.db.base import Base

_ROOT = pathlib.Path(__file__).parents[2]
_ALERTS = _ROOT / "dashboards" / "alerts" / "usher.yml"
_GRAFANA_ALERTS = _ROOT / "dashboards" / "alerts" / "grafana" / "usher.yml"
_PRD_10 = _ROOT / "docs" / "prd" / "10-telemetry-and-dashboards.md"
_PRD_08 = _ROOT / "docs" / "prd" / "08-operations.md"
_DASHBOARD_THREE = _ROOT / "dashboards" / "03-pipeline.json"
_DASHBOARD_FIVE = _ROOT / "dashboards" / "05-cost-and-compliance.json"

# PRD 10's `## Alerts` section, scoped to the heading rather than to the file.
# The table is the last thing in the document today, so the lookahead has to
# accept the end of the file as well as the next `## ` -- a `(?=^## )` alone
# matches nothing and hands back an empty section, which would make every
# assertion below vacuous. `test_the_prd_alert_table_parse_is_falsifiable` is
# what says so.
_ALERTS_SECTION = re.compile(r"^## Alerts$(?P<body>.*?)(?=^## |\Z)", re.M | re.S)

# A two-column table row. The header and the `|---|---|` separator are dropped
# by the filter in `prd_alerts` rather than by the pattern: a pattern that
# excluded them would also exclude an alert someone named "Alert", and the
# filter is readable where a negative lookahead is not.
_TABLE_ROW = re.compile(r"^\|(?P<alert>[^|]+)\|(?P<condition>[^|]+)\|\s*$", re.M)

# 🔴 **The ledger, and it is empty because D13 was the last.** This map named
# which task owed which alert, and the `xfail(strict=True)` that used to sit on
# the bidirectional check below read its entries out in the failure message.
# D12 landed two, D14 landed *Cost anomaly* -- in the *other* rule file, because
# it has no metric to name -- and D13 landed *Disk projection*, which had no
# series at all and therefore ships as three rules in two engines.
#
# **The marker is gone rather than emptied**, which is the point of `strict`: a
# strict xfail that passes is a failure, so the last task had to come back and
# turn the case into a plain assertion. It is kept as an empty dict rather than
# deleted because it is the vocabulary a future debt would be written in, and
# because the failure message below still reads it -- an alert PRD 10 names with
# no rule now renders as `(no task)`, which is the honest answer once nobody is
# assigned.
_OWED: dict[str, str] = {}

# 🔴 **The one series in this file that Usher does not emit, and the only
# exemption from the two catalogue checks below.**
#
# *Disk projection* is the single alert in PRD 10's table whose subject Usher
# has no instrument for and cannot have one: `telemetry.py`'s own register
# records why an observable callback cannot query Postgres, and a `du` over
# `image_cache_dir` would be disk I/O on a lane for one consumer. The disk
# facts come from the stack instead.
#
# **Measured, not assumed.** The OTel collector's `hostmetrics` receiver was
# run against this host on 2026-09-11 -- the shared stack's own collector image
# (`otel/opentelemetry-collector-contrib:0.158.0`) and its own
# `prometheusremotewrite` exporter, pointed at a throwaway Prometheus -- and the
# name it stores is `system_filesystem_usage_bytes`, carrying `mountpoint`,
# `device`, `type`, `mode` and **`state` in {free, used, reserved}**. Free space
# is a *label value*, not a name: `usher_disk_free_bytes` is a name nothing on
# this host produces, and a rule naming it would be D11's failure exactly --
# parsed, loaded, evaluated empty, healthy forever.
#
# The exemption is a frozenset of one and is asserted by name and by size in
# `test_the_stack_series_exemption_is_one_measured_name_and_not_a_blanket`,
# because an exemption nobody counts is how every later rule escapes the check.
_MEASURED_STACK_SERIES = frozenset({"system_filesystem_usage_bytes"})

# `### Resource envelope` in PRD 08 -- scoped to the heading, and with the same
# end-of-file lookahead `_ALERTS_SECTION` needs, for the same reason.
_RESOURCE_SECTION = re.compile(r"^### Resource envelope$(?P<body>.*?)(?=^#{2,3} |\Z)", re.M | re.S)

# A number with a byte unit attached. Unanchored numbers (`1,272,367 titles`,
# `768`, `50 s`) are deliberately not figures: this parse exists to forbid a
# *byte threshold*, and a table full of counts would make the prohibition match
# everything.
_BYTE_FIGURE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(B|bytes|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\b")

# **Both readings of every ambiguous unit**, because the table is prose and
# "5 GB" in prose means 5e9 to one writer and 2**30 * 5 to another. A
# prohibition that guessed would be a prohibition half the spellings walk
# through.
_DECIMAL = {"B": 1, "bytes": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12}
_BINARY = {"B": 1, "bytes": 1, "KB": 2**10, "MB": 2**20, "GB": 2**30, "TB": 2**40}
_EXPLICIT = {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "TiB": 2**40}

# Every integer literal in a PromQL expression. `\b` on both sides, so a digit
# embedded in an identifier is not a literal; the range vectors are stripped
# before this runs, so `[7d]` contributes no `7` either. What is left is the
# numbers somebody typed as numbers -- `14`, `86400`, `0` in the rules here.
_INTEGER_LITERAL = re.compile(r"\b\d+\b")

# The instrument factories `src/usher/` calls, mapped to how the OTel
# collector's Prometheus translation renders the result. **Measured off this
# host's Prometheus on 2026-09-11** (76 `usher_`/`http_` names under
# `/api/v1/label/__name__/values`), not read out of a specification:
#
#   - a gauge's unit becomes a name segment -- `unit="1"` -> `_ratio`
#     (`usher_jobs_queued_ratio`), `unit="s"` -> `_seconds`
#     (`usher_scheduler_job_due_seconds`);
#   - a counter's unit is **dropped** in favour of `_total`
#     (`usher.source.push.reconnects`, `unit="1"` ->
#     `usher_source_push_reconnects_total`, not `..._ratio_total`);
#   - a histogram takes `_seconds` for `unit="s"` and **nothing** for
#     `unit="1"` (`usher.search.results` -> `usher_search_results_bucket`),
#     then one of `_bucket`/`_count`/`_sum`.
#
# So `unit="1"` renders as `_ratio` on a gauge and on nothing else, which is
# the part no amount of reading the instrument name would tell you.
_UNIT_SEGMENT = {"s": "_seconds", "ms": "_milliseconds", "By": "_bytes"}
_GAUGE_FACTORIES = frozenset({"create_observable_gauge"})
_COUNTER_FACTORIES = frozenset({"create_counter", "create_observable_counter"})
_HISTOGRAM_FACTORIES = frozenset({"create_histogram"})
_INSTRUMENT_FACTORIES = _GAUGE_FACTORIES | _COUNTER_FACTORIES | _HISTOGRAM_FACTORIES


def prd_alerts(text: str | None = None) -> dict[str, str]:
    """PRD 10's alert table, name -> condition."""
    source = text if text is not None else _PRD_10.read_text(encoding="utf-8")
    section = _ALERTS_SECTION.search(source)
    if section is None:
        return {}
    found: dict[str, str] = {}
    for row in _TABLE_ROW.finditer(section.group("body")):
        alert = row.group("alert").strip()
        if alert == "Alert" or set(alert) <= set("-: "):
            continue
        found[alert] = row.group("condition").strip()
    return found


def committed_rules(text: str | None = None) -> list[dict[str, Any]]:
    """Every rule in the committed file, flattened across its groups."""
    source = text if text is not None else _ALERTS.read_text(encoding="utf-8")
    document: Any = yaml.safe_load(source)
    return [rule for group in document["groups"] for rule in group["rules"]]


def grafana_rules(text: str | None = None) -> list[dict[str, Any]]:
    """Every Grafana-managed rule in `alerts/grafana/usher.yml`.

    **A second loader rather than a second format in one file**, and the
    separation is forced from both ends. Prometheus cannot evaluate a
    `SELECT`; Grafana's provisioning shape is not a Prometheus rule group
    (`title` where the other says `alert`, a `data` list where the other says
    `expr`); and `rule_files: [/etc/prometheus/rules/*.yml]` would read a
    sibling file and refuse the whole directory.
    `test_the_postgres_rule_is_not_in_the_directory_prometheus_globs` is the
    case that holds the location.
    """
    source = text if text is not None else _GRAFANA_ALERTS.read_text(encoding="utf-8")
    document: Any = yaml.safe_load(source)
    return [rule for group in document["groups"] for rule in group["rules"]]


def alert_names() -> set[str]:
    """Every alert this repository ships, across both engines.

    PRD 10's table is one list and does not say which engine evaluates a row,
    so the bidirectional check has to be over the union or it grades one file
    against a table describing two.
    """
    return {str(rule["alert"]) for rule in committed_rules()} | {
        str(rule["title"]) for rule in grafana_rules()
    }


def _grafana_rule(title: str) -> dict[str, Any]:
    for rule in grafana_rules():
        if rule["title"] == title:
            return rule
    raise AssertionError(
        f"no Grafana rule titled {title!r}; the file has "
        f"{sorted(str(rule['title']) for rule in grafana_rules())}"
    )


def cost_anomaly_sql() -> str:
    """The statement *Cost anomaly* fires on, read out of the committed rule.

    Exported because `tests/integration/test_cost_anomaly_query.py` executes
    **this** string against a real `pgvector/pgvector:pg17` rather than a
    transcription of it -- `db/repositories/llm_call.py` exports
    `_LIST_SINCE_SQL` for the same reason, and its own plan case imports it.
    A statement measured in one file and shipped from another is a statement
    whose copy is what stops tracking the original.
    """
    rule = _grafana_rule("Cost anomaly")
    queries = [query for query in rule["data"] if query["refId"] == rule["condition"]]
    assert len(queries) == 1, (
        f"the rule's `condition` names {rule['condition']!r}, which matches "
        f"{len(queries)} of its queries"
    )
    sql = [
        str(query["model"]["rawSql"])
        for query in rule["data"]
        if "rawSql" in query.get("model", {})
    ]
    assert len(sql) == 1, f"expected exactly one SQL query on this rule, found {len(sql)}"
    return sql[0]


def _rule(alert: str) -> dict[str, Any]:
    for rule in committed_rules():
        if rule["alert"] == alert:
            return rule
    raise AssertionError(f"no rule named {alert!r}; the file has {_committed_names()}")


def _committed_names() -> list[str]:
    return sorted(str(rule["alert"]) for rule in committed_rules())


def _instrument_declarations() -> list[tuple[str, str, str]]:
    """`(name, factory, unit)` for every instrument `src/usher/` declares.

    **An AST walk over a docstring-stripped tree, not a text scan**, and the
    difference is measurable rather than stylistic: `telemetry.py`'s
    `register_queue_gauges` docstring and the comment above `_queue_reader`
    both spell `create_observable_gauge("usher.jobs.queued", callbacks=[other])`
    while arguing about the SDK discarding a *second* registration. A `find()`
    over the source text reports 43 declarations against the real 41, two of
    them prose -- one of which supplies a name and no unit, i.e. it would grade
    `usher.jobs.queued` as a unitless gauge and quietly accept
    `usher_jobs_queued` as its stored spelling. That is the exact defect
    invariant 2 exists to catch, arriving through the check itself.
    `.claude/rules/testing-discipline.md` names the shape: *"prose that answers
    it"*.

    Comments are invisible to `ast` for free; docstrings are not, so they are
    removed before the walk.
    """
    found: list[tuple[str, str, str]] = []
    for path in sorted((_ROOT / "src" / "usher").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                node.value = ast.Constant(value=None)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _INSTRUMENT_FACTORIES:
                continue
            name = next(
                (
                    argument.value
                    for argument in node.args
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                ),
                None,
            )
            unit = next(
                (
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "unit"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                "",
            )
            if name is not None:
                found.append((name, node.func.attr, unit))
    return found


def stored_spellings() -> dict[str, set[str]]:
    """Every declared instrument, mapped to the names Prometheus stores for it.

    A set rather than one name because a histogram reaches Prometheus as three
    series, and an alert may legitimately name any of them -- `_count` for
    "how many", `_sum` over `_count` for a mean, `_bucket` under
    `histogram_quantile`.
    """
    spellings: dict[str, set[str]] = {}
    for name, factory, unit in _instrument_declarations():
        base = name.replace(".", "_")
        if factory in _COUNTER_FACTORIES:
            spellings.setdefault(name, set()).add(f"{base}_total")
            continue
        segment = _UNIT_SEGMENT.get(unit, "_ratio" if unit == "1" else "")
        if factory in _HISTOGRAM_FACTORIES:
            stem = base + (segment if segment != "_ratio" else "")
            spellings.setdefault(name, set()).update(
                {f"{stem}_bucket", f"{stem}_count", f"{stem}_sum"}
            )
        else:
            spellings.setdefault(name, set()).add(base + segment)
    return spellings


def _gauge_stored_names() -> set[str]:
    """The stored spellings of every instrument declared as an observable gauge.

    Derived from the declarations rather than listed, for
    `stored_spellings()`'s reason: a name typed into a set here is a name that
    stops being checked the day it is renamed. A gauge is the shape whose
    `increase()` decays out of its own window -- see
    `test_no_decaying_window_is_as_long_as_the_for_that_waits_on_it`.

    ⚠️ **`_MEASURED_STACK_SERIES` is added by name, because the derivation
    cannot reach it.** `system_filesystem_usage_bytes` is produced by the
    collector's `hostmetrics` receiver and not by any `create_*` call in
    `src/usher/`, so a set built from the declarations alone would put D13's
    rule on the *exempt* side of the split -- and that case's own docstring says
    in as many words that disk free belongs on the graded side. It is a level by
    the only test this split cares about, and that was watched rather than
    assumed: on 2026-09-11 `/` dropped 4,033 MB in one minute and was then flat
    to within a few MB, which is a step that leaves its own range vector exactly
    `[W]` later. A counter would have been re-fed the whole time.
    """
    spellings = stored_spellings()
    return {
        stored
        for name, factory, _unit in _instrument_declarations()
        if factory in _GAUGE_FACTORIES
        for stored in spellings[name]
    } | set(_MEASURED_STACK_SERIES)


def _tokens_of(rule: dict[str, Any]) -> set[str]:
    return _metric_tokens(str(rule["expr"]))


# `state="free"` and not `mountpoint=~"/|/data"`: only an **equality** matcher
# survives into an `absent()` result. A regex matcher does not, and neither
# does anything the missing series would have carried.
_EQUALITY_MATCHER = re.compile(r'(\w+)\s*=\s*"')


def _absence_subject_labels(expr: str) -> set[str] | None:
    """The labels an `absent()` rule can render, or `None` if it is not one.

    🔴 **An `absent()` alert has no per-instance subject, and interpolating one
    renders empty.** Prometheus builds the result's label set from the
    selector's equality matchers alone -- there is no series to take labels
    from, which is the condition. So `{{ $labels.mountpoint }}` on such a rule
    produces a page reading *"  is projected to fill"*, which is the same defect
    `sum by (le)` causes on an aggregating rule and is invisible to the same
    check.

    The recogniser is structural -- the *whole* expression must be one
    `absent()` call -- so `absent(x) or y > 0` is graded as an ordinary rule and
    still has to name a subject.
    """
    stripped = " ".join(expr.split())
    opening = len("absent(") - 1
    if not stripped.startswith("absent("):
        return None
    if _balanced(stripped, opening) != len(stripped) - 1:
        return None
    return set(_EQUALITY_MATCHER.findall(stripped))


def resource_table_figures(text: str | None = None) -> set[int]:
    """Every byte figure in PRD 08's `### Resource envelope` table, in bytes.

    🔴 **Parsed rather than retyped, and that is the whole point of the case
    that uses it.** A retyped list is a list somebody has to keep in step with a
    document nobody reads; the prohibition it defends -- *no threshold in either
    rule file is derived from that table* -- is about numbers that move, and
    M9's Track 2 withdrew a design over one of them (ADR-0036).

    Both the decimal and the binary reading of every ambiguous unit are
    returned, so `~5 GB` forbids 5,000,000,000 **and** 5,368,709,120.
    """
    source = text if text is not None else _PRD_08.read_text(encoding="utf-8")
    section = _RESOURCE_SECTION.search(source)
    if section is None:
        return set()
    figures: set[int] = set()
    for value, unit in _BYTE_FIGURE.findall(section.group("body")):
        number = float(value.replace(",", ""))
        for scale in (_DECIMAL.get(unit), _BINARY.get(unit), _EXPLICIT.get(unit)):
            if scale is not None:
                figures.add(int(number * scale))
    return figures


def _byte_thresholds(expressions: list[tuple[str, str]], figures: set[int]) -> list[str]:
    """Every integer literal in an expression that is one of `figures`.

    The *expressions* and never the annotations: both disk rules **quote** PRD
    08's measured baseline as what the database was on a date, which is the
    honest use of that table, and a scan that could not tell the two apart
    would forbid saying the number at all.
    """
    return [
        f"{name}: {literal}"
        for name, expression in expressions
        for literal in _INTEGER_LITERAL.findall(_RANGE.sub(" ", expression))
        if int(literal) in figures
    ]


def _every_expression() -> list[tuple[str, str]]:
    """`(alert, expression)` for every rule in both files, in both languages."""
    found = [(str(rule["alert"]), str(rule["expr"])) for rule in committed_rules()]
    for rule in grafana_rules():
        for query in rule["data"]:
            sql = query["model"].get("rawSql")
            if sql:
                found.append((str(rule["title"]), str(sql)))
    return found


def _dashboard_three_panel_titles() -> set[str]:
    return panel_titles(_DASHBOARD_THREE)


def test_every_alert_prd_10_names_exists_and_every_rule_names_a_series_the_catalogue_holds() -> (
    None
):
    """The bidirectional name check, and the catalogue check over every rule.

    **Both directions, because both are defects.** An alert PRD 10 names with
    no rule is an operator staring at a condition nothing watches. A rule PRD 10
    does not name falsifies the sentence the table opens with -- *"Kept few, so
    they mean something"* is a claim a seventh, unnamed rule makes false, and
    nothing else in this file would notice one.

    **The positive controls are the case, not decoration.** A regex that matched
    three of the seven rows would turn the equality assertion into a comparison
    of two small wrong sets, which passes the day the file happens to hold those
    three. So the row count is asserted against PRD 10's own number before the
    sets are compared at all.

    🔴 **The `xfail(strict=True)` is gone, and D13 is the task that removed
    it.** D11 held this case as a strict xfail rather than a red, so that
    `uv run pytest` stayed a trustworthy signal for every task between it and
    the last one -- a red left in the tree reads exactly like a regression and
    trains whoever sees it to ignore the suite. The `strict` half is what made
    the debt collectable: a strict xfail that *passes* is a failure, so the task
    that finally satisfied the assertion could not leave the marker behind.

    D11 expected that task to be D14. It was not: measured on the milestone HEAD
    D14 rebased onto -- `97d851a`, 2026-09-11 -- D12 had landed its two and
    `m10/D13` still had nothing committed on it, so D14 shipped *Cost anomaly*,
    took its own name out of `_OWED` and handed the marker on. D13 is where it
    lands, `_OWED` is empty above, and this is a plain assertion again. **The
    handover is the mechanism working, not a deferral** -- at no point did the
    ledger claim a debt that was paid or hide one that was not.

    **The names come from `alert_names()` and so span both files.** PRD 10's
    table is one list that says nothing about which engine evaluates a row;
    six of its seven are Prometheus rules and *Cost anomaly* is a Grafana
    Postgres rule, so a check reading only `alerts/usher.yml` would report the
    seventh missing forever while it sat one directory down.
    """
    named = prd_alerts()
    assert named, "no alert rows parsed out of PRD 10"
    assert len(named) == 7, (
        f"PRD 10's `## Alerts` table is seven rows; this parse found {len(named)}: {sorted(named)}"
    )

    rules = committed_rules()
    catalogue = metric_catalogue()
    assert catalogue, "PRD 10's metric table parsed to nothing"
    unknown = [
        f"{rule['alert']}: {token}"
        for rule in rules
        for token in _tokens_of(rule)
        if token not in _MEASURED_STACK_SERIES and normalise_metric(token) not in catalogue
    ]
    assert unknown == [], (
        "these tokens normalise to a name PRD 10's metric table does not hold, so the "
        f"rule selects nothing and reads as all-clear forever: {unknown}"
    )

    committed = alert_names()
    missing = sorted(set(named) - committed)
    assert committed == set(named), (
        "the rule file and PRD 10's table disagree. Owed: "
        + ", ".join(f"{alert} ({_OWED.get(alert, 'no task')})" for alert in missing)
        + f"; named by no PRD 10 row: {sorted(committed - set(named))}"
    )


def test_every_committed_rule_names_a_series_the_catalogue_holds() -> None:
    """The catalogue arm of the case above, over the rules that exist today.

    The named case carries both halves because the task text says it does, and
    an `xfail` swallows every assertion in the body -- including that one. So
    the half that *is* falsifiable at this HEAD runs here as well, where it has
    teeth now rather than in three tasks' time. This is not duplication for its
    own sake: it is the difference between D12 finding a typo when it writes its
    rule and D14 finding it.
    """
    rules = committed_rules()
    assert len(rules) == 7, (
        "D11 shipped three rules, D12 added two, and D13 adds two more -- both named "
        f"*Disk projection*. Found {len(rules)}: {_committed_names()}"
    )
    catalogue = metric_catalogue()
    assert catalogue, "PRD 10's metric table parsed to nothing"

    graded = [
        (str(rule["alert"]), token)
        for rule in rules
        for token in _tokens_of(rule)
        if token not in _MEASURED_STACK_SERIES
    ]
    assert len(graded) >= 4, (
        "the token scan found fewer metric names than the three rules spell, so it has "
        f"stopped reading the expressions: {graded}"
    )
    for alert, token in graded:
        assert normalise_metric(token) in catalogue, (
            f"{alert}: {token!r} normalises to {normalise_metric(token)!r}, which PRD 10's "
            "metric table does not hold"
        )


def test_every_rule_is_written_in_the_spelling_prometheus_stores() -> None:
    """🔴 The check the catalogue check cannot be.

    `usher_jobs_queued` normalises to `usher_jobs_queued`, which is in PRD 10's
    table, so the case above passes it. It also selects **nothing**, because the
    collector stores that gauge as `usher_jobs_queued_ratio` -- and an alert
    expression that selects nothing does not fail and does not empty a panel; it
    evaluates to no alerts, which is what "healthy" looks like. D8 found the
    same spelling gap on Dashboard 3's panels, where the cost was a blank
    rectangle somebody would eventually notice.

    The stored spellings are **derived from the declarations**, so a new
    instrument is covered the day it is declared, and the derivation itself is
    pinned in `test_the_stored_spelling_derivation_matches_this_hosts_prometheus`.
    """
    spellings = stored_spellings()
    known = {name for names in spellings.values() for name in names}
    assert len(spellings) >= 40, (
        f"the declaration walk found only {len(spellings)} instruments, so every "
        "assertion below is graded against a catalogue that has stopped being read"
    )

    wrong = [
        (str(rule["alert"]), token, sorted(spellings.get(_otel_name(token, spellings), set())))
        for rule in committed_rules()
        for token in _tokens_of(rule)
        if token not in known and token not in _MEASURED_STACK_SERIES
    ]
    assert wrong == [], (
        "these tokens are not names this deployment's collector stores -- the exporter "
        "puts the instrument's unit in the name, so the rule parses, evaluates to an "
        f"empty vector and reads as all-clear forever: {wrong}"
    )


def _otel_name(token: str, spellings: dict[str, set[str]]) -> str:
    """The PRD 10 name a mis-spelled token was probably reaching for."""
    key = normalise_metric(token)
    return next((name for name in spellings if name.replace(".", "_") == key), token)


def test_the_stored_spelling_derivation_matches_this_hosts_prometheus() -> None:
    """The control for the case above, and it is the whole of its credibility.

    A derivation that is wrong in the same direction as the rule file grades
    every rule green. These are names read off this host's Prometheus on
    2026-09-11 under `/api/v1/label/__name__/values`, one per instrument shape
    the derivation has to get right, and each is paired with the PRD 10 name it
    has to come out of.

    The three rows that carry the whole argument are the `unit="1"` ones:
    it becomes `_ratio` on a gauge, is **dropped** for `_total` on a counter,
    and is dropped entirely on a histogram. No instrument name says which of the
    three it is.
    """
    spellings = stored_spellings()
    measured = {
        "usher.jobs.queued": "usher_jobs_queued_ratio",
        "usher.jobs.parked": "usher_jobs_parked_ratio",
        "usher.source.push.connected": "usher_source_push_connected_ratio",
        "usher.sse.connections": "usher_sse_connections_ratio",
        "usher.scheduler.job.due": "usher_scheduler_job_due_seconds",
        "usher.source.push.reconnects": "usher_source_push_reconnects_total",
        "usher.cache.hits": "usher_cache_hits_total",
        "usher.enrich.result": "usher_enrich_result_total",
        "usher.jobs.duration": "usher_jobs_duration_seconds_count",
        "usher.enrichment.latency": "usher_enrichment_latency_seconds_bucket",
        "usher.search.results": "usher_search_results_bucket",
        "usher.suggest.results": "usher_suggest_results_sum",
    }
    for otel, stored in measured.items():
        assert otel in spellings, (
            f"the declaration walk did not find {otel!r} at all, so nothing it derives "
            "about that instrument is checked"
        )
        assert stored in spellings[otel], (
            f"this host's Prometheus holds {stored!r} for {otel!r}; the derivation says "
            f"{sorted(spellings[otel])}"
        )

    assert "usher_jobs_queued" not in {name for names in spellings.values() for name in names}, (
        "the derivation has stopped appending the unit segment, which makes "
        "`test_every_rule_is_written_in_the_spelling_prometheus_stores` accept the one "
        "spelling that never fires"
    )


def test_the_ingest_stalled_rule_reaches_a_lane_that_has_never_settled_a_job() -> None:
    """🔴 The headline: the rule must fire on a *zero* completions count and on
    an *absent* one, and `and ... == 0` only does the first.

    `and` is a set intersection -- a left-hand series survives only where a
    right-hand series with the same labels exists. The two halves of this rule
    have opposite shapes:

    - **Depth is always nine series.** `PostgresJobQueue.depth` fills
      `dict.fromkeys(JobKind, 0)` before returning, with the reason in its own
      comment: *"a gauge that stops reporting a series is indistinguishable from
      one reporting zero"*.
    - **Completions are only the kinds that have settled something.**
      `usher.jobs.duration` is a recorded histogram, so a lane that has never
      run a job has no `kind` of its own on that side at all.

    So `and` drops exactly the lanes that have never settled a job, which is the
    *worst* case of "ingest stalled" rather than an edge of it: a deployment
    where `USHER_LLM_ENABLED=false` leaves `curate` unclaimable
    (`composition.worker_kinds`) queues curate jobs forever and this rule, spelled
    with `and`, says nothing. Measured against this host's Prometheus on
    2026-09-11: the `and` spelling reaches 5 kinds and the `unless` spelling
    reaches 9, and the four it adds are bootstrap, curate, sync and
    watch_writeback -- every lane that has never settled a job.

    `unless` is the complement, so the completions half becomes a positive
    filter (`> 0`, "this lane did settle something") and everything it does not
    match -- zero *and* absent alike -- stays.
    """
    expr = str(_rule("Ingest stalled")["expr"])
    assert "usher_jobs_queued_ratio" in expr and "usher_jobs_duration_seconds_count" in expr, (
        f"this rule no longer names both halves of PRD 10's condition: {expr!r}"
    )
    assert re.search(r"\bunless\b", expr), (
        "the depth half is joined to the completions half by something other than "
        "`unless`, so a lane with no completions series at all is dropped by the label "
        f"matching and never fires: {expr!r}"
    )
    assert re.search(
        r"unless[^\n]*\n?\s*increase\(usher_jobs_duration_seconds_count\[[^\]]+\]\)\s*>\s*0",
        expr,
    ), (
        "`unless` must exclude the lanes that *did* settle something, so its right-hand "
        f"side is a `> 0` filter on the completions count: {expr!r}"
    )
    assert not re.search(r"usher_jobs_duration_seconds_count\[[^\]]+\]\)\s*==\s*0", expr), (
        "an `== 0` on the completions count is the spelling that only sees a reported "
        f"zero, which is four of the nine lanes short: {expr!r}"
    )


# The range functions whose answer *decays out of its own window*: a step
# change is inside `[W]` for exactly W and then gone. `min_over_time` and the
# instant selectors are deliberately absent -- their answer persists, so a long
# `for:` under them is patience rather than a knife edge.
#
# **`predict_linear` is on this list, added by D13**, and it belongs here on the
# stated property rather than by family resemblance: it is a least-squares fit
# over the samples in its range vector, so a one-off step -- the `VACUUM`-less
# migration `08-operations.md` measures at +637 MB transient -- tilts the line
# for exactly the window's length and then leaves it. A `for:` as long as the
# window is the same knife edge `increase` has.
_DECAYING = re.compile(r"\b(?:increase|rate|irate|delta|idelta|deriv|predict_linear)\(")
_WINDOW = re.compile(r"\[(\d+)([smhdwy])\]")
_DURATION = re.compile(r"(\d+)([smhdwy])")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def _seconds(duration: str) -> int:
    """A Prometheus duration (`30m`, `1h30m`) in seconds."""
    parts = _DURATION.findall(duration)
    assert parts, f"{duration!r} is not a Prometheus duration"
    return sum(int(value) * _UNIT_SECONDS[unit] for value, unit in parts)


def test_no_decaying_window_is_as_long_as_the_for_that_waits_on_it() -> None:
    """🔴 Two windows of thirty minutes is sixty, and the rule fires on neither.

    `increase(depth[30m]) > 0` with `for: 30m` is **unsatisfiable for a queue
    that rises once and then stops moving**, which is the commonest stall there
    is. A step change sits inside a `[30m]` range vector for exactly thirty
    minutes and then leaves it, so the condition is true for at most as long as
    `for:` demands -- a knife edge that one evaluation loses. Measured live on
    2026-09-11 with 25 `curate` jobs against a worker that could not claim them,
    depth flat at 25 the whole time: the alert went `pending` at 17:09:58Z and
    back to `inactive` at **17:39:34Z**, one evaluation before its own
    `for: 30m` would have fired it. It is not a slow alert; it is a silent one.

    `min_over_time(...) > 0` reads the same thirty minutes the other way and its
    answer *persists*, so this case exempts the non-decaying functions by name
    rather than banning long `for:` outright -- the patience has to live
    somewhere, and under a decaying window it cannot live in both places.

    🔴 **D11 stated this as a law about `increase()`/`rate()`; D12 measured it
    and it is a law about the *instrument*.** The knife edge is not a property
    of the function -- it is a property of whether the signal under the function
    is *regenerated while the fault lasts*:

    - A **gauge** is a level. It steps once and freezes, so the step leaves the
      range vector exactly `[W]` later and the condition is true for at most W.
      D11's queue depth is this, and its measurement stands.
    - A **counter** (and a histogram's bucket/count series) is fed by *every
      event*. Under a fault lasting D, `rate()` stays above the threshold for
      about D + W, so a `for:` longer than W is patience and not a race.

    Measured with `promtool test rules` on 2026-09-11, against D12's own rules,
    both of which put a `for:` **longer** than their `[5m]` window:

    - *Provider degraded* (`for: 10m` over `[5m]`, a sustained 10 % 429 rate):
      silent at 9m, **firing at 16m**, still firing at 60m, resolved by 40m once
      the 429s stopped.
    - *Enrichment SLA missed* (`for: 15m` over `[5m]`, demand p99 ~6 s): silent
      at 14m, **firing at 21m** with `trigger="demand"` and `$value` 7.475s,
      still firing at 60m.

    So the blanket rule would have forbidden two rules that demonstrably fire,
    and forced their windows out to `[20m]`/`[15m]` -- which buys nothing and
    makes both slow to resolve. The guard is narrowed to the case it was
    measured on rather than deleted, and the exemption is **derived from the
    declarations** (`create_observable_gauge`) rather than listed, so a new
    gauge is covered the day it is declared.

    ⚠️ **Still for D13-D14.** *Disk projection* and *Cost anomaly* are both
    predicates over levels -- disk free is a gauge, a daily spend comparison is
    a step -- so both land on the graded side of this split, not the exempt one.

    ✅ **D13 landed on the graded side, as that paragraph said it would**, and
    it took two changes to get there. `predict_linear` joined `_DECAYING` on the
    stated property: a least-squares fit is tilted by a step for exactly the
    window's length and then not at all. And `_MEASURED_STACK_SERIES` joined
    `_gauge_stored_names()`, because the level set is derived from
    `src/usher/`'s declarations and the disk series is produced by the
    collector, not by Usher -- without it D13's rule would have been *exempt*
    here, which is the opposite of what this docstring promised. `for: 7d`
    planted against `predict_linear(...[7d], ...)` dies on the assertion below.
    """
    graded = 0
    exempt = 0
    for rule in committed_rules():
        expr = str(rule["expr"])
        wait = _seconds(str(rule["for"]))
        # The window has to come from *inside* a decaying call, not from
        # anywhere in the expression: `Ingest stalled`'s depth half is a
        # `min_over_time(...[30m])` whose answer persists, and grading `for:`
        # against that window would forbid the very spelling that repairs the
        # defect.
        for call in _DECAYING.finditer(expr):
            opening = call.end() - 1
            body = expr[opening + 1 : _balanced(expr, opening)]
            over_a_level = bool(_metric_tokens(body) & _gauge_stored_names())
            for value, unit in _WINDOW.findall(body):
                window = int(value) * _UNIT_SECONDS[unit]
                if not over_a_level:
                    exempt += 1
                    continue
                graded += 1
                assert wait < window, (
                    f"{rule['alert']}: `for: {rule['for']}` is not shorter than the "
                    f"[{value}{unit}] window its own decaying condition lives in, and "
                    "that condition is over a *gauge* -- a level that steps once leaves "
                    "the range vector at the moment `for:` is satisfied, so the rule "
                    "fires on a knife edge it loses"
                )
    assert graded >= 1, (
        f"the scan graded {graded} decaying windows over a gauge, so it has stopped "
        "reading the expressions and this case is vacuous"
    )
    assert exempt >= 2, (
        f"the scan exempted {exempt} decaying windows over a counter or histogram; D12's "
        "two rules are both that shape, so a zero here means the exemption has stopped "
        "recognising them and they are passing for the wrong reason"
    )


def test_the_push_down_rule_fires_on_a_zero_and_not_on_an_absence() -> None:
    """The other half of the same distinction, decided the other way.

    Here an absent series is **not** an incident. `api/lanes.py`'s
    `push_snapshots()` iterates `self._open_adapters`, and a lane only opens for
    an *enabled* source -- so a source with no series is a source nobody is
    running a lane for, which is a configuration state an operator chose. PRD
    10's qualifier *"on a source that supports it"* therefore needs no join
    against `sources.supports_push`: the series is already scoped to exactly
    those sources by construction, and an `absent()` arm would page somebody for
    having parked a server that is being rebuilt.

    A reported **zero** is the incident, and it is a narrower claim than "the
    socket is shut": `PushSnapshot.delivering` is `PushHealth.is_delivering`, so
    a channel that upgraded, is held open and delivers nothing reads 0 -- which
    is the failure ADR-0004 measured and the one this alert exists for.
    """
    rule = _rule("Push down")
    expr = str(rule["expr"])
    assert "absent" not in expr, (
        "an absent push series is a source nobody is running a lane for, which is "
        f"configuration and not an incident: {expr!r}"
    )
    assert re.search(r"usher_source_push_connected_ratio\s*==\s*0", expr), (
        f"PRD 10's condition is `push.connected == 0`, spelled on the stored name: {expr!r}"
    )
    assert rule["for"] == "15m", f"PRD 10's window is 15 min; this rule waits {rule['for']!r}"


def test_the_disk_rule_is_grounded_in_a_measured_series_and_not_in_the_resource_table() -> None:
    """🔴 D13's headline, and it is two prohibitions that fail in opposite directions.

    **The series half.** *Disk projection* is the one alert in PRD 10's table
    with **no series at all** on this deployment -- measured 2026-09-11, the
    shared Prometheus holds 93 metric names and not one of them is a disk,
    filesystem or node series, because its `prometheus.yml` carries no
    `scrape_configs` on purpose and the collector has no `hostmetrics`
    receiver. So this rule is the one most able to commit D11's failure: a name
    nothing stores parses, loads, evaluates to an empty vector and reads
    *healthy* forever. The name in the file therefore has to be one that was
    **watched arriving**, and the allow-list is the set of those names.

    `usher_disk_free_bytes` -- the spelling this task was handed -- is the
    planted control, because it is the mistake that was actually available:
    plausible, prefixed like an Usher instrument, and produced by nothing.

    **The threshold half, which fails the other way round.** A literal lifted
    out of `08-operations.md`'s resource envelope would select plenty and fire;
    the defect is that it would be *enforcing a number with no forcing
    function*. That table's own header says nothing reads it, no host enforces
    it and no policy derives from it, and M9's Track 2 derived a 2.0 GB ceiling
    from one row, measured a design at 2.702 GB and **withdrew the design**
    (ADR-0036). The figures are parsed out of the table rather than retyped, in
    both the decimal and the binary reading, so the prohibition tracks the
    document instead of a list somebody has to remember -- and it is applied to
    **both** rule files, because the Postgres half could carry a byte ceiling
    just as easily as the PromQL half.

    **The parse is asserted before it is used**, because a scan for numbers that
    finds none passes exactly like a file with no bad numbers in it.
    """
    figures = resource_table_figures()
    assert figures, "no figures parsed out of the resource table"
    assert len(figures) >= 40, (
        f"the resource-table parse found only {len(figures)} figures, so the prohibition "
        "below is graded against a table it has stopped reading"
    )
    assert 5_025_650_355 in figures, (
        "the parse no longer reads `pg_database_size` 5,025,650,355 B, the measured "
        "baseline these rules quote -- the single figure most likely to be promoted from "
        "a measurement into a threshold"
    )
    assert 2_147_483_648 in figures and 2_000_000_000 in figures, (
        "the parse no longer reads M9's withdrawn 2.0 GB ceiling in either reading, which "
        "is the exact number ADR-0036 records a design being refused against"
    )

    disk = [rule for rule in committed_rules() if str(rule["alert"]) == "Disk projection"]
    assert len(disk) == 2, (
        "PRD 10's one *Disk projection* row is two Prometheus rules -- the projection and "
        f"the guard that makes its own blindness loud; found {len(disk)}"
    )
    assert _grafana_rule("Disk projection"), "the database-growth half is not in the Grafana file"

    def ungrounded(rules: list[dict[str, Any]]) -> list[str]:
        return [
            f"{rule['alert']}: {token}"
            for rule in rules
            for token in _tokens_of(rule)
            if token not in _MEASURED_STACK_SERIES
        ]

    planted_name = [
        {
            "alert": "Disk projection",
            "expr": 'predict_linear(usher_disk_free_bytes{mountpoint="/"}[7d], 1209600) < 0',
        }
    ]
    assert ungrounded(planted_name) != [], (
        "the series scan cannot see `usher_disk_free_bytes`, a name no producer on this "
        "host emits -- so it would grade a permanently-empty rule green"
    )
    assert ungrounded(disk) == [], (
        "these tokens are not names anything was watched storing, so the rule evaluates "
        f"to an empty vector and reads as all-clear forever: {ungrounded(disk)}"
    )

    planted_threshold = [
        ("Disk projection", "system_filesystem_usage_bytes < 5025650355"),
        ("Disk projection", "SELECT pg_database_size(current_database()) > 2147483648"),
    ]
    assert len(_byte_thresholds(planted_threshold, figures)) == 2, (
        "the threshold scan cannot see a literal lifted out of the resource table in both "
        "languages, so the prohibition ADR-0036 was written for is decorative: "
        f"{_byte_thresholds(planted_threshold, figures)}"
    )
    expressions = _every_expression()
    assert len(expressions) >= 9, (
        "the expression scan found fewer than the seven Prometheus rules and the two "
        f"Grafana statements, so it has stopped reading one of the files: {len(expressions)}"
    )
    assert _byte_thresholds(expressions, figures) == [], (
        "a rule carries a byte literal that is a figure from PRD 08's resource envelope. "
        "That table is sizing estimates for an operator provisioning a disk -- nothing "
        "reads them, no host enforces them, and M9's Track 2 withdrew a design against "
        f"one: {_byte_thresholds(expressions, figures)}"
    )


def test_the_stack_series_exemption_is_one_measured_name_and_not_a_blanket() -> None:
    """The control for the exemption itself.

    Every other rule in `alerts/usher.yml` is graded twice -- against PRD 10's
    metric catalogue and against the spelling derived from `src/usher/`'s own
    `create_*` calls. *Disk projection*'s two Prometheus rules can be graded by
    neither, because their subject is a filesystem and Usher declares no
    instrument for one. An exemption is the only way they land, and **an
    exemption nobody counts is how every later rule escapes the check** -- D10
    hit the same shape with the `pg_class` panel and asserted its exemption by
    name and by count rather than merely granting it.

    So: one name, spelled exactly as it was observed arriving, and used by
    exactly the rules that own it.
    """
    assert set(_MEASURED_STACK_SERIES) == {"system_filesystem_usage_bytes"}, (
        "the exemption has grown past the one series this file measured. Any addition is "
        "a name somebody has to have watched Prometheus store, and the measurement goes "
        f"in `dashboards/README.md` beside it: {sorted(_MEASURED_STACK_SERIES)}"
    )

    exempt_users = {
        str(rule["alert"])
        for rule in committed_rules()
        if _tokens_of(rule) & _MEASURED_STACK_SERIES
    }
    assert exempt_users == {"Disk projection"}, (
        "a rule other than *Disk projection* is reaching through the stack-series "
        f"exemption: {sorted(exempt_users)}"
    )

    for name in _MEASURED_STACK_SERIES:
        assert name not in {
            spelling for spellings in stored_spellings().values() for spelling in spellings
        }, (
            f"{name!r} is now something `src/usher/` declares, so it is no longer an "
            "exemption -- delete it from the allow-list and let the derivation grade it"
        )


def test_the_resource_table_parse_is_scoped_and_falsifiable() -> None:
    """The figure parse has to be able to come back wrong, in each of its parts.

    Three separate failures, and only the first is loud. A heading regex that
    matches nothing hands back an empty set, which the case above catches with
    `assert figures`. A scope that **leaks past** the section quietly widens the
    prohibition to every number in PRD 08 -- and then a rule is red for carrying
    a figure from a table this task was told not to read *and did not*. And a
    figure pattern that accepted unanchored numbers would forbid `1,272,367`, a
    title count, as a byte threshold.
    """
    document = (
        "# Operations\n\n"
        "### Resource envelope\n\n"
        "| | |\n|---|---|\n"
        "| Postgres | **~5 GB at 1,272,367 titles**, 2,048 bytes a vector |\n\n"
        "## Testing\n\n"
        "| Something else | 99 GB |\n"
    )
    parsed = resource_table_figures(document)
    assert 5_000_000_000 in parsed and 5_368_709_120 in parsed, (
        f"`~5 GB` no longer parses in both the decimal and the binary reading: {parsed}"
    )
    assert 2048 in parsed, f"an explicit `bytes` figure no longer parses: {parsed}"
    assert 1_272_367 not in parsed, (
        "an unanchored number is being read as a byte figure, so the prohibition now "
        f"forbids a title count: {sorted(parsed)}"
    )
    assert 99_000_000_000 not in parsed and 106_300_440_576 not in parsed, (
        "the section scope has leaked past `### Resource envelope` and is reading another "
        f"section's numbers as resource figures: {sorted(parsed)}"
    )
    assert resource_table_figures("# Operations\n\nNo resource section here.\n") == set(), (
        "a document with no `### Resource envelope` parses to something, so the section "
        "regex is matching the whole file"
    )


#: The output list of `Cost anomaly`'s statement -- everything between its
#: final `SELECT` and the `FROM judged` that closes it. Sliced rather than
#: regex-matched over the whole statement, because four CTEs above it also
#: end lines in `AS <name>` and a scan that read those would grade the
#: `bounds`/`ledger`/`daily` internals as if Grafana saw them.
def _cost_anomaly_outputs(sql: str) -> str:
    marker = "\nSELECT\n"
    assert sql.count(marker) >= 1, (
        f"no top-level `SELECT` found in the cost-anomaly statement, so the column scan "
        f"below reads nothing: {sql!r}"
    )
    return sql.rsplit(marker, 1)[1]


_AN_OUTPUT_ALIAS = re.compile(r"\bAS\s+(\w+),?\s*$", re.M)


def disk_growth_sql() -> str:
    """The statement *Disk projection*'s database-growth half fires on.

    Read out of the committed rule for `cost_anomaly_sql()`'s reason: a
    statement measured in one place and shipped from another is a statement
    whose copy is what stops tracking the original.
    """
    rule = _grafana_rule("Disk projection")
    queries = [query for query in rule["data"] if query["model"].get("rawSql")]
    assert len(queries) == 1, (
        f"the disk-growth rule carries {len(queries)} SQL queries, not one: "
        f"{[query['refId'] for query in queries]}"
    )
    return str(queries[0]["model"]["rawSql"])


def test_the_postgres_rule_is_not_in_the_directory_prometheus_globs() -> None:
    """🔴 A Grafana provisioning file beside `usher.yml` disarms the other
    five rules, and the directory layout is the whole of the defence.

    `alerts/usher.yml`'s header tells an operator to mount `dashboards/alerts`
    at `/etc/prometheus/rules` and set
    `rule_files: [/etc/prometheus/rules/*.yml]`. That glob is not recursive,
    Prometheus unmarshals rule files **strictly**, and a Grafana rule group
    carries `apiVersion`, `folder`, `condition` and `data` -- none of which a
    Prometheus rule group has. The result is not one ignored file: a server
    started on such a directory **exits 2 before opening a port**, so every
    rule in `usher.yml` stops existing as the price of adding the seventh
    alert. Measured on `prom/prometheus:v3.13.2`, 2026-09-11, and recorded in
    `dashboards/README.md` -- the same server on the committed layout serves
    all five.

    So this case pins two things at once: that the Prometheus directory holds
    exactly the one file Prometheus can read, and that the Grafana rule is
    somewhere that glob does not reach. The first assertion is what fails if
    somebody adds `grafana.yml` beside `usher.yml`; the second is what fails
    if the Grafana file is moved up rather than deleted.
    """
    prometheus_directory = _ALERTS.parent
    globbed = sorted(path.name for path in prometheus_directory.glob("*.yml"))
    assert globbed == ["usher.yml"], (
        "`rule_files: [/etc/prometheus/rules/*.yml]` reads every one of these, and "
        "Prometheus refuses the whole set if one of them is not a Prometheus rule file: "
        f"{globbed}"
    )
    assert _GRAFANA_ALERTS.is_file(), f"{_GRAFANA_ALERTS} does not exist"
    assert _GRAFANA_ALERTS not in set(prometheus_directory.glob("*.yml")), (
        f"{_GRAFANA_ALERTS.name} is in the directory Prometheus globs"
    )
    assert _GRAFANA_ALERTS.parent.parent == prometheus_directory, (
        "the Grafana rules have moved out from under `dashboards/alerts/`, so the two "
        "engines' alert files no longer live together and nothing points from one to the "
        f"other: {_GRAFANA_ALERTS}"
    )

    # The positive control for the glob itself: a `*.yml` that matched nothing
    # would satisfy the equality above for an empty directory just as happily.
    assert _ALERTS.name in globbed, "the glob does not even find the file it is about"


def test_the_postgres_rule_names_only_tables_and_columns_this_schema_holds() -> None:
    """Invariant 1, in the only form a rule with no metric can have it.

    A PromQL expression naming a series nobody stores evaluates to an empty
    vector and reads as healthy forever; a `SELECT` naming a column nobody
    stores does **not** -- it raises, and Grafana's `execErrState: Error` is
    what carries that to a human. The failure this case is really for is the
    one in between: a column that exists on a *different* table, or a table
    renamed by a migration while the rule keeps the old name. Both are only
    visible against `Base.metadata`, which is where D6's invariant 3 already
    looks, so this reuses `_sql_pairs` rather than teaching a second scanner
    the same lesson.

    ⚠️ The same coverage limit applies as there, and it is why the statement
    writes `llm_calls.at` rather than aliasing the table: an alias is not a
    table name, so an aliased statement yields no pairs and is graded on
    nothing. The count assertion below is what notices that.
    """
    pairs = _sql_pairs(cost_anomaly_sql())
    assert len(pairs) >= 3, (
        "the table.column scan found almost nothing in the cost-anomaly statement, so "
        "either it has been rewritten in aliases or the scan has stopped reading it: "
        f"{pairs}"
    )
    for table, column in pairs:
        columns = Base.metadata.tables[table].columns
        assert column in columns, (
            f"Cost anomaly selects {table}.{column}, which is not a column of {table}: "
            f"{sorted(c.name for c in columns)}"
        )
    assert {table for table, _ in pairs} == {"llm_calls"}, (
        "PRD 10's cost ledger is `llm_calls` and this rule reads something else too, "
        f"which makes its window a different question: {sorted({t for t, _ in pairs})}"
    )


def test_the_cost_anomaly_statement_carries_its_floor_its_window_and_stays_in_numeric() -> None:
    """🔴 The four decisions the task text calls "properties of that query",
    each spelled so that deleting it is red here.

    - **Eight calendar days, seven of them judged.** The trailing median
      excludes today, so the window has to hold seven *complete* days plus the
      partial one being judged -- a seven-day window including today compares
      today against a median it is a member of.
    - **A median, not a mean**, as PRD 10 specifies: one generation per
      household per night means a single failed night at $0 and a single re-run
      at 2x drag a mean far enough that 3x stops meaning anything.
    - **The comparison stays in `numeric`.** `cost_usd` is `NUMERIC(12, 8)`
      precisely so money is not a float, and a rule that casts to `float8` for
      the ratio reintroduces the rounding that column exists to refuse.
      ⚠️ **`percentile_cont` is that cast**: Postgres has no `numeric`
      overload of it, so `percentile_cont(0.5) WITHIN GROUP (ORDER BY
      <numeric>)` returns `double precision` -- measured with `pg_typeof` on
      PostgreSQL 17.10, 2026-09-11. `percentile_disc` is
      `anyelement -> anyelement` and over a seven-element set returns the same
      element. That is why the statement the task text supplies is not the
      statement that shipped.
    - **An absolute floor**, because a `0` trailing median makes `3 x median`
      zero and any spend at all an anomaly -- the default state of every
      deployment that has not priced its model.

    The scans are substrings, which `testing-discipline.md` warns is how a
    rendered artefact becomes a change-detector. They are substrings *here*
    because each is a claim another component honours: the floor literal is
    compared against the number the description promises an operator, and the
    window and the aggregate are the two the mutation sweep is aimed at.
    """
    sql = cost_anomaly_sql()

    assert sql.count("interval '7 days'") == 2, (
        "the seven trailing days are spelled twice on purpose -- once for the calendar "
        "series and once for the index-served lower bound -- and the two must move "
        f"together or the query reads more rows than it judges: {sql}"
    )
    assert "generate_series(" in sql and "coalesce(ledger.spend, 0)" in sql, (
        "the day series is grouped rather than generated, so a night with no LLM calls "
        "is *absent* from the trailing set instead of being a 0 -- and a deployment that "
        "curates three nights a week then takes its median over three nonzero days"
    )
    assert "percentile_disc(0.5)" in sql, "the trailing statistic is no longer a median"
    assert "percentile_cont" not in sql, (
        "`percentile_cont` has no `numeric` overload, so it casts `cost_usd` to "
        "`double precision` and returns one -- the float this column was declared "
        "`NUMERIC(12, 8)` to refuse"
    )
    assert not re.search(r"float8|double\s+precision|::real", sql), (
        f"the comparison has left `numeric`: {sql}"
    )
    assert "AT TIME ZONE 'UTC'" in sql, (
        "`date_trunc('day', <timestamptz>)` truncates in the *session's* time zone, so "
        "without this the answer depends on how the Grafana server was started"
    )

    floors = re.findall(r">= (\d+\.\d+)\b", sql)
    assert floors == ["0.02"], (
        "the floor is one stated constant and this statement has a different number of "
        f"them: {floors}"
    )


def test_the_cost_anomaly_rule_hands_grafana_exactly_one_numeric_column() -> None:
    """🔴 The Postgres-side twin of "a metric nobody stores reads healthy
    forever", and it fails in the opposite, louder direction.

    Grafana's SQL-to-alerting conversion turns every **numeric** column of a
    table frame into a series the condition is evaluated over and every
    **string** column into a label on it. The threshold below is `> 0`. So a
    second numeric column -- `days_in_window`, which is 8 by construction --
    would be judged by that same threshold and this alert would fire on every
    evaluation, forever, with a page naming no anomaly. That is why every
    diagnostic column carries `::text`: the casts are the rule's wiring, not
    its formatting.

    The types themselves are read out of a real PostgreSQL in
    `tests/integration/test_cost_anomaly_query.py`, through
    `information_schema`; what is checkable here is the shape that produces
    them, and that the summary interpolates only labels this statement
    actually returns.
    """
    rule = _grafana_rule("Cost anomaly")
    outputs = _cost_anomaly_outputs(cost_anomaly_sql())
    columns = _AN_OUTPUT_ALIAS.findall(outputs)
    assert columns[:1] == ["fired"], (
        f"the value column is no longer first or no longer named `fired`: {columns}"
    )
    assert len(columns) == 7, f"expected seven output columns, found {columns}"
    assert outputs.count("::text") == len(columns) - 1, (
        "every column but `fired` has to reach Grafana as a *label*, which means a "
        f"`::text` each; this statement casts {outputs.count('::text')} of "
        f"{len(columns) - 1}: {outputs}"
    )

    fired = outputs.split("AS fired", 1)[0]
    assert "::text" not in fired and "THEN 1 ELSE 0 END" in fired, (
        f"`fired` is not a bare 0/1 numeric column: {fired!r}"
    )

    condition = next(query for query in rule["data"] if query["refId"] == rule["condition"])
    assert condition["model"]["type"] == "threshold", (
        "the condition is no longer a threshold, so the `> 0` this statement is written "
        f"against is not what decides: {condition['model']}"
    )
    assert condition["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [0]}, (
        "the threshold is not `> 0`, which puts a second number in the decision beside "
        f"the 3x in the SQL: {condition['model']['conditions']}"
    )

    interpolated = set(re.findall(r"\$labels\.(\w+)", str(rule["annotations"])))
    assert interpolated, "the page interpolates no label, so it names no subject"
    assert interpolated <= set(columns), (
        "the page interpolates labels this statement does not return, so they render "
        f"empty: {sorted(interpolated - set(columns))}"
    )


def test_the_cost_anomaly_summary_survives_an_undefined_ratio() -> None:
    """🔴 The one label on this rule that is sometimes a **sentence**, and the
    page has to stay a sentence when it is.

    `spend_ratio` is `round(today / nullif(median, 0), 4)` with a
    `coalesce(..., 'undefined (zero trailing median)')` behind it, because a
    ratio against a zero median has no value and rendering it as `0.0000` or
    `Infinity` would put a number in front of an operator meaning neither "no
    anomaly" nor "an enormous one". That path is **reachable and is a real
    page**: a zero trailing median with today above the floor fires, which is
    the second arm of
    `test_a_zero_trailing_median_is_held_by_the_floor_and_not_by_the_comparison`.

    The summary's first spelling was `{{ $labels.spend_ratio }}x the trailing
    7-day median`, which renders *"LLM spend today is undefined (zero trailing
    median)x the trailing 7-day median"*. Measured against
    `grafana/grafana:13.1.3` on 2026-09-11 -- by reading the rendered
    annotation off a real alert instance, which is the only place a template
    becomes a sentence. So the guard is on the adjacency rather than on the
    whole line: a label that can be prose must not be glued to a unit.
    """
    rule = _grafana_rule("Cost anomaly")
    summary = " ".join(str(rule["annotations"]["summary"]).split())
    assert "{{ $labels.spend_ratio }}" in summary, (
        f"the page no longer carries the ratio it fired on: {summary!r}"
    )
    assert not re.search(r"\$labels\.spend_ratio\s*\}\}\s*[a-zA-Z]", summary), (
        "`{{ $labels.spend_ratio }}` is glued to a word, and that label is the sentence "
        "`undefined (zero trailing median)` on every firing with a zero trailing median "
        f"-- which is a page this rule really does send: {summary!r}"
    )
    for label in ("today_spend_usd", "trailing_median_usd"):
        assert f"{{{{ $labels.{label} }}}}" in summary, (
            f"the page does not carry {label}, so the numbers the verdict was computed "
            f"from are only in the description: {summary!r}"
        )


def test_the_cost_anomaly_description_names_its_floor_the_two_price_settings_and_its_panel() -> (
    None
):
    """A page has to land somewhere, and this one has two things to explain
    that the Prometheus three do not.

    **The floor**, because a constant that suppresses the alert is a constant
    an operator will eventually need to raise, and a number in the SQL that
    the description does not carry is a number nobody finds. It is asserted as
    *the same string* the statement uses, so the two cannot drift.

    **The two price settings**, because their defaults make this alert silent
    by construction. `llm_price_in_per_mtok` and `llm_price_out_per_mtok` both
    default to `Decimal(0)` (`src/usher/config.py`), which that file calls the
    honest value for a local model and the wrong one for a hosted model an
    operator forgot to price -- and this host's LLM is a local vLLM. With them
    unset every `cost_usd` is `0.00000000` and no multiple of zero is an
    anomaly. An operator who believes this rule is watching a hosted model has
    to be told where to look, in the page itself.

    The panel is on **Dashboard 5**, not 3, and is checked against
    `05-cost-and-compliance.json` for the reason the Prometheus case checks
    Dashboard 3's: renaming a panel should be red here rather than silently
    pointing a page at a screen that no longer exists.
    """
    rule = _grafana_rule("Cost anomaly")
    description = " ".join(str(rule["annotations"]["description"]).split())

    assert rule.get("for"), "PRD 10's conditions are all durations; this rule has no `for:`"
    assert rule["labels"]["severity"], "no severity"
    assert rule["annotations"]["summary"], "no summary"
    assert rule["noDataState"] == "OK", (
        "a ledger with no rows is a deployment that has not curated, not an anomaly; "
        f"this rule answers no-data with {rule['noDataState']!r}"
    )

    floor = re.findall(r">= (\d+\.\d+)\b", cost_anomaly_sql())[0]
    assert floor in description, (
        f"the statement does not fire below {floor} and the description never says so, so "
        "the one number an operator has to change is only in the SQL"
    )
    for setting in ("llm_price_in_per_mtok", "llm_price_out_per_mtok"):
        assert setting in description, (
            f"the description does not name {setting}, whose default of 0 is what makes "
            "an unpriced deployment silent"
        )
    # **Qualified, and the `or column in description` fallback was deleted after
    # a plant survived it.** `llm_calls.at`'s column name is `at`, which is a
    # substring of "that", "later" and a dozen other words this paragraph
    # contains -- so the unqualified arm graded the description green with the
    # column removed. The qualified form is also what an operator can paste.
    for table, column in _sql_pairs(cost_anomaly_sql()):
        assert f"{table}.{column}" in description, (
            f"the description does not name {table}.{column}, which the rule fires on"
        )

    panels = panel_titles(_DASHBOARD_FIVE)
    named = re.findall(r'"([^"]+)" on dashboard 5', description)
    assert named, (
        "the description names no Dashboard 5 panel, so a page lands on a query rather "
        f"than on a screen: {description!r}"
    )
    for title in named:
        assert title in panels, (
            f"names panel {title!r}, which `05-cost-and-compliance.json` does not hold: "
            f"{sorted(panels)}"
        )


def test_the_disk_growth_statement_names_only_tables_and_columns_this_schema_holds() -> None:
    """Invariant 1 for the half of *Disk projection* that has no series.

    Same argument as *Cost anomaly*'s: a `SELECT` naming a column nobody stores
    raises rather than reading healthy, and `execErrState: Error` carries that
    to a human -- but a column that exists on a **different** table, or a table
    renamed by a migration while the rule keeps the old name, is only visible
    against `Base.metadata`.

    ⚠️ **Seven relations, and the statement is deliberately unaliased so that
    every one of them is graded.** An alias is not a table name and an aliased
    statement yields no pairs, so the count assertion is what notices if
    somebody tidies it.
    """
    pairs = _sql_pairs(disk_growth_sql())
    assert len(pairs) >= 7, (
        "the table.column scan found fewer than the seven relations this statement "
        f"counts, so it has been rewritten in aliases or the scan has stopped reading it: "
        f"{pairs}"
    )
    for table, column in pairs:
        columns = Base.metadata.tables[table].columns
        assert column in columns, (
            f"Disk projection selects {table}.{column}, which is not a column of {table}: "
            f"{sorted(c.name for c in columns)}"
        )
    counted = {table for table, _ in pairs}
    assert counted == {
        "titles",
        "credits",
        "raw_payloads",
        "title_neighbors",
        "title_embeddings",
        "tmdb_ids",
        "people",
    }, (
        "the set of relations this statement prices has changed. Every one of them has to "
        "carry a creation timestamp -- `title_search_names` and `images` do not, which is "
        f"the coverage gap the rule's own description states: {sorted(counted)}"
    )


def test_the_disk_growth_rule_hands_grafana_exactly_one_numeric_column() -> None:
    """🔴 D14's finding, applied to the rule that arrived after it.

    Grafana turns every **numeric** column of a table frame into a series the
    condition is evaluated over and every **string** column into a label on it.
    The threshold is `> 0`. So a second numeric column -- `added_bytes_7d`,
    which is a large positive number on any deployment that has ingested
    anything -- would be judged by that same threshold and this alert would fire
    on every evaluation forever, with a page naming no growth. The `::text`
    casts are the rule's wiring, not its formatting, and they were confirmed
    rather than assumed: run against the live catalog on 2026-09-11 the
    statement's output types are `integer` once and `text` five times.
    """
    rule = _grafana_rule("Disk projection")
    outputs = disk_growth_sql().rsplit("\nSELECT\n", 1)[1]
    columns = _AN_OUTPUT_ALIAS.findall(outputs)
    assert columns[:1] == ["fired"], (
        f"the value column is no longer first or no longer named `fired`: {columns}"
    )
    assert len(columns) == 6, f"expected six output columns, found {columns}"
    assert outputs.count("::text") == len(columns) - 1, (
        "every column but `fired` has to reach Grafana as a *label*, which means a "
        f"`::text` each; this statement casts {outputs.count('::text')} of "
        f"{len(columns) - 1}: {outputs}"
    )

    fired = outputs.split("AS fired", 1)[0]
    assert "::text" not in fired and "THEN 1 ELSE 0 END" in fired, (
        f"`fired` is not a bare 0/1 numeric column: {fired!r}"
    )

    condition = next(query for query in rule["data"] if query["refId"] == rule["condition"])
    assert condition["model"]["type"] == "threshold", (
        "the condition is no longer a threshold, so the `> 0` this statement is written "
        f"against is not what decides: {condition['model']}"
    )
    assert condition["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [0]}, (
        "the threshold is not `> 0`, which puts a second number in the decision beside "
        f"the comparison in the SQL: {condition['model']['conditions']}"
    )

    interpolated = set(re.findall(r"\$labels\.(\w+)", str(rule["annotations"])))
    assert interpolated, "the page interpolates no label, so it names no subject"
    assert interpolated <= set(columns), (
        "the page interpolates labels this statement does not return, so they render "
        f"empty: {sorted(interpolated - set(columns))}"
    )


def test_no_grafana_rule_carries_a_zero_width_relative_time_range() -> None:
    """D14 measured that Grafana rejects `from: 0, to: 0`, and that a rejected
    file provisions **no** rules at all rather than one bad one.

    So the failure is not "this rule is missing" -- it is *every* rule in the
    file, including the one that was there first. That makes it worth a check
    over the whole file rather than a note on the rule that would have caused
    it: the cost of the mistake falls on somebody else's alert.
    """
    ranges = [
        (str(rule["title"]), query["refId"], query["relativeTimeRange"])
        for rule in grafana_rules()
        for query in rule["data"]
    ]
    assert len(ranges) >= 4, (
        f"the scan found {len(ranges)} queries across the Grafana rules, so it has "
        "stopped reading the file"
    )
    zero_width = [entry for entry in ranges if entry[2]["from"] == entry[2]["to"]]
    assert zero_width == [], (
        "Grafana rejects a zero-width relative time range at provisioning time "
        "(`[alerting.alert-rule.invalidRelativeTime]`) and provisions **no** rules from a "
        f"file it rejected, so this disarms every alert beside it too: {zero_width}"
    )


def test_no_rule_takes_a_quantile_over_a_histogram_still_on_the_sdk_defaults() -> None:
    """D9's panel guard, turned on the rule file, and it is here for D12.

    `configure_metrics` installs no `View`, so a seconds-unit histogram with no
    `explicit_bucket_boundaries_advisory` takes the SDK's second-scale defaults
    and every observation under five seconds lands in one bucket.
    `histogram_quantile` over that does not fail and does not empty: D1 measured
    a flat **2.5000 s** against a true p50 of **35.20 ms**.

    On a panel that draws a plausible wrong line. **On an alert it decides
    whether the rule can fire at all**, and it decides it wrongly in both
    directions at once -- a `> 5s` threshold over such a histogram fires
    permanently on the interpolated 2.5 s the moment the bucket has two
    observations, or never fires whatever the real latency is. That is issue
    #86, and it lands on D12: PRD 10's *"Enrichment SLA missed -- demand-triggered
    p99 > 5 s"* is a quantile over `usher.enrichment.latency`, which carries no
    advisory today.

    ⚠️ **D12 landed the quantile rule this case was written for, and fixed the
    instrument rather than the expression.** `usher.enrichment.latency` now
    declares `explicit_bucket_boundaries_advisory` boundaries that bracket the
    5 s threshold, so *Enrichment SLA missed* is graded here and passes on its
    merits. The plant below therefore moved to `usher.jobs.duration`, which is
    still on the SDK defaults -- a plant naming the one instrument D12 fixed
    would have been a control that stopped controlling anything the moment it
    was fixed, which is the failure this whole module is about.

    #86 is **not** closed: thirteen seconds-unit histograms still carry no
    advisory (measured 2026-09-11). What D12 closed is the one instrument an
    alert takes a quantile of.
    """
    declared = _declared_histograms()
    seconds = {name for name, body in declared.items() if 'unit="s"' in body}
    with_advisory = {
        name for name, body in declared.items() if "explicit_bucket_boundaries_advisory" in body
    }
    assert len(declared) >= 15, (
        f"the histogram scan found only {len(declared)} declarations, so this case is vacuous"
    )
    assert seconds - with_advisory, (
        "every seconds-unit histogram now carries an advisory -- #86 is closed and this "
        "case has nothing left to protect. Delete it and say so."
    )
    unfixed = {normalise_metric(name) for name in seconds - with_advisory}

    def offenders(rules: list[dict[str, Any]]) -> list[str]:
        return [
            f"{rule['alert']}: {operand.strip()}"
            for rule in rules
            if "histogram_quantile" in str(rule["expr"])
            for _labels, operand in _aggregations(str(rule["expr"]))
            for token in _metric_tokens(operand)
            if normalise_metric(token) in unfixed
        ]

    assert "usher_enrichment_latency" not in unfixed, (
        "`usher.enrichment.latency` is back on the SDK's second-scale defaults, so the "
        "p99 the `Enrichment SLA missed` rule compares against 5 s is a flat "
        "interpolation across a bucket whose own edge is 5 s"
    )
    planted = [
        {
            "alert": "Job duration SLA",
            "expr": (
                "histogram_quantile(0.99, sum by (le) "
                "(rate(usher_jobs_duration_seconds_bucket[5m]))) > 5"
            ),
        }
    ]
    assert offenders(planted) != [], (
        "the scan cannot see a quantile over `usher.jobs.duration`, which carries no "
        "advisory -- so it would grade a quantile rule green whatever bucket boundaries "
        "it lands on"
    )
    assert offenders(committed_rules()) == [], (
        "these rules take a quantile over a histogram still on the SDK's second-scale "
        "defaults, which answers a flat ~2.5 s rather than failing -- so the threshold "
        "is compared against a number the instrument cannot produce. Use "
        "`rate(_sum)/rate(_count)` or fix the instrument's buckets (#86)."
    )


def test_every_rule_carries_a_window_a_severity_and_a_description_naming_its_series_and_panel() -> (
    None
):
    """PRD 10's alerts are pages, and a page has to land somewhere.

    D11's acceptance: each description names **the series, the label vocabulary
    and the panel it corresponds to**, so an operator woken at 2 a.m. arrives at
    a panel rather than at a PromQL prompt. All three are checked against
    something the repository already holds -- the panel titles against the
    dashboard JSON itself, so renaming a panel is red here rather than silently
    pointing a page at a panel that no longer exists.

    ⚠️ **The dashboard number is read out of the sentence rather than fixed at
    3.** D11 wrote `" on dashboard 3"` because all three of its rules landed on
    the Pipeline board and D12's two joined them there; D13's *Disk projection*
    is drawn from Dashboard 5's disk panel, and a check that could only look at
    3 would have had to be relaxed to let it through -- the shape that turns one
    exemption into a check nobody runs. Two premises rather than one, so a scan
    that has stopped reading dashboards is red before the loop grades anything.

    🔴 **And an `absent()` rule is graded differently on its labels, because it
    has none of its own.** Prometheus builds such a result's label set from the
    selector's equality matchers alone -- there is no series to take labels
    from, which is the condition -- so `{{ $labels.mountpoint }}` renders empty
    and the page names no subject. That is the same defect `sum by (le)` causes
    one paragraph down, arriving by a different route and invisible to the same
    check.

    The label half is two claims, and the second is the one with teeth. An
    annotation that renders `{{ $labels.kind }}` has to (a) explain what `kind`
    is, because a page reading *"enrich jobs are parking"* is only actionable if
    the reader knows `enrich` is a lane, and (b) survive the expression's own
    aggregations -- `sum by (le) (...)` drops `kind` and the page then says
    *"jobs are parking"* with an empty lane, which is a rendered alert that
    names no subject. The committed three aggregate nothing, so (b) grades zero
    aggregations today and its teeth are proved on a planted rule instead.

    ⚠️ **"Names the label vocabulary" is checked against the description's own
    prose, not against the attribute keys `src/usher/` emits.** Linking an
    instrument to its attributes needs dataflow -- `usher.jobs.queued` and
    `usher.jobs.parked` share one `_observations` helper and neither names a
    key at its own declaration -- and a check that guessed would be a check that
    passed. So this is the weaker claim, stated as such: every label the page
    interpolates is a label the page explains.
    """
    assert len(_dashboard_three_panel_titles()) == 10, (
        "Dashboard 3 has ten panels; this scan found "
        f"{len(_dashboard_three_panel_titles())}: {sorted(_dashboard_three_panel_titles())}"
    )
    assert len(panel_titles(_DASHBOARD_FIVE)) == 8, (
        "Dashboard 5 has eight panels; this scan found "
        f"{len(panel_titles(_DASHBOARD_FIVE))}: "
        f"{sorted(panel_titles(_DASHBOARD_FIVE))}"
    )
    boards = {3: _DASHBOARD_THREE, 5: _DASHBOARD_FIVE}

    def dropped_labels(rules: list[dict[str, Any]]) -> list[str]:
        return [
            f"{rule['alert']}: {sorted(rendered - labels)} dropped by `{operand.strip()}`"
            for rule in rules
            for rendered in [set(re.findall(r"\$labels\.(\w+)", str(rule["annotations"])))]
            for labels, operand in _aggregations(str(rule["expr"]))
            if rendered - labels
        ]

    planted = [
        {
            "alert": "Jobs parking",
            "expr": "sum by (le) (increase(usher_jobs_parked_ratio[15m])) > 0",
            "annotations": {"summary": "{{ $labels.kind }} jobs are parking"},
        }
    ]
    assert dropped_labels(planted) != [], (
        "the aggregation scan cannot see a `sum by (le)` dropping the one label the "
        "summary interpolates, so it would pass a page that names no lane"
    )

    absence = 'absent(system_filesystem_usage_bytes{state="free"})'
    assert _absence_subject_labels(absence) == {"state"}, (
        "the absence recogniser no longer reads an `absent()` rule's own equality "
        f"matchers: {_absence_subject_labels(absence)}"
    )
    assert _absence_subject_labels(f"{absence} or vector(1) > 0") is None, (
        "the absence recogniser is matching an expression that is more than one "
        "`absent()` call, so a rule with a real subject would escape the label check"
    )
    assert "mountpoint" not in (_absence_subject_labels(absence) or set()), (
        "the recogniser thinks an `absent()` result carries `mountpoint`, which is the "
        "label a page would interpolate to empty"
    )
    assert dropped_labels(committed_rules()) == [], (
        "these rules interpolate a label their own aggregation has already dropped, so "
        "the rendered page names no subject"
    )

    for rule in committed_rules():
        alert = str(rule["alert"])
        annotations = " ".join(str(rule["annotations"]).split())
        description = " ".join(str(rule["annotations"]["description"]).split())
        assert rule.get("for"), f"{alert}: PRD 10's conditions are all durations; no `for:`"
        assert rule["labels"]["severity"], f"{alert}: no severity"
        assert rule["annotations"]["summary"], f"{alert}: no summary"

        for token in _tokens_of(rule):
            assert token in description, (
                f"{alert}: the description does not name {token!r}, the series it fires on"
            )
        named_panels = re.findall(r'"([^"]+)" on dashboard (\d+)', description)
        assert named_panels, (
            f"{alert}: the description names no dashboard panel, so a page lands on a "
            f"query rather than on a screen: {description!r}"
        )
        for named, number in named_panels:
            assert int(number) in boards, (
                f"{alert}: names a panel on dashboard {number}, which this case does not "
                f"know how to read: {sorted(boards)}"
            )
            titles = panel_titles(boards[int(number)])
            assert named in titles, (
                f"{alert}: names panel {named!r} on dashboard {number}, which "
                f"`{boards[int(number)].name}` does not hold: {sorted(titles)}"
            )
        rendered = set(re.findall(r"\$labels\.(\w+)", annotations))
        subject = _absence_subject_labels(str(rule["expr"]))
        if subject is None:
            assert rendered, f"{alert}: the page interpolates no label, so it names no subject"
        else:
            assert rendered <= subject, (
                f"{alert}: `absent()` carries only its own equality matchers "
                f"{sorted(subject)}, because there is no series to take labels from -- "
                f"{sorted(rendered - subject)} renders empty and the page names no subject"
            )
        for label in rendered:
            assert re.search(rf"\b{label}\b", description), (
                f"{alert}: interpolates {{{{ $labels.{label} }}}} and never says what "
                f"{label!r} is, so the page is a value with no vocabulary"
            )


def test_the_prd_alert_table_parse_is_falsifiable() -> None:
    """The parse above has to be able to come back wrong, in both its halves.

    The section scope and the row filter are separate failures. A section regex
    that cannot reach the end of the file hands back nothing -- and `## Alerts`
    is the last heading in PRD 10 today, so that is not hypothetical. A row
    filter that keeps the header turns seven alerts into eight, one of them
    called "Alert", and the equality check in the named case then fails for a
    reason nobody can read.
    """
    document = (
        "# Telemetry\n\n"
        "## Alerts\n\n"
        "Kept few, so they mean something:\n\n"
        "| Alert | Condition |\n"
        "|---|---|\n"
        "| Ingest stalled | Queue depth rising for 30 min with zero completions |\n"
        "| Push down | `push.connected == 0` for 15 min on a source that supports it |\n"
    )
    parsed = prd_alerts(document)
    assert parsed == {
        "Ingest stalled": "Queue depth rising for 30 min with zero completions",
        "Push down": "`push.connected == 0` for 15 min on a source that supports it",
    }, f"the table parse does not read a two-row table at the end of a file: {parsed}"

    assert prd_alerts(document + "\n## Something else\n\n| Nope | Not an alert |\n") == parsed, (
        "the section scope has leaked past `## Alerts` and is reading another section's "
        "table as alerts"
    )
    assert prd_alerts("# Telemetry\n\nNo alert section here.\n") == {}, (
        "a document with no `## Alerts` section parses to something, so the section "
        "regex is matching the whole file"
    )


def test_the_declaration_walk_does_not_read_the_prose_that_answers_it() -> None:
    """`telemetry.py` argues about `create_observable_gauge` in a comment and in
    a docstring, and a text scan reads both as declarations.

    Measured 2026-09-11: `source.find("create_observable_gauge(")` over
    `src/usher/` reports two more gauges than exist, and one of the two supplies
    the name `usher.jobs.queued` with **no `unit=`** -- which would derive its
    stored spelling as `usher_jobs_queued`, the one name that never fires. The
    check meant to catch that spelling would have been the thing that accepted
    it.

    So the walk is over an AST with docstrings removed. This case is that claim,
    made falsifiable: the prose is real, it is quoted here, and a walk that
    reads it produces a unitless duplicate.
    """
    prose = '''
class Meter:
    """A docstring that argues about the SDK discarding a second registration:

    a second `create_observable_gauge("usher.jobs.queued", callbacks=[other])`
    against the same provider is silently discarded.
    """

    def register(self) -> None:
        # And a comment that spells it too:
        # create_observable_gauge("usher.jobs.queued", callbacks=[other])
        meter.create_observable_gauge("usher.jobs.queued", unit="1")
'''
    tree = ast.parse(prose)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            node.value = ast.Constant(value=None)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _INSTRUMENT_FACTORIES
    ]
    assert len(calls) == 1, (
        f"the docstring-stripped AST walk found {len(calls)} declarations in a module that "
        "declares one and describes two"
    )
    assert prose.count("create_observable_gauge(") == 3, (
        "the quoted prose no longer contains the two non-declarations this case exists to "
        "show a text scan reading"
    )

    declared = _instrument_declarations()
    names = [name for name, _factory, _unit in declared]
    assert names.count("usher.jobs.queued") == 1, (
        "`src/usher/telemetry.py` declares `usher.jobs.queued` once and writes it twice "
        f"more in prose; the walk found it {names.count('usher.jobs.queued')} times"
    )
    assert ("usher.jobs.queued", "create_observable_gauge", "1") in declared, (
        'the walk lost the `unit="1"` on the real declaration, which is the whole of the '
        "stored spelling"
    )


# One range-vector selector, `name[5m]` or `name{matchers}[5m]`. Used to
# compare a ratio's two sides matcher-for-matcher rather than by eyeball.
#
# **The matcher block is optional and the `[` lookahead is what makes that
# safe.** `Provider degraded`'s denominator is a bare
# `usher_provider_requests_total[5m]` -- which is the whole point of a
# denominator -- and a pattern requiring `{...}` finds one selector in a
# two-sided ratio and grades the rule against itself.
_SELECTOR = re.compile(r"\b(?P<name>[a-z_][a-z0-9_]*)(?:\{(?P<matchers>[^}]*)\})?(?=\[)")
_MATCHER = re.compile(r"(?P<label>\w+)\s*(?P<op>=~|!~|!=|=)\s*\"(?P<value>[^\"]*)\"")


def _matchers(block: str | None) -> dict[str, tuple[str, str]]:
    return {
        m.group("label"): (m.group("op"), m.group("value")) for m in _MATCHER.finditer(block or "")
    }


def test_every_quantile_rule_collapses_the_labels_it_is_not_a_quantile_of() -> None:
    """🔴 A `histogram_quantile` without `by (le)` is a quantile *per label set*.

    `usher.enrichment.latency` carries `outcome` as well as `trigger`, so
    `histogram_quantile(0.99, rate(..._bucket[5m]))` -- no aggregation at all --
    computes one p99 for `outcome="enriched"` and another for
    `outcome="failed"`, and PRD 10's *"demand-triggered p99 > 5 s"* is neither
    of them. The failure is quiet in the way this module is about: both numbers
    are plausible, both draw a line, and the alert fires on whichever crosses
    first -- most likely the failures, whose latency is a timeout rather than a
    fetch.

    **`le` must be in the grouping and every other label of the histogram must
    not be**, except one the selector has already pinned to a single value.
    That exception is why the committed rule groups `by (le, trigger)`: keeping
    a pinned label changes no arithmetic and is what lets the page name its
    subject, which
    `test_every_rule_carries_a_window_a_severity_and_a_description_naming_its_series_and_panel`
    separately requires.

    This is a static defect and needs no Prometheus, which is the point: the
    live firing in `dashboards/README.md` proves the rule *can* fire, and a
    firing cannot tell you it fired on the wrong population.
    """
    quantile_rules = [
        rule for rule in committed_rules() if "histogram_quantile" in str(rule["expr"])
    ]
    assert len(quantile_rules) == 1, (
        "this case grades the quantile rules and found "
        f"{len(quantile_rules)}: {[r['alert'] for r in quantile_rules]}"
    )

    def offenders(rules: list[dict[str, Any]]) -> list[str]:
        found: list[str] = []
        for rule in rules:
            expr = str(rule["expr"])
            # Only the quantile rules. Without this the ratio rules below are
            # graded as quantiles with no aggregation and the case is red for a
            # correct tree -- the shape `.claude/rules/testing-discipline.md`
            # calls a check that has stopped reading its own subject.
            if "histogram_quantile" not in expr:
                continue
            aggregations = [
                (labels, operand)
                for labels, operand in _aggregations(expr)
                if any("_bucket" in token for token in _metric_tokens(operand))
            ]
            if not aggregations:
                found.append(f"{rule['alert']}: the quantile wraps no aggregation at all")
                continue
            for labels, operand in aggregations:
                if "le" not in labels:
                    found.append(f"{rule['alert']}: `{operand.strip()}` is not grouped by `le`")
                pinned = {
                    label
                    for selector in _SELECTOR.finditer(operand)
                    for label, (op, _value) in _matchers(selector.group("matchers")).items()
                    if op == "="
                }
                for label in sorted(labels - {"le"} - pinned):
                    found.append(
                        f"{rule['alert']}: groups by {label!r}, which the selector does not "
                        "pin, so this is one quantile per value of it"
                    )
        return found

    planted = [
        {
            "alert": "Enrichment SLA missed",
            "expr": (
                "histogram_quantile(0.99, sum by (le, outcome) "
                '(rate(usher_enrichment_latency_seconds_bucket{trigger="demand"}[5m]))) > 5'
            ),
        },
        {
            "alert": "No aggregation at all",
            "expr": (
                "histogram_quantile(0.99, "
                'rate(usher_enrichment_latency_seconds_bucket{trigger="demand"}[5m])) > 5'
            ),
        },
    ]
    assert len(offenders(planted)) == 2, (
        "the scan does not catch a quantile grouped by an unpinned `outcome`, nor one "
        f"taken over no aggregation at all: {offenders(planted)}"
    )
    assert offenders(committed_rules()) == [], (
        "these rules take a quantile per label set rather than over the population PRD 10 "
        f"names: {offenders(committed_rules())}"
    )


def test_the_provider_degraded_ratio_counts_transport_failures_on_both_sides() -> None:
    """🔴 `error` in the denominator only makes the ratio *fall* during an outage.

    PRD 10 states one half of this and not the other: *"a denominator that
    omitted the failures would read low exactly during an outage."* The
    numerator half follows and is nowhere written down -- a transport failure
    never reached a status line, so `status="error"` is what
    `adapters/tmdb/client.py` records for it, and a numerator matching only
    `429|5..` counts it nowhere. In the limit that is silent: when *every*
    request fails in the transport, the numerator is 0, the denominator is the
    error count, and the rule reads a healthy **0%** during a total outage.
    `promtool` drives exactly that case in `dashboards/README.md`; this case is
    the static half, which is the one that survives somebody rewriting the
    expression.

    **The two sides must differ only by the status match.** A denominator that
    also picked up an extra matcher -- or lost the one the numerator has -- is a
    ratio of two different populations, which still renders a plausible number.
    """
    expr = str(_rule("Provider degraded")["expr"])
    selectors = [
        (match.group("name"), _matchers(match.group("matchers")))
        for match in _SELECTOR.finditer(expr)
    ]
    assert len(selectors) == 2, (
        f"this rule is a ratio and should hold exactly two selectors; found {selectors}"
    )
    (numerator_name, numerator), (denominator_name, denominator) = selectors
    assert numerator_name == denominator_name == "usher_provider_requests_total", (
        f"the two sides name different series: {numerator_name} over {denominator_name}"
    )

    assert "status" in numerator, (
        f"the numerator has no `status` matcher, so it selects every request: {expr!r}"
    )
    operator, pattern = numerator["status"]
    assert operator == "=~", f"the numerator's `status` match is {operator!r}, not a regex"
    assert "error" in pattern, (
        "`error` is not in the numerator's status match, so a transport failure is counted "
        "in the denominator alone and this ratio *falls* during a total outage -- the "
        f"quietest way for this rule to be wrong: {pattern!r}"
    )
    assert "429" in pattern, f"PRD 10's condition names 429 and this pattern does not: {pattern!r}"
    assert "5.." in pattern, (
        f"PRD 10's condition names 5xx; `5..` is the anchored spelling: {pattern!r}"
    )
    assert "4.." not in pattern, (
        "a `4..` class puts 404 in the numerator, which is a title TMDb does not have "
        f"rather than a degraded provider: {pattern!r}"
    )

    assert {label: match for label, match in numerator.items() if label != "status"} == {
        label: match for label, match in denominator.items() if label != "status"
    }, (
        "the numerator and denominator differ by something other than the status match, "
        f"so this is a ratio of two different populations: {numerator} over {denominator}"
    )
    assert "status" not in denominator, (
        "the denominator filters on `status` too, so it is not the total request count "
        f"PRD 10's rate is taken against: {denominator}"
    )
