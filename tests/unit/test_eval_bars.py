"""The bar file, its hash, and the four verdicts a bar can return."""

import inspect
import tomllib
from pathlib import Path

import pytest

from usher.eval.bars import BarSet, Judgement, load_bars

_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED = _ROOT / "docs" / "evals" / "bars.toml"


def _bar(
    *,
    kind: str,
    surface: str = "s",
    tier: str = "t",
    metric: str = "m",
    stratum: str = "all",
    low: float | None = None,
    high: float | None = None,
    source: str | None = "why this bar was registered",
) -> str:
    """One `[[bar]]` table, rendered as the text a bars file holds.

    TOML text rather than a `Bar` handed straight to `BarSet`, because the
    file is the artefact: a helper that constructed the dataclass would
    exercise `judge` against bars `load_bars` would never have accepted, which
    is how a refusal at load reads as covered while nothing calls it.
    """
    fields: dict[str, float | str | None] = {
        "surface": surface,
        "tier": tier,
        "metric": metric,
        "stratum": stratum,
        "kind": kind,
        "low": low,
        "high": high,
        "source": source,
    }
    lines = [
        f"{name} = {value!r}" if isinstance(value, str) else f"{name} = {value}"
        for name, value in fields.items()
        if value is not None
    ]
    return "[[bar]]\n" + "\n".join(lines) + "\n"


def _bars(tmp_path: Path, body: str) -> BarSet:
    path = tmp_path / "bars.toml"
    path.write_text(body)
    return load_bars(path)


def test_the_shipped_bar_file_loads() -> None:
    """The file `usher eval` will actually read.

    read through the loader that will actually read it.

    A bars file is data, so every other case in this module builds its own --
    and a module of cases over synthetic files passes just as happily against
    a shipped file that has been gutted, renamed or moved. This is the one
    case that says the real one exists and parses.
    """
    bars = load_bars(_SHIPPED)

    assert bars.sha256
    assert len(bars.sha256) == 64, f"a sha256 is 64 hex characters, not {bars.sha256!r}"
    assert bars.path == _SHIPPED, (
        "the set records the file it came from, so a ledger row says which bars "
        "a run faced; a hard-coded path would name the right file for the wrong "
        f"reason: {bars.path}"
    )
    assert (
        bars.find(surface="suggest", tier="prefix", metric="recall_at_5", stratum="all") is not None
    )


def test_the_registered_numbers_are_the_ones_that_were_registered() -> None:
    """The five bars E1 pre-registered, pinned as **literals**."""
    bars = load_bars(_SHIPPED)

    registered = {
        (one.surface, one.tier, one.metric, one.stratum): (one.kind, one.low, one.high)
        for one in bars.bars
    }
    assert registered == {
        ("suggest", "prefix", "recall_at_5", "all"): ("window", 0.016, 0.028),
        ("suggest", "prefix", "latency_p95_ms", "all"): ("floor", 0.0, 10.0),
        ("suggest", "fuzzy", "recall_at_5", "all"): ("pending", None, None),
        ("suggest", "fuzzy", "recall_at_5", "band=2-4"): ("pending", None, None),
        ("suggest", "fuzzy", "recall_at_5", "typo_class=transposition"): ("pending", None, None),
    }
    assert all(one.source.strip() for one in bars.bars), (
        "a bar with no `source` is a threshold with no argument behind it, "
        "which is the thing pre-registration exists to prevent: "
        f"{[one.metric for one in bars.bars if not one.source.strip()]}"
    )


def test_the_three_pending_suggest_bars_are_three_bars_and_not_one_found_three_times() -> None:
    """The shipped file holds three bars that agree on surface.

    tier and metric and differ only in `stratum`, which is what makes it the file that
    can tell a four-key lookup from a three-key one.

    A `find` that ignored `stratum` would answer the `all` bar for every one
    of the three -- all three are `pending`, so `judge` would agree and every
    verdict in this module would still be right, while the *bar* a report
    names and the reasoning it quotes would belong to another stratum.
    """
    bars = load_bars(_SHIPPED)
    strata = ("all", "band=2-4", "typo_class=transposition")

    found = [
        bars.find(surface="suggest", tier="fuzzy", metric="recall_at_5", stratum=one)
        for one in strata
    ]
    assert [one.stratum for one in found if one is not None] == list(strata)
    assert len({one.source for one in found if one is not None}) == 3, (
        "the three strata came back carrying one reason between them, so the "
        "lookup is answering the same bar three times"
    )


def test_the_hash_changes_when_the_file_changes(tmp_path: Path) -> None:
    """The hash is the whole mechanism.

    If it did not move with the content, a bar edited after seeing a number would be
    invisible.
    """
    one = tmp_path / "one.toml"
    one.write_text(_bar(kind="floor", low=0.5))
    first = load_bars(one).sha256
    one.write_text(_bar(kind="floor", low=0.6))
    assert load_bars(one).sha256 != first


def test_the_hash_is_over_the_bytes_so_a_comment_edited_after_the_fact_moves_it(
    tmp_path: Path,
) -> None:
    """A digest over the *parsed* document passes the case above and is still the wrong digest.

    The claim the hash makes is "these are the bars that run faced, and this
    is the argument that was written beside them". A comment is where that
    argument lives -- `bars.toml`'s three pending entries are four fifths
    prose -- so a digest that could not see a comment being rewritten after
    the result was known would leave the pre-registration claim unfalsifiable
    in exactly the direction it is made in.

    The second half is the control: two files whose **bytes** are identical
    hash identically, so the answer is a function of the content and not of
    the path, the mtime or the call. Without it, "the hash moved" is also what
    a digest of a random number produces.
    """
    body = _bar(kind="window", low=0.1, high=0.2)
    quiet = tmp_path / "quiet.toml"
    quiet.write_text(body)
    before = load_bars(quiet).sha256

    quiet.write_text("# re-argued once the number was known\n" + body)
    assert load_bars(quiet).sha256 != before, (
        "a comment was rewritten and the digest did not move, so the hash is "
        "over the parsed tables rather than over the file"
    )

    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text(body)
    assert load_bars(elsewhere).sha256 == before, (
        "two files holding the same bytes hashed differently, so the digest is "
        "not a function of the content alone"
    )


def test_a_window_fails_in_both_directions(tmp_path: Path) -> None:
    """A window exists because *movement either way* means the thing measured is not the thing.

    that was measured before, so a window checked on one side is a floor wearing a
    window's name.
    """
    bars = _bars(tmp_path, _bar(kind="window", low=0.016, high=0.022))
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.019) is Judgement.PASS
    )
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.004) is Judgement.FAIL
    )
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.400) is Judgement.FAIL
    )


def test_a_window_admits_the_two_bounds_it_names(tmp_path: Path) -> None:
    """`[low, high]` is closed at both ends, and only a value *at* a bound can say so.

    Every other case in this module sits comfortably inside or outside, so
    `<` for `<=` on either end -- the likeliest single-character defect in the
    module -- is invisible to all of them. The bounds are dyadic (0.5, 0.75)
    so the comparison is exact rather than nearly exact.
    """
    bars = _bars(tmp_path, _bar(kind="window", low=0.5, high=0.75))
    assert bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.5) is Judgement.PASS
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.75) is Judgement.PASS
    )
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.25) is Judgement.FAIL
    )
    assert bars.judge(surface="s", tier="t", metric="m", stratum="all", value=1.0) is Judgement.FAIL


def test_a_floor_fails_only_below(tmp_path: Path) -> None:
    """The comparison inverted.

    a floor that refuses everything above it -- is the other single-character defect,
    and this is what sees it.
    """
    bars = _bars(tmp_path, _bar(kind="floor", low=0.5))
    assert bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.9) is Judgement.PASS
    assert bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.4) is Judgement.FAIL


def test_a_floor_with_a_ceiling_fails_above_it(tmp_path: Path) -> None:
    """The shipped latency bar is spelled this way.

    a floor of 0.0 with a ceiling of 10.0, because the failure direction is slow -- so a
    `high` the floor branch ignores would leave the one bar in the file that gates on a
    latency gating on nothing.
    """
    bars = _bars(tmp_path, _bar(kind="floor", low=0.0, high=10.0))
    assert bars.judge(surface="s", tier="t", metric="m", stratum="all", value=4.0) is Judgement.PASS
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=40.0) is Judgement.FAIL
    )


def test_a_floor_admits_the_floor_itself_and_the_ceiling_itself(tmp_path: Path) -> None:
    """The boundary case for the other kind.

    because a wrong implementation is free to branch on `kind` and spell one comparison
    strictly.

    `>= low` and `<= high` are what the file's own header claims, and a value
    landing exactly on a registered bound is not hypothetical: the latency
    bar's floor is 0.0 and a suggest tier that answered nothing would report
    exactly that.
    """
    bars = _bars(tmp_path, _bar(kind="floor", low=0.0, high=10.0))
    assert bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.0) is Judgement.PASS
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=10.0) is Judgement.PASS
    )
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=-0.5) is Judgement.FAIL
    )
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=10.5) is Judgement.FAIL
    )


def test_a_pending_bar_never_gates(tmp_path: Path) -> None:
    """No number is wrong against a bar that does not exist yet.

    Reporting PENDING rather than PASS keeps a run from claiming a bar it never faced.
    """
    bars = _bars(tmp_path, _bar(kind="pending"))
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.0) is Judgement.PENDING
    )
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=1.0) is Judgement.PENDING
    )


def test_the_four_judgements_are_four_different_strings_and_pending_is_not_spelled_pass() -> None:
    """The verdicts are values on the wire.

    a report line, a ledger row, an exit code -- so what they *are* matters as much as
    which one is returned.
    """
    assert [one.value for one in Judgement] == ["pass", "fail", "pending", "unbarred"]
    assert len({Judgement.PASS, Judgement.FAIL, Judgement.PENDING, Judgement.UNBARRED}) == 4, (
        "two members share a value, so one is an alias of the other and the "
        "distinction the four exist to draw has silently collapsed: "
        f"{[(one.name, one.value) for one in Judgement]}"
    )


def test_an_unbarred_metric_is_unbarred_rather_than_passing(tmp_path: Path) -> None:
    """A metric nobody wrote a bar for must not read as green.

    That is how a surface gets added and silently gates on nothing.
    """
    bars = _bars(tmp_path, _bar(kind="pending"))
    assert (
        bars.judge(surface="s", tier="t", metric="other", stratum="all", value=0.9)
        is Judgement.UNBARRED
    )


@pytest.mark.parametrize(
    ("surface", "tier", "metric", "stratum"),
    [
        ("other", "t", "m", "all"),
        ("s", "other", "m", "all"),
        ("s", "t", "other", "all"),
        ("s", "t", "m", "other"),
    ],
)
def test_a_bar_is_found_by_all_four_of_its_keys(
    tmp_path: Path, surface: str, tier: str, metric: str, stratum: str
) -> None:
    """A lookup that compared three of the four keys would answer this bar for a question it was.

    not registered against.

    That is not a hypothetical shape in this file: `bars.toml` holds three
    bars agreeing on surface, tier and metric and differing only in stratum,
    and E2 adds surfaces that will share metric names with this one. One arm
    per key, because a case varying only the metric -- which is the obvious
    one to write -- is passed by three of the four wrong implementations.
    """
    bars = _bars(tmp_path, _bar(kind="window", low=0.1, high=0.2))
    assert (
        bars.judge(surface="s", tier="t", metric="m", stratum="all", value=0.15) is Judgement.PASS
    ), (
        "the premise: the exact key is barred, so an UNBARRED below is about "
        "the key that was changed and not about a bar that was never found"
    )
    assert (
        bars.judge(surface=surface, tier=tier, metric=metric, stratum=stratum, value=0.15)
        is Judgement.UNBARRED
    )


def test_the_four_lookup_keys_cannot_be_handed_over_positionally() -> None:
    """The four keys are keyword-only, and that is a guard rather than a style.

    `surface`, `tier`, `metric` and `stratum` are four adjacent `str`
    parameters, and the middle pair reads most alike -- they are also the pair
    E2's new surfaces will be inventing. Transposed positionally, the lookup
    finds nothing, `judge` answers `UNBARRED`, and `UNBARRED` fails at no
    level: not the judgement, not the run's verdict, not the exit code. So the
    defect's entire symptom is a gate that stopped gating, and `mypy` cannot
    help -- measured against `f392bec`, `judge("suggest", "prefix", "all",
    "recall_at_5", 0.400)` type-checks clean and turns a `fail` into an
    `unbarred`.

    Asserted over the **signature** rather than by making the call, because the
    call that would prove it is one `mypy` rejects in this file -- which is the
    point of the change, and would make this module fail the gate rather than
    the case. The premise guard is not decoration either: a scan over a
    signature that has lost its parameters passes exactly like a scan over one
    that kept them keyword-only.
    """
    for method in (BarSet.find, BarSet.judge, BarSet.judge_with_bar):
        kinds = {
            name: parameter.kind
            for name, parameter in inspect.signature(method).parameters.items()
            if name != "self"
        }
        assert kinds, f"the premise: {method.__name__} still takes arguments at all"
        assert set(kinds.values()) == {inspect.Parameter.KEYWORD_ONLY}, (
            f"{method.__name__} takes a key positionally, so `metric` and "
            "`stratum` can be transposed at a call site and the answer is "
            f"UNBARRED rather than an error: {kinds}"
        )


def test_the_bar_and_the_verdict_come_from_one_lookup() -> None:
    """A ledger row carries both.

    the verdict, and the `kind`/`low`/`high` of the bar it was reached against -- so the
    two have to be about the same bar.

    The obvious way to fill such a row in is `find` and then `judge`, which
    scans the bars twice and re-derives the key. That agrees today and is one
    edit to either scan away from a row quoting one bar's thresholds beside
    another bar's judgement, with nothing anywhere able to notice. So the
    single lookup is the one this method exposes, and `judge` delegates to it.

    The shipped file is what makes the case say something rather than restate
    the implementation: three of its bars agree on surface, tier and metric and
    differ only in `stratum`, so a second lookup that dropped a key would hand
    back a *different, real* bar beside the same verdict -- which is precisely
    the failure a synthetic one-bar file cannot exhibit.
    """
    bars = load_bars(_SHIPPED)
    key = {"surface": "suggest", "tier": "fuzzy", "metric": "recall_at_5"}

    bar, judgement = bars.judge_with_bar(**key, stratum="band=2-4", value=0.5)
    assert bar is not None, "the premise: that stratum really is registered"
    assert bar.stratum == "band=2-4", (
        "the bar handed back beside the verdict belongs to another stratum, so "
        f"a row would quote its reasoning against this one's number: {bar}"
    )
    assert bar is bars.find(**key, stratum="band=2-4")
    assert judgement is bars.judge(**key, stratum="band=2-4", value=0.5)
    assert judgement is Judgement.PENDING

    absent, unbarred = bars.judge_with_bar(**key, stratum="band=never-registered", value=0.5)
    assert absent is None, (
        "an unbarred key came back carrying a bar, so the row would name a bar "
        f"the run never faced: {absent}"
    )
    assert unbarred is Judgement.UNBARRED


@pytest.mark.parametrize(("low", "high"), [(0.1, None), (None, 0.2)])
def test_a_window_missing_a_bound_is_refused_at_load(
    tmp_path: Path, low: float | None, high: float | None
) -> None:
    """A window with no `high` silently degrades to a floor.

    the failure direction the window existed to catch stops being caught, and nothing
    says so.

    Both ends, because a check written against the missing `high` alone -- the
    one the plan names -- lets the mirror image through, and a window with no
    `low` degrades to a ceiling just as quietly.
    """
    with pytest.raises(ValueError, match="window"):
        _bars(tmp_path, _bar(kind="window", low=low, high=high))


def test_a_floor_with_no_floor_is_refused_at_load(tmp_path: Path) -> None:
    """The window guard's twin, and the one nobody writes.

    A `floor` with no `low` is not a bar that gates loosely, it is a bar that
    gates on nothing at all: every value passes, the run reports `pass`, and
    the file still looks like it holds a regression floor. Refusing it at load
    is the same argument the window guard makes one kind over -- a bar that
    cannot do its job must say so rather than answer green.
    """
    with pytest.raises(ValueError, match="floor"):
        _bars(tmp_path, _bar(kind="floor", high=10.0))


@pytest.mark.parametrize(("low", "high"), [(0.278, None), (None, 0.9)])
def test_a_pending_bar_carrying_a_number_is_refused_at_load(
    tmp_path: Path, low: float | None, high: float | None
) -> None:
    """`pending` means *no prior measurement exists*.

    so a pending bar with a number beside it is the exact failure the design names -- a
    bar reverse-engineered from the number it judges is not a bar.

    It is refused rather than tolerated, and that also settles the precedence
    question by making it unreachable: with a pending bar that carries bounds
    impossible to load, there is no state in which "does `kind` win or do the
    bounds?" has an answer to get wrong. The reachable version of this is Task
    14 writing 0.278 into the band bar and forgetting to change its `kind`,
    which under either precedence is a bar that quietly never gates.
    """
    with pytest.raises(ValueError, match="pending"):
        _bars(tmp_path, _bar(kind="pending", low=low, high=high))


def test_an_unknown_kind_is_refused_rather_than_judged_by_its_bounds(tmp_path: Path) -> None:
    """Three kinds, and a fourth spelling is a typo rather than a new idea.

    A loader that fell through to the bound comparisons would judge
    `kind = "flooor"` as a window and report a verdict against a bar nobody
    registered; one that skipped it would report UNBARRED and read as a metric
    nobody had barred. Both are worse than the red.
    """
    with pytest.raises(ValueError, match="kind"):
        _bars(tmp_path, _bar(kind="flooor", low=0.1, high=0.2))


@pytest.mark.parametrize(("kind", "low", "high"), [("window", 0.2, 0.1), ("floor", 10.0, 0.0)])
def test_a_bar_whose_bounds_are_transposed_is_refused_at_load(
    tmp_path: Path, kind: str, low: float, high: float
) -> None:
    """A floor above its own ceiling refuses every value there is.

    The failure is loud rather than silent, which is what makes this the
    smallest of the refusals -- but a run that reports `fail` on every stratum
    says nothing about which of the two numbers is the wrong way round, and
    leaves an operator reading the surface for a defect that is in this file.
    Naming it at load is one line and turns a whole red run into a sentence.

    Both kinds, because the `floor` arm is the shipped shape: the latency bar
    is a floor carrying a `high`, so a guard written for `window` alone would
    miss the only bar in the file that has two bounds and is not a window.
    """
    with pytest.raises(ValueError, match="wrong way round"):
        _bars(tmp_path, _bar(kind=kind, low=low, high=high))


def test_two_bars_sharing_all_four_keys_are_refused_rather_than_one_shadowing_the_other(
    tmp_path: Path,
) -> None:
    """`find` returns the first match.

    so a second bar on the same four keys is dead weight that still reads as a
    registered bar.
    """
    shadowed = _bar(kind="pending", metric="recall_at_5") + _bar(
        kind="floor", metric="recall_at_5", low=0.7014
    )
    with pytest.raises(ValueError, match="two bars answer to one key"):
        _bars(tmp_path, shadowed)

    distinct = _bar(kind="pending", metric="recall_at_5") + _bar(
        kind="floor", metric="recall_at_5", stratum="band=2-4", low=0.7014
    )
    assert len(_bars(tmp_path, distinct).bars) == 2, (
        "the control: two bars differing only in `stratum` are two bars, so "
        "the refusal above is about the key they share and not about a file "
        "holding more than one entry"
    )


def test_a_bar_file_holding_no_bars_is_refused_rather_than_judging_nothing(
    tmp_path: Path,
) -> None:
    """A file with every table commented out is not a set of bars.

    It loads, it hashes, and it answers UNBARRED to every question -- so the
    ledger records a `sha256` of a pre-registration that registers nothing,
    which is the one artefact this module exists to make trustworthy. The
    reachable spelling is somebody disabling the gate for an afternoon.
    """
    with pytest.raises(ValueError, match="no bars"):
        _bars(tmp_path, "# every bar commented out\n")


def test_an_absent_bar_file_is_loud_rather_than_an_empty_bar_set(tmp_path: Path) -> None:
    """`bars.toml` is data this code reads, so the question is what happens when it is not there.

    A loader carrying a built-in default -- or answering an empty `BarSet` --
    would make a missing, moved or mistyped path read exactly like a file
    holding no failures. It raises instead, and the path it was handed is in
    the message because that is the one fact an operator needs.
    """
    with pytest.raises(FileNotFoundError):
        load_bars(tmp_path / "nothing-here.toml")


def test_a_malformed_bar_file_is_loud_rather_than_a_default(tmp_path: Path) -> None:
    """Same argument one step in: a file that is present and not parseable.

    A `try`/`except` around the parse returning defaults is the repair
    somebody reaches for when a bad file breaks CI, and it converts a broken
    gate into a passing one.
    """
    with pytest.raises(tomllib.TOMLDecodeError):
        _bars(tmp_path, "[[bar]\nsurface = broken\n")


def test_a_bar_missing_one_of_its_keys_is_loud_rather_than_unmatchable(tmp_path: Path) -> None:
    """A bar with no `metric` cannot be looked up by anything.

    The tempting spelling is `entry.get("metric", "")`, which builds a bar no
    query can ever match -- so the file holds five bars, the harness finds
    four, and the fifth metric reports UNBARRED forever with nothing to say
    why. Raising on the missing key is the difference between a bar that is
    absent and a bar that is invisible.

    The read that actually raises is the one `load_bars` makes to *name* the
    bar in its refusal messages, not the four in the `Bar(...)` construction
    -- measured, by planting each half separately (2026-08-19). Defaulting the
    construction alone is an equivalent mutant today and this case is right
    not to see it; defaulting both is the careful spelling and dies here.
    """
    body = _bar(kind="window", low=0.1, high=0.2).replace("metric = 'm'\n", "")
    assert "metric" not in body, f"the premise: the key really is gone: {body!r}"
    with pytest.raises(KeyError, match="metric"):
        _bars(tmp_path, body)
