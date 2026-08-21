"""Pre-registered bars, and the hash that proves which ones a run faced.

`tomllib` rather than a config library: it is stdlib on 3.13, the file is
read once per run, and a bar file is the one artefact in this project that
must be trivially readable by a person deciding whether a number was moved
after the fact.

**Nothing here has a default bar, a default file or a default answer.** The
bars live in `docs/evals/bars.toml`, which is data this code reads, so every
way that file can fail to be usable -- absent, unparseable, holding no bars,
holding a bar that cannot gate, holding two bars that answer to one key --
raises rather than degrading. That is the
whole design: a harness that answers `pass` for a bar it could not read is
worse than one that crashes, because the crash gets fixed and the `pass` gets
believed. ADR-0002's typo gate is the reason the distinction is not
theoretical -- it failed both halves of a bar written down before the numbers
were known, and that failure is only worth anything because the bar predated
the number.
"""

import hashlib
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: The three kinds a bar may declare, and a fourth spelling is a typo rather
#: than a new idea -- `load_bars` refuses one instead of falling through to
#: the bound comparisons, where an unregistered kind would be judged as a
#: window and reported as a verdict.
#:
#: **A fourth *idea* is a different question, and it is deferred rather than
#: absent.** There is no ceiling-only kind, so a bar whose only real gate is an
#: upper bound is spelled `floor` with `low = 0.0` -- which is what the shipped
#: latency bar is. `judge`'s docstring says what that costs and why adding the
#: kind is not a change to make in passing.
_KINDS = frozenset({"window", "floor", "pending"})


class Judgement(StrEnum):
    """What a bar says about one value.

    Four members rather than a bool, because three of them are not failures
    and collapsing them loses the distinction that keeps a gate trusted.

    **The four values are four different strings, and that is a property
    rather than a coincidence.** These reach a report line, a ledger row and
    an exit code, so a member sharing another's value is not a cosmetic
    duplicate -- an enum makes it an *alias*, and `PENDING = "pass"` would
    leave `judge(...) is Judgement.PENDING` true everywhere while every
    published artefact said the run passed a bar nobody has set yet.
    """

    # S105: a verdict, not a credential -- bandit's heuristic matches the
    # member *name*. Both sides of this line are load-bearing: the name is
    # what every call site reads and the string is what a report, a ledger row
    # and `exit_code_for` publish, so neither can be spelled around the rule.
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    PENDING = "pending"
    UNBARRED = "unbarred"


@dataclass(frozen=True, slots=True)
class Bar:
    """One pre-registered bar: what it is about, how it gates, and why.

    `source` is not decoration. A threshold with no argument behind it is
    indistinguishable from a threshold reverse-engineered from the number it
    judges, which is the one thing pre-registration exists to rule out.

    **What carries it to a later reader is the file's sha256, not the ledger
    row -- and this docstring claimed the opposite until 2026-08-19, saying it
    "travels with the bar into whatever reports it".** It does not, and nothing
    is scheduled to make it: the ledger row this harness is being built to
    write carries `bar_kind`, `bar_low`, `bar_high` and the bar file's digest,
    with no column for the argument, and the report line prints tier, metric,
    value, n and the judgement. So the row an auditor ends up reading carries
    the threshold and not the argument, which is exactly the thing the
    paragraph above condemns. What makes the argument
    recoverable anyway is that the digest is over the **raw bytes**, and the
    prose lives in those bytes: hash each revision of `docs/evals/bars.toml`
    that git holds, and the one matching a run's recorded digest is the file
    that run faced, `source` included. That costs a `git rev-list` and a loop
    rather than a `SELECT`; carrying a `bar_source` column into the ledger
    would make it a `SELECT`, and that is a decision for whoever builds the
    ledger and the report rather than a promise this docstring gets to make on
    their behalf.
    """

    surface: str
    tier: str
    metric: str
    stratum: str
    kind: str
    low: float | None
    high: float | None
    source: str


@dataclass(frozen=True, slots=True)
class BarSet:
    """Every bar, and the sha256 of the file they came from."""

    bars: tuple[Bar, ...]
    sha256: str
    path: Path

    def find(self, *, surface: str, tier: str, metric: str, stratum: str) -> Bar | None:
        """The bar registered for exactly this key, or `None`.

        **All four keys, matched together.** Three of the five shipped bars
        agree on surface, tier and metric and differ only in `stratum` -- the
        band ADR-0002 failed on, the typo class it measured at 0.0%, and the
        mean over everything -- so a lookup comparing three of the four would
        answer one stratum's bar to another stratum's question and quote its
        reasoning in the report.

        **All four keyword-only, and that is a guard rather than a style.**
        Four adjacent `str` parameters, two of which -- `metric` and `stratum`
        -- read most alike and are the pair E2's new surfaces will be
        inventing. Transposed positionally the lookup finds nothing, `judge`
        answers `UNBARRED`, and `UNBARRED` fails at no level: not the
        judgement, not the verdict, not the exit code. `mypy` cannot see it
        either, because all four are `str`. Keyword-only makes the positional
        spelling unspellable, which is the only check available for a defect
        whose whole symptom is silence.
        """
        for bar in self.bars:
            if (bar.surface, bar.tier, bar.metric, bar.stratum) == (
                surface,
                tier,
                metric,
                stratum,
            ):
                return bar
        return None

    def judge_with_bar(
        self, *, surface: str, tier: str, metric: str, stratum: str, value: float
    ) -> tuple[Bar | None, Judgement]:
        """The bar this value was judged against, and the judgement.

        **One lookup, so the two answers cannot be about different bars.** A
        ledger row needs both -- the verdict, and the `kind`/`low`/`high` it
        was reached against -- and the obvious way to fill one in is `find`
        then `judge`, which scans twice and re-derives the key. That is correct
        today and is one edit away from not being: a later change to either
        scan leaves a row quoting one bar's thresholds beside another bar's
        verdict, with nothing to notice. Writing the lookup once makes the
        agreement structural rather than conventional, which is the same move
        `_settle` made for *record and commit* in the curation service.

        **An absent bar is `UNBARRED`, not `PASS`.** A metric nobody wrote a
        bar for reading green is how a surface gets added and silently gates
        on nothing -- the "a run that did not run is not a pass" trap, one
        level down.

        **`pending` is read off the kind and answered before any comparison**,
        rather than inferred from having no bounds. The two spellings agree on
        every file `load_bars` will accept, because a pending bar carrying a
        number is refused there; reading the kind is what keeps them agreeing
        if that refusal is ever loosened.

        Both bounds are closed -- `>= low`, `<= high` -- which is what the bar
        file's own header claims of a floor, and a value landing exactly on a
        registered bound is ordinary rather than exotic: the shipped latency
        floor is 0.0, and a tier that answered nothing scores exactly that.

        **That latency bar's `low = 0.0` cannot fail, and it is documentation
        rather than a gate.** A p95 over a monotonic-clock delta has no
        negative value to produce, so the half of that bar which actually gates
        is its `high = 10.0`; the floor states, for a reader of the file alone,
        which direction the quantity runs. It is spelled that way because this
        vocabulary has no ceiling-only kind, and both available repairs are
        larger than they look -- a fourth member of `_KINDS`, or a change to
        the shape of a bar that is already registered -- so both are deferred
        deliberately rather than made in a review round. Note what is *not*
        unfalsifiable: the comparison itself is exercised, by
        `test_a_floor_admits_the_floor_itself_and_the_ceiling_itself` judging
        -0.5. What cannot fail is that one bar's floor, not this code.
        """
        bar = self.find(surface=surface, tier=tier, metric=metric, stratum=stratum)
        if bar is None:
            return bar, Judgement.UNBARRED
        if bar.kind == "pending":
            return bar, Judgement.PENDING
        if bar.low is not None and value < bar.low:
            return bar, Judgement.FAIL
        if bar.high is not None and value > bar.high:
            return bar, Judgement.FAIL
        return bar, Judgement.PASS

    def judge(
        self, *, surface: str, tier: str, metric: str, stratum: str, value: float
    ) -> Judgement:
        """The verdict alone, for a caller that does not need the bar.

        Delegates rather than repeating the comparisons, so there is exactly
        one implementation of the precedence and one lookup behind both
        spellings -- see `judge_with_bar`, which is where the argument lives.
        """
        _, judgement = self.judge_with_bar(
            surface=surface, tier=tier, metric=metric, stratum=stratum, value=value
        )
        return judgement


def load_bars(path: Path) -> BarSet:
    """Read and hash the bar file.

    The hash is over the **raw bytes**, not over the parsed structure: a
    comment edited to justify a number after the fact is exactly the change
    this exists to make visible, and a structural hash would miss it. The
    three pending entries in `docs/evals/bars.toml` are four fifths prose, so
    that is most of what the file says.

    **Every refusal below is a bar that would otherwise gate on nothing while
    still looking like a bar**, and each is named separately so the message
    tells an operator which one:

    * a `window` missing either bound degrades to a floor or a ceiling, and
      the direction it was written to catch stops being caught;
    * a `floor` with no `low` admits every value there is;
    * a `pending` bar carrying a number is the failure the design names in so
      many words -- *a bar reverse-engineered from the number it judges is not
      a bar* -- and refusing it is also what makes `judge`'s precedence
      question unreachable rather than merely decided;
    * an unknown `kind` is a typo, and falling through would judge it as a
      window;
    * a `low` above its `high` refuses every value there is, and the run is
      then red for a reason that is in the file rather than in the thing
      measured -- loud, but only the message makes an operator look at the
      bounds rather than at the surface;
    * **two bars answering to one key** are one bar and one piece of dead
      weight that still reads as registered, because `find` returns the first
      match. The reachable spelling is filling a pending bar in by copying its
      neighbour, changing `kind`, `low` and `source`, and forgetting the
      `stratum` line: the pending copy answers first, the filled floor gates on
      nothing, that stratum reports `pending` forever, and the run exits 0;
    * a file holding no bars at all hashes and answers `UNBARRED` to
      everything, so the ledger would record the sha256 of a pre-registration
      that registered nothing.

    **Every refusal here is a `ValueError` and deliberately not an
    `EvalRefused`, and the distinction is load-bearing.** `EvalRefused` is what
    the runner catches into `skipped` and `baseline-invalid`, **both of which
    exit 0** -- so a bar file the harness could not read would be reported as
    "this measurement did not happen, and that is fine". The next
    contributor's instinct is to unify this package's error vocabulary onto its
    own exception type; that refactor turns every refusal above into a green
    run, silently, and is the one change to this function that needs an
    argument rather than a tidy-up.

    A missing key raises `KeyError` rather than being defaulted:
    `entry.get("metric", "")` builds a bar no query can match, so the file
    would hold five bars while the harness found four and the fifth metric
    reported `UNBARRED` forever with nothing to say why.

    **`key` is what raises, and it is built for every entry rather than only
    for the ones being refused.** Re-measured 2026-08-19 against 32 cases:
    with `key` eager, defaulting all four keys in the `Bar(...)` construction
    below survives every one of them -- nothing reaches the construction
    without having already read them -- and defaulting them in `key` too fails
    `test_a_bar_missing_one_of_its_keys_is_loud_rather_than_unmatchable`. So
    the eager line is the load-bearing one and the reads below are its
    redundancy, not the reverse; moving `key` inside the guards to save a tuple
    on the happy path is what would open this.

    **`named` is derived from `key` rather than reading `entry` a second
    time**, so all four keys are read eagerly in exactly one place. Two eager
    reads are each a redundant copy of the other's guarantee, which is how one
    of them later gets tidied away on the grounds that the other covers it.
    The duplicate check compares the **tuple** and not that rendered string,
    because `/` is legal inside a stratum name and `("a/b", "c", ...)` renders
    identically to `("a", "b/c", ...)` -- a refusal keyed on the display form
    would refuse two bars that are not the same bar.
    """
    raw = path.read_bytes()
    document = tomllib.loads(raw.decode())
    bars: list[Bar] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in document.get("bar", []):
        kind = entry["kind"]
        low = entry.get("low")
        high = entry.get("high")
        key = (entry["surface"], entry["tier"], entry["metric"], entry["stratum"])
        named = "/".join(key)
        if kind not in _KINDS:
            raise ValueError(
                f"unknown bar kind {kind!r}: {named} in {path} declares a kind that is "
                f"none of {sorted(_KINDS)}. A kind nothing recognises is a typo, and "
                "judging it as a window would report a verdict against a bar nobody "
                "registered."
            )
        if kind == "window" and (low is None or high is None):
            raise ValueError(
                f"a window bar needs both bounds: {named} has low={low} high={high}. "
                "A window missing one bound is a floor wearing a window's name, and "
                "the direction it was written to catch stops being caught."
            )
        if kind == "floor" and low is None:
            raise ValueError(
                f"a floor bar needs a floor: {named} has low={low}. A floor with no "
                "low admits every value there is, so the run reports a pass against "
                "a bar that gated on nothing."
            )
        if kind == "pending" and (low is not None or high is not None):
            raise ValueError(
                f"a pending bar carries no number: {named} is pending and has "
                f"low={low} high={high}. `pending` states that no prior measurement "
                "exists, so a number beside it is the failure this file is written "
                "against -- a bar reverse-engineered from the number it judges is "
                "not a bar. Set the kind to `window` or `floor` to gate on it."
            )
        if low is not None and high is not None and low > high:
            raise ValueError(
                f"a bar's bounds are the wrong way round: {named} has low={low} "
                f"high={high}. A floor above its own ceiling refuses every value "
                "there is, so the run goes red for a reason that is in this file "
                "rather than in the thing being measured. Swap them."
            )
        if key in seen:
            raise ValueError(
                f"two bars answer to one key: {named} is registered twice in "
                f"{path}. `find` returns the first match, so the second gates on "
                "nothing while still reading as a registered bar. The reachable "
                "spelling is filling a pending bar in by copying its neighbour "
                "and forgetting to change the `stratum` -- which leaves the "
                "pending copy answering first, the filled bar unreachable, and "
                "the run exiting 0."
            )
        seen.add(key)
        bars.append(
            Bar(
                surface=entry["surface"],
                tier=entry["tier"],
                metric=entry["metric"],
                stratum=entry["stratum"],
                kind=kind,
                low=low,
                high=high,
                source=entry.get("source", ""),
            )
        )
    if not bars:
        raise ValueError(
            f"{path} holds no bars. A bar file with every table absent or commented "
            "out still parses and still hashes, and answers `unbarred` to every "
            "question -- so a ledger row would record the sha256 of a "
            "pre-registration that registered nothing."
        )
    return BarSet(bars=tuple(bars), sha256=hashlib.sha256(raw).hexdigest(), path=path)
