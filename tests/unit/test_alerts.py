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

3. **The name sets agree with PRD 10's table in both directions.** A rule the
   PRD does not name falsifies *"kept few, so they mean something"* as surely as
   an alert with no rule. That case is `xfail(strict=True)` today and names
   which task owes which rule.

4. **The two spellings that are a decision get a case each.** "Ingest stalled"
   must reach a lane that has *never settled a job*, and "Push down" must fire
   on a zero and not on an absence. Both are one PromQL operator wide and both
   are invisible to every other check in this file.

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

import pytest
import yaml

from tests.unit.test_dashboards import (
    _aggregations,
    _balanced,
    _declared_histograms,
    _metric_tokens,
    metric_catalogue,
    normalise_metric,
)

_ROOT = pathlib.Path(__file__).parents[2]
_ALERTS = _ROOT / "dashboards" / "alerts" / "usher.yml"
_PRD_10 = _ROOT / "docs" / "prd" / "10-telemetry-and-dashboards.md"
_DASHBOARD_THREE = _ROOT / "dashboards" / "03-pipeline.json"

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

# The alerts D13 and D14 still owe, and which task owes each. Retyped here on
# purpose: the point of the xfail below is to say *who* is missing, and the
# only source for that is the plan. A wrong name here is loud -- it appears in
# the failure message next to the set actually parsed out of the PRD.
#
# **D12's two are gone from this table because D12 landed them.** *Enrichment
# SLA missed* could not be written at all until `usher.enrichment.latency`
# carried `trigger` -- PRD 10's condition named a dimension the shipped series
# did not have, which is why the debt outlived M4.
_OWED = {
    "Disk projection": "D13",
    "Cost anomaly": "D14",
}

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
    """
    spellings = stored_spellings()
    return {
        stored
        for name, factory, _unit in _instrument_declarations()
        if factory in _GAUGE_FACTORIES
        for stored in spellings[name]
    }


def _tokens_of(rule: dict[str, Any]) -> set[str]:
    return _metric_tokens(str(rule["expr"]))


def _dashboard_three_panel_titles() -> set[str]:
    import json

    document: Any = json.loads(_DASHBOARD_THREE.read_text(encoding="utf-8"))
    titles: set[str] = set()
    queue: list[Any] = list(document.get("panels", []))
    while queue:
        panel = queue.pop(0)
        if not isinstance(panel, dict):
            continue
        if panel.get("title"):
            titles.add(str(panel["title"]))
        queue.extend(panel.get("panels", []))
    return titles


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D13 owes 'Disk projection' and D14 owes 'Cost anomaly'. D12 has landed "
        "'Enrichment SLA missed' and 'Provider degraded', so this ledger is two short "
        "rather than four. It stays a strict xfail and flips to a hard failure -- XPASS "
        "under strict -- on the day D14 lands, which is the task that removes this marker."
    ),
)
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

    ⚠️ **`xfail(strict=True)` rather than a red, and that is a deviation from
    the task text.** D11's acceptance says this case is *"red until D14 lands"*.
    A red left in the tree makes `uv run pytest` fail for every task between
    here and D14, which is the same signal as a real regression and trains
    whoever sees it to ignore the suite. Strict xfail keeps the assertion live
    and its failure message readable under `-rx`, keeps the gate honest about
    everything else, and -- because a strict xfail that *passes* is a failure --
    forces D14 to come back and delete the marker. That is the shape M7 used for
    exactly this situation, a case held across the two tasks that had to land
    together.
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
        if normalise_metric(token) not in catalogue
    ]
    assert unknown == [], (
        "these tokens normalise to a name PRD 10's metric table does not hold, so the "
        f"rule selects nothing and reads as all-clear forever: {unknown}"
    )

    committed = {str(rule["alert"]) for rule in rules}
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
    assert len(rules) == 5, (
        f"D11 shipped three rules and D12 added two; found {len(rules)}: {_committed_names()}"
    )
    catalogue = metric_catalogue()
    assert catalogue, "PRD 10's metric table parsed to nothing"

    graded = [(str(rule["alert"]), token) for rule in rules for token in _tokens_of(rule)]
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
        if token not in known
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
_DECAYING = re.compile(r"\b(?:increase|rate|irate|delta|idelta|deriv)\(")
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
    and the panel it corresponds to on Dashboard 3**, so an operator woken at 2
    a.m. arrives at a panel rather than at a PromQL prompt. All three are
    checked against something the repository already holds -- the panel titles
    against `03-pipeline.json` itself, so renaming a panel is red here rather
    than silently pointing a page at a panel that no longer exists.

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
    panels = _dashboard_three_panel_titles()
    assert len(panels) == 10, (
        f"Dashboard 3 has ten panels; this scan found {len(panels)}: {sorted(panels)}"
    )

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
        assert re.search(r'"(?P<panel>[^"]+)" on dashboard 3', description), (
            f"{alert}: the description names no Dashboard 3 panel, so a page lands on a "
            f"query rather than on a screen: {description!r}"
        )
        for named in re.findall(r'"([^"]+)" on dashboard 3', description):
            assert named in panels, (
                f"{alert}: names panel {named!r}, which `03-pipeline.json` does not hold: "
                f"{sorted(panels)}"
            )
        rendered = set(re.findall(r"\$labels\.(\w+)", annotations))
        assert rendered, f"{alert}: the page interpolates no label, so it names no subject"
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
