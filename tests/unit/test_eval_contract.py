"""Structural guarantees about `usher.eval` that no runtime test can see.

Each is an absence claim -- *nothing imports this package*, *nothing outside
one package imports `ranx`* -- and an absence is exactly what rots silently,
because the thing that would falsify it is a line somebody adds in a file this
test does not name. There are three cases now: the eleventh contract's source
list, the twelfth's, and the one inch of the twelfth's claim that a `forbidden`
contract cannot express. A later task widens this module to a fourth, a schema
that acquires a migration; until that task lands there are three, and a
docstring promising four would be the same kind of rot one layer up.

**Every case here derives its expectation from a walk of the package**, which
is the repair `test_ports_repository_package.py` makes for the same failure
mode one contract up: a static-analysis contract configured by an enumeration
needs a test that the enumeration is complete, and a hand-written expected list
is a second copy of the thing under test. Each walk carries its own premise
guard, because a scan that globs nothing passes exactly like a scan that
passes.
"""

import ast
import pkgutil
import tomllib
from pathlib import Path
from typing import Any

import grimp

import usher
import usher.eval

_ROOT = Path(__file__).resolve().parents[2]

# `usher.cli` is the eval package's composition root -- `usher eval` is a
# subcommand -- so it is exempt, exactly as `usher.composition` is exempt from
# the contracts it composes. `usher.eval` is the forbidden module itself and
# cannot be a source of a contract forbidding it. Every other top-level name
# is a source, and the case below is what makes that sentence true rather than
# aspirational.
_EXEMPT = {"usher.cli"}
_THE_PACKAGE_ITSELF = {"usher.eval"}

# The twelfth contract's exemption, and it is a different one on purpose.
# `usher.cli` is *not* exempt there: being the harness's composition root is a
# reason to let it import `usher.eval`, and no reason at all to let it import
# `ranx`. What is exempt is the one package allowed to name the library, and it
# is exempted from *both* walks by subtraction -- `usher.eval` drops out of the
# top-level walk because its children are enumerated instead.
_MAY_IMPORT_RANX = "usher.eval.metrics"

# The fifth contract, found by what it forbids rather than by its name, for the
# reason every list in this module is derived rather than written down. It is
# the contract the eleventh's `allow_indirect_imports` leans on -- see
# `test_the_eval_package_is_named_by_an_import_contract`.
_THE_COMPOSITION_ROOT = "usher.cli"

# The one source the eleventh contract lists that nothing in the fifth
# contract's six can reach -- which is what makes it the documented exception to
# that contract's safety argument. It is the *only* thing named here: the case
# below derives the unreachable set from the graph and asserts it equals exactly
# this, rather than looping over the three modules that are reachable, which
# would be a second copy of a fact a reader has to trust somebody enumerated.
_REACHED_BY_NOTHING = "usher.__main__"


def _top_level_names() -> set[str]:
    """Every top-level importable name under `src/usher/`.

    `pkgutil.iter_modules` and not a `glob`, because the question the contract
    asks is which names are *importable*: it yields `__main__` (a real module
    that a `glob` for packages would miss and the contract did miss) and drops
    `__init__` (not a separate importable name), which is exactly the set the
    contract has to cover.
    """
    return {f"usher.{module.name}" for module in pkgutil.iter_modules(usher.__path__)}


def _eval_child_names() -> set[str]:
    """Every importable name directly under `src/usher/eval/`.

    The twelfth contract cannot name `usher.eval` as a source -- a `forbidden`
    contract's sources cover a module *and all its descendants*, so that would
    forbid the one import the design exists for -- so it names `usher.eval`'s
    children individually, minus the one package allowed to import `ranx`.
    """
    return {f"usher.eval.{module.name}" for module in pkgutil.iter_modules(usher.eval.__path__)}


def _import_linter_config() -> dict[str, Any]:
    with (_ROOT / "pyproject.toml").open("rb") as handle:
        loaded: dict[str, Any] = tomllib.load(handle)["tool"]["importlinter"]
    return loaded


def _contracts() -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = _import_linter_config()["contracts"]
    return contracts


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

    **`allow_indirect_imports` is asserted here too, and it is the same kind of
    claim: the configuration *is* the contract.** Measured 2026-08-19 -- with
    the flag deleted and the `usher eval` subcommand planted (a used,
    ruff-clean `from usher.eval.metrics import ir` in `usher/cli.py`), this
    contract reports **11 kept, 1 broken** on
    `usher.__main__ -> usher.cli -> usher.eval.metrics.ir`, because a
    `forbidden` contract reports indirect chains by default and the container
    entrypoint imports the CLI. So the `usher.cli` exemption does not hold for
    the one case it exists for, and the flag is what makes it hold.

    **The assertion is here because the repair somebody will reach for is the
    wrong one.** The red names `usher.__main__`, so the obvious fix is to drop
    it from `source_modules` -- which unpicks the measured hole recorded above
    (a used `from usher.eval import goldens` in `__main__.py` reported *11
    kept, 0 broken* while unlisted) and then fails the set equality below,
    pointing the reader further from the repair. A one-token deletion that
    re-arms a trap is exactly the shape a configuration test exists for.

    **And the flag's *safety* argument is pinned too, which until 2026-08-19 it
    was not.** The flag is defensible because the fifth contract still reports a
    chain through `usher.cli` for every source that can reach it -- a measured
    graph fact (`usher.config`, `usher.composition` and `usher.telemetry` had
    3, 3 and 12 direct importers inside that contract's six on the day it was
    written) with nothing checking it, next to four things about the same flag
    that *were* checked. A refactor leaving one of those three unimported from
    within the six would reopen the hole with the gate still at 12 kept and
    every existing assertion here green, so the last block below derives the
    reachability from `grimp` instead, and derives `usher.__main__` as the
    single exception rather than repeating the prose.
    """
    naming = [one for one in _contracts() if "usher.eval" in one.get("forbidden_modules", [])]
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
    assert contract.get("allow_indirect_imports") is True, (
        "without this the contract breaks on usher.__main__ -> usher.cli -> "
        "usher.eval the day `usher eval` lands, which is the subcommand the "
        "usher.cli exemption exists for -- and the repair the red invites is "
        "to delete usher.__main__ from source_modules, which reopens a "
        "measured hole. Every *direct* import of usher.eval is still caught in "
        "every source listed; what the flag gives up is a chain through "
        "usher.cli, which the fifth contract still reports for every source "
        "but usher.__main__"
    )

    assert set(contract["source_modules"]) == walked - _EXEMPT - _THE_PACKAGE_ITSELF, (
        "the eval contract's source list has drifted from the package. "
        f"unlisted (free to import usher.eval): "
        f"{sorted(walked - _EXEMPT - _THE_PACKAGE_ITSELF - set(contract['source_modules']))}; "
        f"listed but gone: {sorted(set(contract['source_modules']) - walked)}"
    )

    # **The safety argument for `allow_indirect_imports`, which until now rested
    # on a measured graph fact that nothing checked.** What the flag gives up is
    # a chain through `usher.cli`; what makes that acceptable is that the fifth
    # contract ("cli is a composition root, nothing depends on it") carries no
    # such flag, so any source it *can reach* still gets the chain reported
    # there. `usher.__main__` is the documented exception -- nothing imports it,
    # which is exactly what it is for -- and the rest of the argument is a
    # property of the graph that a refactor could quietly falsify with the gate
    # still reporting 12 kept.
    #
    # So the exception is **derived** rather than asserted in prose: every
    # source the eleventh contract lists and the fifth does not is checked for
    # reachability, and the set that comes back unreached must be exactly
    # `usher.__main__`. A refactor that left `usher.telemetry` unimported from
    # within those six fails here by name.
    guarding = [
        one for one in _contracts() if one.get("forbidden_modules") == [_THE_COMPOSITION_ROOT]
    ]
    assert len(guarding) == 1, (
        "expected exactly one contract forbidding usher.cli -- the assertion "
        "below reads its source list, so two of them would silently check the "
        f"wrong one: {guarding!r}"
    )
    (composition_root,) = guarding
    watched = set(composition_root["source_modules"])
    assert len(watched) >= 6 and "usher.services" in watched, (
        "the composition-root contract's source list is not the six this "
        "argument rests on, so every reachability answer below is about some "
        f"other question: {sorted(watched)}"
    )

    graph = grimp.build_graph("usher")
    assert "usher.domain" in graph.modules and len(graph.modules) >= 100, (
        f"grimp built a graph of {len(graph.modules)} modules with no "
        "usher.domain in it, so it is reading something other than src/usher "
        "-- and a graph that found nothing answers False to every chain query, "
        "which would read as 'nothing is reachable' rather than as a broken "
        "premise"
    )

    beyond = set(contract["source_modules"]) - watched
    assert _REACHED_BY_NOTHING in beyond, (
        f"{_REACHED_BY_NOTHING} is the exception this assertion derives, so it "
        "has to be one of the sources the fifth contract does not already "
        f"watch: listed beyond the six are {sorted(beyond)}"
    )
    unreached = {
        one
        for one in beyond
        if not any(
            graph.chain_exists(importer=source, imported=one, as_packages=True)
            for source in watched
        )
    }
    assert unreached == {_REACHED_BY_NOTHING}, (
        "the eleventh contract's allow_indirect_imports is safe only because "
        "every source it lists beyond the fifth contract's six is reachable "
        "from one of those six, so a chain through usher.cli is still reported "
        f"there. Now unreachable, and therefore silently exempt from both: "
        f"{sorted(unreached - {_REACHED_BY_NOTHING})}; newly reachable, so the "
        "documented exception has moved and this comment with it: "
        f"{sorted({_REACHED_BY_NOTHING} - unreached)}"
    )


def test_the_ranx_contract_names_every_module_that_may_not_import_it() -> None:
    """The same shape one contract over, for the twelfth.

    `usher/eval/metrics/__init__.py` says `ir.py` is the only module in this
    project that imports `ranx`, and that sentence is the whole mitigation for
    the risk it records -- `numba`/`llvmlite` pin an LLVM ABI and lag new
    CPython, so the escape to `ir_measures` has to stay a small, bounded change.
    Until 2026-08-19 the sentence was conventional and `pyproject.toml` claimed
    otherwise: `ranx` was not in the import graph at all, and a *used*
    `import ranx` planted in `usher/adapters/http.py` and `usher/eval/errors.py`
    at once reported **11 kept, 0 broken**.

    It is a contract now, and **the source list is the whole contract** -- a
    module that lands unlisted is a module free to import the library while the
    gate reports 12 kept. So the expectation is derived from two walks rather
    than written down twice: every top-level name, plus `usher.eval`'s own
    children because the package cannot be named as a source without forbidding
    the one import the design exists for.
    """
    naming = [one for one in _contracts() if "ranx" in one.get("forbidden_modules", [])]
    assert len(naming) == 1, (
        "expected exactly one contract forbidding ranx; the assertion below is "
        "about a list, so two of them would silently check the wrong one: "
        f"{naming!r}"
    )
    (contract,) = naming

    assert _import_linter_config().get("include_external_packages") is True, (
        "the twelfth contract names an external package, which grimp only puts "
        "in the graph when include_external_packages is set. import-linter "
        "refuses to start without it -- measured -- so this assertion is the "
        "legible failure rather than the only one"
    )
    assert contract.get("allow_indirect_imports") is True, (
        "without this the contract breaks on usher.cli -> usher.eval.metrics.ir "
        "-> ranx, which is the eval subcommand doing exactly what it is for; "
        "the claim being enforced is that nothing outside usher.eval.metrics "
        "*names* ranx"
    )

    walked = _top_level_names()
    children = _eval_child_names()
    assert len(walked) >= 8, (
        f"the top-level scan found {len(walked)} names, fewer than usher has "
        "had since M1 -- a scan pointed at the wrong directory globs nothing "
        f"and passes exactly like one that passes: {sorted(walked)}"
    )
    assert len(children) >= 3, (
        f"the usher.eval scan found {len(children)} names, fewer than the "
        "package has had since this harness was created -- same trap, one "
        f"level down: {sorted(children)}"
    )
    assert "usher.eval" in walked, (
        "usher.eval is dropped from the top-level walk by subtraction below "
        "and its children enumerated instead, so a rename would silently widen "
        f"this case rather than fail it: {sorted(walked)}"
    )
    assert _MAY_IMPORT_RANX in children, (
        "the one exempt package is exempted by subtraction, so a rename or a "
        f"move would silently widen this case rather than fail it: {sorted(children)}"
    )
    assert "usher.cli" in set(contract["source_modules"]), (
        "usher.cli is exempt from the eleventh contract and must not be exempt "
        "from this one: composing the harness is a reason to import usher.eval, "
        "not a reason to import ranx"
    )

    expected = (walked - _THE_PACKAGE_ITSELF) | (children - {_MAY_IMPORT_RANX})
    assert set(contract["source_modules"]) == expected, (
        "the ranx contract's source list has drifted from the package. "
        f"unlisted (free to import ranx): "
        f"{sorted(expected - set(contract['source_modules']))}; "
        f"listed but gone: {sorted(set(contract['source_modules']) - expected)}"
    )


def test_only_the_ir_module_inside_the_metrics_package_names_ranx() -> None:
    """The one inch of the twelfth contract's claim that the contract cannot
    reach, held by a scan for M8 Task 17's recorded reason -- *prefer a graph
    property wherever one is expressible*, and cover what it cannot with the
    other kind of check, because neither subsumes the other.

    A `forbidden` contract's `source_modules` cover a module **and all its
    descendants**, so `usher.eval.metrics` cannot be a source with `ir` carved
    out of it. A second module inside that package importing `ranx` is
    therefore KEPT by the contract, while `metrics/__init__.py` still says
    `ir.py` is the only one.

    **An `ast` walk and not a text scan**, because the text is already there:
    `metrics/__init__.py`'s own docstring names `ranx` three times explaining
    why it is confined, and a `"ranx" in source` scan would report the file
    that documents the rule as the file that breaks it -- then be "fixed" by
    deleting the explanation. Same trap `test_api_rows.py` hit from the other
    side, where prose *satisfied* a scan on behalf of a reader that did not
    exist.
    """
    package = _ROOT / "src" / "usher" / "eval" / "metrics"
    scanned = sorted(path.name for path in package.glob("*.py"))
    assert len(scanned) >= 2 and "__init__.py" in scanned, (
        f"the metrics package scan found {scanned}, which is not a package -- a "
        "glob pointed at the wrong directory finds nothing and passes exactly "
        "like one that finds everything"
    )

    importers = set()
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import) and any(
                alias.name.split(".")[0] == "ranx" for alias in node.names
            ):
                importers.add(path.name)
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "ranx":
                importers.add(path.name)

    assert importers == {"ir.py"}, (
        "usher/eval/metrics/__init__.py claims ir.py is the only module in this "
        "project that imports ranx, and the twelfth import contract cannot see "
        "inside this package. Modules here importing ranx: "
        f"{sorted(importers)}; files scanned: {scanned}"
    )
