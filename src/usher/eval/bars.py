"""Pre-registered bars, and the hash that proves which ones a run faced."""

import hashlib
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# : The three kinds a bar may declare, and a fourth spelling is a typo rather : than a
# new idea -- `load_bars` refuses one instead of falling through to : the bound
# comparisons, where an unregistered kind would be judged as a : window and reported as
# a verdict.
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
    """One pre-registered bar: what it is about, how it gates, and why."""

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
        """The bar this value was judged against, and the judgement."""
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
    """Read and hash the bar file."""
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
