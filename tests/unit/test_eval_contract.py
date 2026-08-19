"""One structural guarantee about `usher.eval` that no runtime test can see.

It is an absence claim -- *nothing imports this package* -- and an absence is
exactly what rots silently, because the thing that would falsify it is a line
somebody adds in a file this test does not name. A later task widens this
module to a second such claim, a schema that acquires a migration; until that
task lands there is one, and a docstring promising two would be the same kind
of rot one layer up.
"""

import pkgutil
import tomllib
from pathlib import Path

import usher

_ROOT = Path(__file__).resolve().parents[2]

# `usher.cli` is the eval package's composition root -- `usher eval` is a
# subcommand -- so it is exempt, exactly as `usher.composition` is exempt from
# the contracts it composes. `usher.eval` is the forbidden module itself and
# cannot be a source of a contract forbidding it. Every other top-level name
# is a source, and the case below is what makes that sentence true rather than
# aspirational.
_EXEMPT = {"usher.cli"}
_THE_PACKAGE_ITSELF = {"usher.eval"}


def _top_level_names() -> set[str]:
    """Every top-level importable name under `src/usher/`.

    `pkgutil.iter_modules` and not a `glob`, because the question the contract
    asks is which names are *importable*: it yields `__main__` (a real module
    that a `glob` for packages would miss and the contract did miss) and drops
    `__init__` (not a separate importable name), which is exactly the set the
    contract has to cover.
    """
    return {f"usher.{module.name}" for module in pkgutil.iter_modules(usher.__path__)}


def test_the_eval_package_is_named_by_an_import_contract() -> None:
    """The allowlist note in `[tool.importlinter]` says a new top-level package
    must be named by some contract or it escapes all of them -- and **the
    contract's `source_modules` list is the whole contract**, so a top-level
    name that lands unlisted is a module free to import a dev-only extra while
    the gate still reports 11 kept.

    That is not hypothetical here: `usher.__main__` was missing from the list
    as first written, and a *used* `from usher.eval import goldens` planted in
    it reported **11 kept, 0 broken**. The container entrypoint could have
    imported the eval harness and nothing would have said so.

    So the expectation is **derived rather than hand-written**, which is the
    repair `test_ports_repository_package.py` makes for the same failure mode
    one contract up: the membership is exactly what `_top_level_names()` walks,
    so the two agree by construction instead of by someone remembering. A
    hand-written subset -- four layers checked in a loop, as this case began --
    passes just as happily against a list missing five.
    """
    with (_ROOT / "pyproject.toml").open("rb") as handle:
        contracts = tomllib.load(handle)["tool"]["importlinter"]["contracts"]

    naming = [one for one in contracts if "usher.eval" in one.get("forbidden_modules", [])]
    assert len(naming) == 1, (
        "expected exactly one contract forbidding usher.eval; the assertion "
        "below is about a list, so two of them would silently check the wrong "
        f"one -- a narrower contract ahead of this one in the array would pass "
        f"every check while this one was gutted: {naming!r}"
    )
    (contract,) = naming

    walked = _top_level_names()
    assert len(walked) >= 8, (
        "the top-level scan found "
        f"{len(walked)} names, which is fewer than usher has had since M1 -- "
        "a scan pointed at the wrong directory globs nothing and passes "
        f"exactly like one that passes: {sorted(walked)}"
    )
    assert "usher.domain" in walked, (
        "the top-level scan ran but found no usher.domain, "
        f"so it is walking something other than src/usher: {sorted(walked)}"
    )

    assert "usher.cli" in walked, (
        "usher.cli is exempted below by subtraction, so a rename that made the "
        "exemption stale would silently widen this case rather than fail it"
    )
    assert "usher.cli" not in contract["source_modules"], (
        "usher.cli is the eval package's composition root and must stay exempt"
    )

    assert set(contract["source_modules"]) == walked - _EXEMPT - _THE_PACKAGE_ITSELF, (
        "the eval contract's source list has drifted from the package. "
        f"unlisted (free to import usher.eval): "
        f"{sorted(walked - _EXEMPT - _THE_PACKAGE_ITSELF - set(contract['source_modules']))}; "
        f"listed but gone: {sorted(set(contract['source_modules']) - walked)}"
    )
