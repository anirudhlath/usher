"""Pre-registered bars, and the hash that proves which ones a run faced.

`tomllib` rather than a config library: it is stdlib on 3.13, the file is
read once per run, and a bar file is the one artefact in this project that
must be trivially readable by a person deciding whether a number was moved
after the fact.

**Nothing here has a default bar, a default file or a default answer.** The
bars live in `docs/evals/bars.toml`, which is data this code reads, so every
way that file can fail to be usable -- absent, unparseable, holding no bars,
holding a bar that cannot gate -- raises rather than degrading. That is the
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
    judges, which is the one thing pre-registration exists to rule out -- so
    it travels with the bar into whatever reports it.
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

    def find(self, surface: str, tier: str, metric: str, stratum: str) -> Bar | None:
        """The bar registered for exactly this key, or `None`.

        **All four keys, matched together.** Three of the five shipped bars
        agree on surface, tier and metric and differ only in `stratum` -- the
        band ADR-0002 failed on, the typo class it measured at 0.0%, and the
        mean over everything -- so a lookup comparing three of the four would
        answer one stratum's bar to another stratum's question and quote its
        reasoning in the report.
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

    def judge(self, surface: str, tier: str, metric: str, stratum: str, value: float) -> Judgement:
        """Judge one value.

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
        """
        bar = self.find(surface, tier, metric, stratum)
        if bar is None:
            return Judgement.UNBARRED
        if bar.kind == "pending":
            return Judgement.PENDING
        if bar.low is not None and value < bar.low:
            return Judgement.FAIL
        if bar.high is not None and value > bar.high:
            return Judgement.FAIL
        return Judgement.PASS


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
    * a file holding no bars at all hashes and answers `UNBARRED` to
      everything, so the ledger would record the sha256 of a pre-registration
      that registered nothing.

    A missing key raises `KeyError` rather than being defaulted:
    `entry.get("metric", "")` builds a bar no query can match, so the file
    would hold five bars while the harness found four and the fifth metric
    reported `UNBARRED` forever with nothing to say why.

    **`named` is what raises, and it is built for every entry rather than only
    for the ones being refused.** Measured 2026-08-19: with `named` eager,
    defaulting all four keys in the `Bar(...)` construction below survives all
    27 cases -- nothing reaches the construction without having already read
    them -- and defaulting them in `named` too fails
    `test_a_bar_missing_one_of_its_keys_is_loud_rather_than_unmatchable`. So
    the eager line is the load-bearing one and the reads below are its
    redundancy, not the reverse; moving `named` inside the guards to save a
    format on the happy path is what would open this.
    """
    raw = path.read_bytes()
    document = tomllib.loads(raw.decode())
    bars: list[Bar] = []
    for entry in document.get("bar", []):
        kind = entry["kind"]
        low = entry.get("low")
        high = entry.get("high")
        named = f"{entry['surface']}/{entry['tier']}/{entry['metric']}/{entry['stratum']}"
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
