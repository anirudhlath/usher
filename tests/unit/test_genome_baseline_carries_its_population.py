"""The genome's 1.81% pair rate never travels without the population it was measured over.

**The defect this exists to stop is a comparison, not a typo.** M7 measured a
candidate-pair rate of **1.81%** (9,069 of 502,000 pairs) and shipped the genome
term at weight 0.25 on the strength of it being a *conservative floor*. M9
re-measures the same counter over a different population, and the moment the two
numbers sit next to each other a reader will subtract them. They are not
subtractable: 502,000 / `_CANDIDATE_POOL` (100) is exactly **5,020 seeds**, and
those 5,020 were one household's owned titles on a scratch catalog with no TMDb
key -- name-shaped documents selecting a name-shaped pool -- not the tier M9
enriches. A paragraph that quotes the rate without the seed count reads as a
baseline; the same paragraph with `5,020` in it reads as what it is.

So the rule is mechanical and this case enforces it: **every Markdown block that
carries the literal `1.81` also carries the literal `5,020`.** A "block" is a run
of consecutive non-blank lines, which makes a table one block -- the evidence
cell in `progress.md`'s M7 guess table is a table row, and a rule scoped to a
line rather than a block could not reach it.

**The corpus is `docs/prd/**/*.md` plus `docs/plans/progress.md`, and nothing
else.** `docs/specs/` carries two hits and is deliberately outside the scan:
`.claude/rules/prd-maintenance.md` is explicit that a spec is a point-in-time
record and that when the two disagree the PRD is authoritative -- *"Do not edit
an old spec to match -- specs are historical records of what was planned."* A
guard that globbed `docs/` would therefore be a guard that can only be satisfied
by breaking that rule. It is the same rescoping that file already applied to the
PRD link check, where it records that the unscoped version *"never once printed
`OK`"* and that **the exclusion is a correction, not a convenience**.
`.claude/rules/rows-and-genome.md` is outside the scan for a different reason --
it is not `docs/` -- and is held to the same standard by M9 Task S1's acceptance
criteria instead.

**Two control assertions ride in the same case**, for the reason
`test_no_third_party_data.py`'s fifth check states outright: *a guard that globs
nothing passes exactly like a guard that passes*. The hit-count floor and the
named-file assertion are what stop a future edit to `_corpus()` from turning this
green by scanning an empty set.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# The literals, not a regex over them. `1.81` is how every one of these
# paragraphs spells the rate and `5,020` is how every one of them spells the
# seed count -- a looser pattern would match `1.814` or `5020` and would be
# satisfied by prose that never names either.
_RATE = "1.81"
_POPULATION = "5,020"

# The floor exists because deleting the glob is the cheapest way to make this
# case green. Measured 2026-08-11: **eight** blocks over six files carried the
# rate before S1's own `progress.md` entry, **fourteen** after -- so eight is
# the floor rather than the count, and it is the number to keep if the entry is
# ever trimmed. Six is the figure that really matters and cannot be asserted
# directly here (a file may legitimately stop quoting the rate); what a broken
# glob produces is one or zero, which is what this catches.
_MINIMUM_HITS = 8

_DECISIONS = _REPO / "docs" / "prd" / "decisions"
_ADR_0024 = _DECISIONS / "0024-the-genome-is-one-dense-vector-per-title.md"
_PROGRESS = _REPO / "docs" / "plans" / "progress.md"


def _corpus() -> list[Path]:
    """Every file whose quotations of the rate have to carry the population."""
    return [*sorted(_REPO.glob("docs/prd/**/*.md")), _PROGRESS]


def _blocks(text: str) -> list[tuple[int, str]]:
    """`(first line number, block)` for each run of consecutive non-blank lines.

    A table is one block, which is the point: `progress.md`'s guess table states
    the rate in a cell and names its population in the prose above the table,
    and a line-scoped rule could not express "these belong together".
    """
    found: list[tuple[int, str]] = []
    start: int | None = None
    lines: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if start is None:
                start = number
            lines.append(line)
            continue
        if start is not None:
            found.append((start, "\n".join(lines)))
            start, lines = None, []
    if start is not None:
        found.append((start, "\n".join(lines)))
    return found


def test_every_quotation_of_the_pair_rate_names_the_population_it_was_measured_over() -> None:
    """1.81% and 5,020 seeds are one fact, so no block may carry only half of it."""
    corpus = _corpus()

    # Control 1: the scan reaches the file whose `## Uncertainty` paragraph is
    # the load-bearing one -- ADR-0024 is what a later session reads to find out
    # whether the term earns its weight.
    assert _ADR_0024 in corpus, (
        f"the corpus does not include {_ADR_0024.relative_to(_REPO)}; "
        "a scan that cannot see the ADR cannot hold it to this rule"
    )

    hits: list[str] = []
    unpopulated: list[str] = []
    for path in corpus:
        for number, block in _blocks(path.read_text(encoding="utf-8")):
            if _RATE not in block:
                continue
            where = f"{path.relative_to(_REPO)}:{number}"
            hits.append(where)
            if _POPULATION not in block:
                unpopulated.append(where)

    # Control 2: a glob that found nothing, or a `_blocks` that returned
    # nothing, produces an empty `unpopulated` and would otherwise pass.
    assert len(hits) >= _MINIMUM_HITS, (
        f"the scan found only {len(hits)} block(s) quoting {_RATE!r} across "
        f"{len(corpus)} file(s); expected at least {_MINIMUM_HITS}. "
        "The corpus is not being read -- fix the scan before trusting the verdict."
    )

    assert not unpopulated, (
        f"{len(unpopulated)} Markdown block(s) quote the genome's {_RATE}% "
        f"candidate-pair rate without naming the {_POPULATION}-seed population it "
        "was measured over, so each one reads as a baseline for a run it cannot "
        "be a baseline for:\n  " + "\n  ".join(unpopulated)
    )
