"""The genome's 1.81% pair rate never travels without the population it was measured over."""

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# The literals, not a regex over them. `1.81` is how every one of these
# paragraphs spells the rate and `5,020` is how every one of them spells the
# seed count -- a looser pattern would match `1.814` or `5020` and would be
# satisfied by prose that never names either.
_RATE = "1.81"
_POPULATION = "5,020"

# The floor exists because deleting the glob is the cheapest way to make this case
# green.
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
