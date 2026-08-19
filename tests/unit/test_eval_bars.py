"""The bar file, its hash, and the four verdicts a bar can return.

**Every case here was written against a named wrong implementation**, because
this module's whole job is to stop a number being believed and the suites that
went before it in this package each shipped cases that passed against a broken
one. The wrong implementations, and the case that kills each:

* **a `pending` bar that reads as passing** -- the most damaging one, since
  three of the five shipped bars are deliberately pending until Task 14 fills
  them in. `test_a_pending_bar_never_gates` kills the obvious spelling (the
  `kind == "pending"` arm deleted, so the absent bounds pass everything). Its
  *careful* spelling is an enum alias -- `PENDING = "pass"` makes
  `Judgement.PENDING` and `Judgement.PASS` the same object, so that case is
  green while every report, ledger row and exit code says `pass` -- and only
  `test_the_four_judgements_are_four_different_strings_and_pending_is_not_spelled_pass`
  can see it;
* **a window checked on one side**, so a value above the ceiling reads as
  fine, dies on `test_a_window_fails_in_both_directions`;
* **a bound compared with the wrong operator** -- `>` where `>=` is meant, or
  the comparison inverted -- dies on `test_a_floor_fails_only_below` for the
  inversion and on the two boundary cases for the operator, which judge the
  bound value *itself* and are the only cases that can: every other case here
  sits comfortably inside or outside;
* **a missing bar treated as a pass** dies on
  `test_an_unbarred_metric_is_unbarred_rather_than_passing`, and a `find` that
  matches on three of the four keys -- so a stratum's bar answers another
  stratum's question -- dies on `test_a_bar_is_found_by_all_four_of_its_keys`
  and on `test_the_three_pending_suggest_bars_are_three_bars_and_not_one_found_three_times`;
* **a `sha256` over the parsed TOML** rather than the raw bytes, which leaves
  an edited comment or a reordered table invisible and makes the
  pre-registration claim unfalsifiable, dies on
  `test_the_hash_is_over_the_bytes_so_a_comment_edited_after_the_fact_moves_it`
  and **not** on `test_the_hash_changes_when_the_file_changes`, which a parsed
  digest passes -- the two are one page apart on purpose;
* **judgement precedence** wrong when more than one condition applies is
  refused one layer earlier: a bar that names a number *and* declines to gate
  on it cannot be loaded at all
  (`test_a_pending_bar_carrying_a_number_is_refused_at_load`), so neither
  precedence is reachable;
* **a bar file the harness cannot use, read as one it can** -- absent,
  malformed, holding no bars, holding a bar with no floor or an unknown kind --
  dies on the five refusal cases at the end. `docs/evals/bars.toml` is data
  this code reads, and a default that shadows a broken file would make a
  broken file read as working.

**The numbers in `test_the_registered_numbers_are_the_ones_that_were_registered`
are literals on purpose, and a later task will have to edit them.** That is
the point rather than a maintenance cost: filling in a pending bar is exactly
the edit that has to be visible, and a case whose expectation is read out of
the file it is checking would pass against any file at all.
"""

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
    """The file `usher eval` will actually read, read through the loader that
    will actually read it.

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
    assert bars.find("suggest", "prefix", "recall_at_5", "all") is not None


def test_the_registered_numbers_are_the_ones_that_were_registered() -> None:
    """The five bars E1 pre-registered, pinned as **literals**.

    This is the case a bar edited after seeing a number has to get past, and
    the only reason it can do that is that the expectations here are written
    down rather than read out of the file under test. Both halves are needed
    and neither is the other: the `sha256` makes an edit *visible in the
    record*, and this makes it *fail the suite*.

    Task 14 fills the three pending bars in from the first reproducing
    `--full` run, and will have to edit this case to do it. That edit is the
    deliberate, reviewed act the design asks for -- it is not the same event
    as a number being nudged until CI goes green, which is what would happen
    if this case derived its expectations from `bars.toml`.
    """
    bars = load_bars(_SHIPPED)

    registered = {
        (one.surface, one.tier, one.metric, one.stratum): (one.kind, one.low, one.high)
        for one in bars.bars
    }
    assert registered == {
        ("suggest", "prefix", "recall_at_5", "all"): ("window", 0.016, 0.022),
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
    """The shipped file holds three bars that agree on surface, tier and
    metric and differ only in `stratum`, which is what makes it the file that
    can tell a four-key lookup from a three-key one.

    A `find` that ignored `stratum` would answer the `all` bar for every one
    of the three -- all three are `pending`, so `judge` would agree and every
    verdict in this module would still be right, while the *bar* a report
    names and the reasoning it quotes would belong to another stratum.
    """
    bars = load_bars(_SHIPPED)
    strata = ("all", "band=2-4", "typo_class=transposition")

    found = [bars.find("suggest", "fuzzy", "recall_at_5", one) for one in strata]
    assert [one.stratum for one in found if one is not None] == list(strata)
    assert len({one.source for one in found if one is not None}) == 3, (
        "the three strata came back carrying one reason between them, so the "
        "lookup is answering the same bar three times"
    )


def test_the_hash_changes_when_the_file_changes(tmp_path: Path) -> None:
    """The hash is the whole mechanism. If it did not move with the content,
    a bar edited after seeing a number would be invisible."""
    one = tmp_path / "one.toml"
    one.write_text(_bar(kind="floor", low=0.5))
    first = load_bars(one).sha256
    one.write_text(_bar(kind="floor", low=0.6))
    assert load_bars(one).sha256 != first


def test_the_hash_is_over_the_bytes_so_a_comment_edited_after_the_fact_moves_it(
    tmp_path: Path,
) -> None:
    """A digest over the *parsed* document passes the case above and is still
    the wrong digest.

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
    """A window exists because *movement either way* means the thing measured
    is not the thing that was measured before, so a window checked on one side
    is a floor wearing a window's name."""
    bars = _bars(tmp_path, _bar(kind="window", low=0.016, high=0.022))
    assert bars.judge("s", "t", "m", "all", 0.019) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 0.004) is Judgement.FAIL
    assert bars.judge("s", "t", "m", "all", 0.400) is Judgement.FAIL


def test_a_window_admits_the_two_bounds_it_names(tmp_path: Path) -> None:
    """`[low, high]` is closed at both ends, and only a value *at* a bound can
    say so.

    Every other case in this module sits comfortably inside or outside, so
    `<` for `<=` on either end -- the likeliest single-character defect in the
    module -- is invisible to all of them. The bounds are dyadic (0.5, 0.75)
    so the comparison is exact rather than nearly exact.
    """
    bars = _bars(tmp_path, _bar(kind="window", low=0.5, high=0.75))
    assert bars.judge("s", "t", "m", "all", 0.5) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 0.75) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 0.25) is Judgement.FAIL
    assert bars.judge("s", "t", "m", "all", 1.0) is Judgement.FAIL


def test_a_floor_fails_only_below(tmp_path: Path) -> None:
    """The comparison inverted -- a floor that refuses everything above it --
    is the other single-character defect, and this is what sees it."""
    bars = _bars(tmp_path, _bar(kind="floor", low=0.5))
    assert bars.judge("s", "t", "m", "all", 0.9) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 0.4) is Judgement.FAIL


def test_a_floor_with_a_ceiling_fails_above_it(tmp_path: Path) -> None:
    """The shipped latency bar is spelled this way -- a floor of 0.0 with a
    ceiling of 10.0, because the failure direction is slow -- so a `high` the
    floor branch ignores would leave the one bar in the file that gates on a
    latency gating on nothing."""
    bars = _bars(tmp_path, _bar(kind="floor", low=0.0, high=10.0))
    assert bars.judge("s", "t", "m", "all", 4.0) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 40.0) is Judgement.FAIL


def test_a_floor_admits_the_floor_itself_and_the_ceiling_itself(tmp_path: Path) -> None:
    """The boundary case for the other kind, because a wrong implementation is
    free to branch on `kind` and spell one comparison strictly.

    `>= low` and `<= high` are what the file's own header claims, and a value
    landing exactly on a registered bound is not hypothetical: the latency
    bar's floor is 0.0 and a suggest tier that answered nothing would report
    exactly that.
    """
    bars = _bars(tmp_path, _bar(kind="floor", low=0.0, high=10.0))
    assert bars.judge("s", "t", "m", "all", 0.0) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", 10.0) is Judgement.PASS
    assert bars.judge("s", "t", "m", "all", -0.5) is Judgement.FAIL
    assert bars.judge("s", "t", "m", "all", 10.5) is Judgement.FAIL


def test_a_pending_bar_never_gates(tmp_path: Path) -> None:
    """No number is wrong against a bar that does not exist yet. Reporting
    PENDING rather than PASS keeps a run from claiming a bar it never faced."""
    bars = _bars(tmp_path, _bar(kind="pending"))
    assert bars.judge("s", "t", "m", "all", 0.0) is Judgement.PENDING
    assert bars.judge("s", "t", "m", "all", 1.0) is Judgement.PENDING


def test_the_four_judgements_are_four_different_strings_and_pending_is_not_spelled_pass() -> None:
    """The verdicts are values on the wire -- a report line, a ledger row, an
    exit code -- so what they *are* matters as much as which one is returned.

    This is the only case that can see the most damaging spelling of "a
    pending bar reads as passing": `PENDING = "pass"` does not create a fourth
    member, it creates an **alias**, so `Judgement.PENDING is Judgement.PASS`
    and every `is Judgement.PENDING` assertion in this module stays green
    while a run with no bar at all reports `pass`. Same for `UNBARRED`, whose
    whole reason for existing is that silence must not read as success.

    The four literals are written down rather than read off the enum for the
    reason every constant in this module is: an expectation derived from the
    thing under test pins that it is in force and cannot pin its value.

    Both assertions are over the **whole** enum, because the direct spelling
    cannot be written: `Judgement.PENDING is not Judgement.PASS` is refused by
    `mypy` as a *non-overlapping identity check* (measured 2026-08-19), which
    is the type checker being right about today's enum and silent about the
    one this case exists to catch. An alias does not show up as two members
    that are equal -- it shows up as a member that has vanished from the
    iteration and from the set.
    """
    assert [one.value for one in Judgement] == ["pass", "fail", "pending", "unbarred"]
    assert len({Judgement.PASS, Judgement.FAIL, Judgement.PENDING, Judgement.UNBARRED}) == 4, (
        "two members share a value, so one is an alias of the other and the "
        "distinction the four exist to draw has silently collapsed: "
        f"{[(one.name, one.value) for one in Judgement]}"
    )


def test_an_unbarred_metric_is_unbarred_rather_than_passing(tmp_path: Path) -> None:
    """A metric nobody wrote a bar for must not read as green. That is how a
    surface gets added and silently gates on nothing."""
    bars = _bars(tmp_path, _bar(kind="pending"))
    assert bars.judge("s", "t", "other", "all", 0.9) is Judgement.UNBARRED


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
    """A lookup that compared three of the four keys would answer this bar for
    a question it was not registered against.

    That is not a hypothetical shape in this file: `bars.toml` holds three
    bars agreeing on surface, tier and metric and differing only in stratum,
    and E2 adds surfaces that will share metric names with this one. One arm
    per key, because a case varying only the metric -- which is the obvious
    one to write -- is passed by three of the four wrong implementations.
    """
    bars = _bars(tmp_path, _bar(kind="window", low=0.1, high=0.2))
    assert bars.judge("s", "t", "m", "all", 0.15) is Judgement.PASS, (
        "the premise: the exact key is barred, so an UNBARRED below is about "
        "the key that was changed and not about a bar that was never found"
    )
    assert bars.judge(surface, tier, metric, stratum, 0.15) is Judgement.UNBARRED


@pytest.mark.parametrize(("low", "high"), [(0.1, None), (None, 0.2)])
def test_a_window_missing_a_bound_is_refused_at_load(
    tmp_path: Path, low: float | None, high: float | None
) -> None:
    """A window with no `high` silently degrades to a floor -- the failure
    direction the window existed to catch stops being caught, and nothing
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
    """`pending` means *no prior measurement exists*, so a pending bar with a
    number beside it is the exact failure the design names -- a bar
    reverse-engineered from the number it judges is not a bar.

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
    """`bars.toml` is data this code reads, so the question is what happens
    when it is not there.

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
