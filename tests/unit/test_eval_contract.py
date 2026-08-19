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

    assert set(contract["source_modules"]) == walked - _EXEMPT - _THE_PACKAGE_ITSELF, (
        "the eval contract's source list has drifted from the package. "
        f"unlisted (free to import usher.eval): "
        f"{sorted(walked - _EXEMPT - _THE_PACKAGE_ITSELF - set(contract['source_modules']))}; "
        f"listed but gone: {sorted(set(contract['source_modules']) - walked)}"
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
