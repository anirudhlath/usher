"""The typo-tolerance gate's 2,993 cases, regenerated from the live catalog.

**Adopted verbatim from `scripts/measure_suggest_tiers.py`, which adopted it
from ADR-0002's gate.** Nothing here is re-chosen, because a re-chosen
constant makes E1's numbers incomparable with the 2026-08-03 run and with
ADR-0031 -- and the whole reason E1 measures suggest first is that those
numbers exist.

Movies only, `imdb_num_votes >= 500`, names not unique in the catalog excluded
at sampling time, five equal draws of 150 over `char_length(name)` bands, four
typo classes at a uniformly random position, `random.Random(20260803)`.

**The threshold is the gate's and the column is not, and that is a change of
population rather than a rename.** The gate wrote `vote_count` when only the
IMDb bulk import filled it; TMDb enrichment acquired the same column and by
2026-08-19 the predicate selected 8,523 unique-named movies against the gate's
48,549. ADR-0040 split the column by source, and this frame now reads the
IMDb one -- single-source, catalog-wide, unmovable by any TMDb crawl. Whether
that reproduces the gate's five pools is measured in `GATE_POOLS`, not assumed
here.
**2,993 rather than 3,000 because seven two-character names admit no
deletion.**

**And the source's own stated limitation, lifted verbatim rather than
paraphrased.** The reassuring half of that header -- the frame is verified,
the five pool sizes reproduce to the row -- travelled into this port on its
first commit and this half did not, in the one module whose whole thesis is
comparability with a recorded run. Without it a reader concludes E1's cases
*are* the gate's cases:

    What is *not* claimed: that the 750 sampled names are the same 750. The
    gate's draw order was not recorded, only its procedure and its pool
    sizes, so a different-but-equivalent draw is possible and every number
    here carries that caveat.

The generator is split in two on purpose. `build_typo_cases` is pure -- pools
in, cases out -- so it is unit-tested against a hand-built pool with no
database. `read_pools` and `read_frame` are the catalog reads.

**Owed by whichever task builds the runner: `GATE_CASES` as a runtime guard.**
`scripts/measure_suggest_tiers.py` prints `typo cases regenerated: N (gate:
2993)` beside every run and warns when the two disagree, which is how an
operator learns the catalog moved under them rather than reading a quietly
smaller number as if it were the gate's. The port carries the constant and
not the guard, because there is no runner here yet to print it from.
"""

import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.eval.errors import EvalRefused

# The gate's own constants. Named rather than inlined so a reader can see at a
# glance that nothing was re-chosen.
GATE_SEED = 20260803
GATE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("2-4", 2, 4),
    ("5-7", 5, 7),
    ("8-11", 8, 11),
    ("12-19", 12, 19),
    ("20+", 20, 10_000),
)
GATE_DRAW_PER_BAND = 150
# `MappingProxyType` rather than a bare dict, because `Mapping[str, int]` is
# documentation and this is the constant the whole comparability story rests
# on: `GATE_POOLS["2-4"] = 433` on a plain dict is one line, silent, and
# process-wide, and it moves the number `check_frame` refuses against.
GATE_POOLS: Mapping[str, int] = MappingProxyType(
    {
        "2-4": 432,
        "5-7": 2532,
        "8-11": 7178,
        "12-19": 20520,
        "20+": 17887,
    }
)
GATE_SHARED_LOWER_NAMES = 81_054
GATE_CASES = 2_993
TYPO_CLASSES: tuple[str, ...] = ("substitution", "deletion", "transposition", "doubled")

# One statement, two readers. `read_pools` selects from it and `read_frame`
# counts it, so the two can never disagree about *the predicate* -- spelled
# twice they would answer identically today and drift the first time either was
# edited.
#
# **What it does not buy is that the checked frame is the drawn frame.** They
# are two statements and under READ COMMITTED each takes its own snapshot, so a
# write landing between them makes the count a count of a population the draw
# never saw. Closing that is the caller's job and costs one repeatable-read
# transaction spanning both reads; nothing here can do it, because neither
# function opens the session it is handed.
# **The threshold is ADR-0002's and the column is not.** The gate was written
# against `titles.vote_count` when only the IMDb bulk import wrote it; TMDb
# enrichment later wrote the same column with a figure ~38x smaller (paired:
# median TMDb 15 against median IMDb 576 over the same 130,647 enriched rows),
# so by 2026-08-19 `vote_count >= 500` selected **8,523** unique-named movies
# where the gate recorded 48,549, and `check_frame` refused.
#
# `imdb_num_votes` is single-source, catalog-wide, and no TMDb crawl can move
# it -- so this restores ADR-0002's frame semantics rather than re-choosing
# them. Whether it restores the *pools* is an open question this module does
# not get to assume: `GATE_POOLS` below is re-measured against the restored
# catalog, and if it does not reproduce, the observed frame becomes canonical
# and the delta is recorded with its cause. A number is never edited to make a
# run green. ADR-0040.
_ELIGIBLE = """
    SELECT t.id, t.name FROM titles t
    WHERE t.kind = 'movie' AND t.imdb_num_votes >= 500
      AND char_length(t.name) BETWEEN :low AND :high
      AND NOT EXISTS (
          SELECT 1 FROM titles o
          WHERE lower(o.name) = lower(t.name) AND o.id <> t.id
      )
    ORDER BY t.id
"""


@dataclass(frozen=True, slots=True)
class TypoCase:
    """One mutated name, and the title it should still find."""

    title_id: uuid.UUID
    name: str
    band: str
    typo_class: str
    probe: str

    @property
    def query_id(self) -> str:
        """A stable identity for the IR run.

        Band and class are in it because the strata are scored separately and
        a scorer that has to re-join to the case list to know which band a
        query was in is a scorer that can get the join wrong.
        """
        return f"{self.band}|{self.typo_class}|{self.title_id}"


@dataclass(frozen=True, slots=True)
class Frame:
    """The sampling frame, as observed.

    Frozen, and **not hashable** -- `pools` is a `Mapping`, and a `dict` or a
    `mappingproxy` in a field makes the generated `__hash__` raise
    `TypeError`. Stated because this repository has been bitten by "frozen
    therefore hashable" before; nothing here needs the hash.
    """

    shared_lower_names: int
    pools: Mapping[str, int]


def mutate(name: str, typo_class: str, chooser: random.Random) -> str | None:
    """One single-edit typo of `name`, or `None` where the class does not apply.

    The four classes ADR-0002 named, at a uniformly random position.

    **A transposition draws from the positions that transpose to something
    else, and the case count is what says so.** Drawing uniformly and
    declining when the two characters match produces 2,964 against the gate's
    2,993 -- 29 short, all names holding a doubled letter at the drawn
    position. The gate's arithmetic is `3000 - 7`, and the seven are the
    two-character names that admit no deletion, so its transposition arm
    declined nothing. Emitting the unmutated name is another way to reach
    3,000 and is worse: a guaranteed hit for any index, which would make the
    2-4 band's measured 0.0% arithmetically impossible. Rejection sampling --
    redraw until the two characters differ -- reaches 2,993 too and emits no
    unmutated name, so this is the *simplest* reading that produces both
    numbers rather than the only one.
    """
    length = len(name)
    if typo_class == "substitution":
        at = chooser.randrange(length)
        replacement = chooser.choice("abcdefghijklmnopqrstuvwxyz")
        if replacement == name[at].lower():
            replacement = "z" if replacement != "z" else "q"
        return name[:at] + replacement + name[at + 1 :]
    if typo_class == "deletion":
        if length <= 2:
            return None
        at = chooser.randrange(length)
        return name[:at] + name[at + 1 :]
    if typo_class == "transposition":
        positions = [one for one in range(length - 1) if name[one] != name[one + 1]]
        if not positions:
            return None
        at = chooser.choice(positions)
        return name[:at] + name[at + 1] + name[at] + name[at + 2 :]
    if typo_class == "doubled":
        at = chooser.randrange(length)
        return name[:at] + name[at] + name[at:]
    raise ValueError(f"unknown typo class {typo_class}")


def build_typo_cases(
    pools: Mapping[str, Sequence[tuple[uuid.UUID, str]]],
    *,
    seed: int = GATE_SEED,
) -> tuple[TypoCase, ...]:
    """The gate's cases, from pools the caller read.

    **The RNG is consumed in exactly one order and the order is the
    measurement.** One `random.Random(seed)` for the whole run; bands in
    `GATE_BANDS` order; `sample` per band; then the four classes per drawn
    row in `TYPO_CLASSES` order. Any other order draws a different set from
    the same seed, which is the silent way two runs stop being comparable.
    `pools` must therefore arrive ordered by `titles.id`, which `read_pools`
    guarantees with its `ORDER BY`.
    """
    # `random.Random(20260803)` is the gate's own seed. Reproducibility is the
    # entire point; a cryptographic generator here would make the two runs
    # incomparable, which is the defect S311 would be preventing if this were
    # a token.
    chooser = random.Random(seed)  # noqa: S311
    cases: list[TypoCase] = []
    for band, _low, _high in GATE_BANDS:
        rows = list(pools.get(band, ()))
        # Clamped only so a smoke run against a toy catalog exercises this at
        # all. On the real catalog every pool exceeds 150 and `check_frame`
        # has already refused if it does not, so the clamp is unreachable
        # there -- the only condition under which a clamp is not quietly
        # redefining the measurement.
        drawn = chooser.sample(rows, min(GATE_DRAW_PER_BAND, len(rows)))
        for title_id, name in drawn:
            for typo_class in TYPO_CLASSES:
                probe = mutate(name, typo_class, chooser)
                if probe is None:
                    continue
                cases.append(
                    TypoCase(
                        title_id=title_id,
                        name=name,
                        band=band,
                        typo_class=typo_class,
                        probe=probe,
                    )
                )
    return tuple(cases)


def check_frame(observed: Frame) -> Frame:
    """The gate's sampling frame, reproduced or refused.

    Six numbers and all six have to land. A pool one row out is a different
    eligible population, and a recall figure over a different population is
    not the gate's however close it looks.

    **This doubles as the suggest surface's comparability check.** The frame
    numbers *are* what a suggest run depends on, so a frame that reproduces
    is a baseline that is comparable -- which is why `fingerprint.py` digests
    them rather than inventing a second notion of catalog drift.

    **The refusal names the number that moved.** An operator meets this in CI
    as `baseline-invalid`, and the first spelling dumped two five-entry dicts
    and two scalars beside each other and left them to diff six numbers by
    eye -- for a check whose entire thesis is *one row out*. Reporting the
    drift alone also distinguishes an **absent** band, which arrives as a
    `None` in the observed slot rather than folding into a dict inequality
    that says only "these are not equal".
    """
    expected = Frame(shared_lower_names=GATE_SHARED_LOWER_NAMES, pools=dict(GATE_POOLS))
    observed_pools = dict(observed.pools)
    checked: tuple[tuple[str, int | None, int | None], ...] = (
        ("shared_lower_names", expected.shared_lower_names, observed.shared_lower_names),
        *((band, count, observed_pools.get(band)) for band, count in expected.pools.items()),
        # A band the gate never had. Unreachable through `read_frame`, which
        # only ever writes `GATE_BANDS`' keys -- but the equality this replaced
        # would have caught it, and a rewrite that quietly drops a check is how
        # a check stops existing.
        *(
            (band, None, observed_pools[band])
            for band in observed_pools
            if band not in expected.pools
        ),
    )
    drift = {label: (want, got) for label, want, got in checked if want != got}
    if drift:
        raise EvalRefused(
            "the sampling frame does not reproduce the gate's -- "
            + ", ".join(
                f"{label}: expected {want}, observed {got}" for label, (want, got) in drift.items()
            )
            + ". Every recall number would be over a different population."
        )
    return observed


async def read_pools(session: AsyncSession) -> dict[str, list[tuple[uuid.UUID, str]]]:
    """The eligible rows per band, ordered by id so the draw is reproducible."""
    pools: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for band, low, high in GATE_BANDS:
        rows = (await session.execute(text(_ELIGIBLE), {"low": low, "high": high})).all()
        pools[band] = [(row.id, row.name) for row in rows]
    return pools


async def read_frame(session: AsyncSession) -> Frame:
    """The frame as this catalog presents it, counted from the same statement
    `read_pools` draws from."""
    shared = (
        await session.execute(
            text(
                "SELECT count(*) FROM (SELECT lower(name) FROM titles "
                "GROUP BY 1 HAVING count(*) > 1) AS shared"
            )
        )
    ).scalar_one()
    pools: dict[str, int] = {}
    for band, low, high in GATE_BANDS:
        pools[band] = (
            await session.execute(
                # The interpolation is `_ELIGIBLE`, this module's own literal
                # and the statement `read_pools` selects from; the only two
                # values that vary cross as bind parameters.
                text(f"SELECT count(*) FROM ({_ELIGIBLE}) AS eligible"),  # noqa: S608
                {"low": low, "high": high},
            )
        ).scalar_one()
    return Frame(shared_lower_names=int(shared), pools=pools)
