"""`usher.ports.repository` is a package mirroring `usher.db.repositories`
module for module, and these cases are what keep it one.

It was a single 3,434-line module holding 19 ABCs, 107 abstract methods and 19
supporting dataclasses, against implementations that had already split per
aggregate under `src/usher/db/repositories/`. 99 files imported it, so every
service that wanted one port imported a module holding eighteen others, and M8
alone added 616 insertions to it -- because a new port goes where the ports
already are.

**Splitting it once fixes nothing by itself.** The next port lands in whichever
module its author happened to open, and a decade of that is how the single
module got there in the first place. The mirror is what makes the answer
mechanical -- `PostgresThingRepository` lives in `usher.db.repositories.thing`,
so `ThingRepository` lives in `usher.ports.repository.thing` -- and this file is
what makes the mirror a failing test rather than a convention. Four groups of
M9 add a port to this package; none of them has to decide anything.

`usher.ports.repository.search` collides by name with `usher.ports.search` and
is deliberately not renamed: `usher.db.repositories.search` and
`usher.adapters.search` are already that same pair one layer down, and the
mirror is the whole value.
"""

import ast
import importlib
import inspect
import pkgutil
import tomllib
from abc import ABC
from pathlib import Path
from types import ModuleType

import usher.db.repositories
import usher.ports.repository

# The two `Postgres*` classes under `usher.db.repositories` whose port is not,
# and never will be, a repository port. `CredentialStore` is declared in
# `usher.ports.credentials` and `JobQueue` in `usher.ports.jobs`; both sit in
# `db/repositories/` because that is where Postgres implementations live, not
# because they are repositories. Exempted **by name** rather than by a rule
# that would also excuse a genuinely misplaced port -- and the reason travels
# into the assertion message, because a bare name in a skip list is the thing a
# later reader deletes without knowing what it bought.
NOT_REPOSITORY_PORTS: dict[str, str] = {
    "PostgresCredentialStore": "usher.ports.credentials",
    "PostgresJobQueue": "usher.ports.jobs",
}

# Measured by AST over `ports/repository.py` at the commit before the split, and
# stated as **floors** rather than equalities on this repository's own
# precedent (`test_decision_register.py` asserts `>= 23` ADRs against 28 that
# exist). Four M9 groups add a port to this package, and an equality here would
# be a line each of them has to edit -- which is how a count stops being a
# measurement and becomes a number people bump until it is green. The claim
# that the move itself lost nothing is not made here at all: it was made once,
# by comparing `inspect.getsource` of all 38 public objects against
# `git show HEAD:src/usher/ports/repository.py`, byte for byte.
PORTS_AT_THE_SPLIT = 19
ABSTRACT_METHODS_AT_THE_SPLIT = 107
SUPPORTING_TYPES_AT_THE_SPLIT = 19


def _package_modules() -> list[ModuleType]:
    """Every module of `usher.ports.repository`, `_results` included."""
    return [
        importlib.import_module(f"usher.ports.repository.{module.name}")
        for module in pkgutil.iter_modules(usher.ports.repository.__path__)
    ]


def _aggregate_modules() -> list[ModuleType]:
    """The per-aggregate modules only: the ones the mirror is a claim about.

    `_results` is private and named for what it holds rather than for an
    aggregate, so it has no `db/repositories` counterpart and is not one.
    """
    return [
        module
        for module in _package_modules()
        if not module.__name__.rpartition(".")[2].startswith("_")
    ]


def _postgres_port_pairs() -> list[tuple[str, type, type]]:
    """Every `(module name, PostgresThing, Thing)` under `usher.db.repositories`.

    Paired by name -- a class called `PostgresThing` whose bases include one
    called `Thing` -- so the scan cannot be satisfied by a class that merely
    subclasses something abstract.
    """
    pairs: list[tuple[str, type, type]] = []
    for module in pkgutil.iter_modules(usher.db.repositories.__path__):
        namespace = importlib.import_module(f"usher.db.repositories.{module.name}")
        for value in vars(namespace).values():
            if not isinstance(value, type) or value.__module__ != namespace.__name__:
                continue
            port_name = value.__name__.removeprefix("Postgres")
            if port_name == value.__name__:
                continue
            for base in value.__bases__:
                if base.__name__ == port_name:
                    pairs.append((module.name, value, base))
    return pairs


def _public_objects(namespace: ModuleType) -> dict[str, type]:
    """The classes a module declares itself, private names excluded."""
    return {
        name: value
        for name, value in vars(namespace).items()
        if isinstance(value, type)
        and value.__module__ == namespace.__name__
        and not name.startswith("_")
    }


def test_every_postgres_repository_module_has_a_port_module_of_the_same_name() -> None:
    """**The invariant the split exists for.** Every `PostgresThingRepository`
    in `usher.db.repositories.thing` implements a `ThingRepository` declared in
    `usher.ports.repository.thing` -- same module name, both sides, no
    exceptions beyond the two named above.

    Nineteen such pairs exist across sixteen modules; three modules hold two
    ports each (`people`, `search`, `sync`), which is why the mirror is stated
    module-for-module and not port-for-port.

    Both premise assertions are load-bearing rather than decoration. A scan
    that globs nothing passes exactly like a scan that passes -- the failure
    `test_ports.py::test_every_port_abc_is_registered_in_all_ports` carries the
    same guard against, and the one its own sweep found -- so `pairs` is
    asserted non-empty *and* a named anchor is asserted present, because an
    import that quietly stopped resolving would empty the walk without emptying
    the list.
    """
    pairs = _postgres_port_pairs()
    assert pairs, "the repository scan found nothing, so it proves nothing"
    assert "PostgresTitleRepository" in {impl.__name__ for _, impl, _ in pairs}, (
        "the repository scan ran but did not find PostgresTitleRepository, "
        "so it is walking something other than usher.db.repositories"
    )

    misplaced = {
        impl.__name__: (port.__module__, f"usher.ports.repository.{module_name}")
        for module_name, impl, port in pairs
        if impl.__name__ not in NOT_REPOSITORY_PORTS
        and port.__module__ != f"usher.ports.repository.{module_name}"
    }
    exempt = ", ".join(
        f"{name} (its port is in {where}, and always will be)"
        for name, where in sorted(NOT_REPOSITORY_PORTS.items())
    )
    assert not misplaced, (
        "usher.ports.repository mirrors usher.db.repositories module for "
        "module, so a port belongs in the module named for its aggregate. "
        f"These do not: {misplaced!r} (each value is the module it is in, then "
        f"the module it belongs in). Exempt by name: {exempt}."
    )


def test_the_package_re_exports_every_public_object_its_modules_declare() -> None:
    """`__init__.__all__` is the whole compatibility story: 99 files import
    `from usher.ports.repository import ...` and not one of them changed for
    the split. Under mypy's `no_implicit_reexport` a name missing from `__all__`
    is not importable at all, so a module added without its `__all__` entry
    breaks every call site rather than the one file that forgot -- which is why
    the completeness check is a test and not a review habit.
    """
    declared: dict[str, str] = {}
    for namespace in _package_modules():
        for name in _public_objects(namespace):
            declared[name] = namespace.__name__
    assert declared, "the port-module scan found nothing, so it proves nothing"
    assert declared.get("TitleRepository") == "usher.ports.repository.title", (
        "the port-module scan ran but TitleRepository is not where it belongs, "
        "so this case is measuring the wrong package"
    )

    exported = set(usher.ports.repository.__all__)
    unexported = {name: where for name, where in declared.items() if name not in exported}
    assert not unexported, (
        "these public objects are declared in a usher.ports.repository module "
        f"but missing from the package's __all__, so `from usher.ports.repository "
        f"import <name>` does not resolve under no_implicit_reexport: {unexported!r}"
    )
    dangling = exported - set(declared)
    assert not dangling, f"__all__ names objects no module declares: {sorted(dangling)}"


def test_the_independence_contract_names_every_aggregate_port_module() -> None:
    """The tenth `import-linter` contract holds the same invariant as the case
    below, as a graph property rather than as one file's AST scan -- and **its
    module list is the whole contract**, so a port module that lands unlisted is
    a port module nothing constrains while the gate still reports 10 kept.

    `pyproject.toml` already records that failure mode about its own
    `forbidden_modules` list one contract up (*"a seventh adapter package left
    out is a package the contract silently stops covering"*). The repair there
    is a sentence; the repair here is this case, because the membership is
    derivable: it is exactly what `_aggregate_modules()` walks.

    `_results` is deliberately absent from both sides. It is the shared private
    module every aggregate may import, and `_aggregate_modules()` drops it for
    the same reason -- so the two lists agree by construction rather than by
    two people remembering the same exception.
    """
    with (Path(__file__).parents[2] / "pyproject.toml").open("rb") as handle:
        contracts = tomllib.load(handle)["tool"]["importlinter"]["contracts"]

    named = [one for one in contracts if one["type"] == "independence"]
    assert len(named) == 1, (
        "expected exactly one independence contract; the assertion below is "
        f"about a list, so two of them would silently check the wrong one: {named!r}"
    )
    listed = set(named[0]["modules"])
    walked = {module.__name__ for module in _aggregate_modules()}

    assert walked, "the port-module scan found nothing, so it proves nothing"
    assert "usher.ports.repository.title" in walked, (
        "the port-module scan ran but found no title module, "
        "so it is walking something other than usher.ports.repository"
    )
    assert listed == walked, (
        "the independence contract's module list has drifted from the package. "
        f"unlisted (unconstrained): {sorted(walked - listed)}; "
        f"listed but gone: {sorted(listed - walked)}"
    )


def test_no_aggregate_module_imports_another_aggregate_module() -> None:
    """**The cycle the private `_results` module exists to prevent.**

    `BulkWriteResult` is returned by six ports across six modules. Homing it in
    `bulk.py` and importing it back the other way resolves perfectly well today
    -- and makes `collection.py`, `episode.py`, `media_item.py`, `people.py` and
    `search.py` each drag the bulk-load port into every consumer, which is one
    package over from the shape the ninth import contract was written for. The
    private `_results.py` is the fix, and this case is what keeps it applied:
    aggregate modules may import `_results`, and may not import each other.

    Asserted over the *source* rather than by importing and looking for an
    `ImportError`, because a cycle between two modules both reachable from
    `__init__` frequently does not raise -- it resolves in whichever order
    `__init__` happens to list them, and then stops resolving for the first
    person who imports a submodule directly.
    """
    modules = _aggregate_modules()
    assert modules, "the port-module scan found nothing, so it proves nothing"
    names = {module.__name__ for module in modules}
    assert "usher.ports.repository.title" in names, (
        "the port-module scan ran but found no title module, "
        "so it is walking something other than usher.ports.repository"
    )

    offenders: dict[str, list[str]] = {}
    for module in modules:
        source = Path(inspect.getsourcefile(module) or "").read_text()
        reached = sorted(
            {
                node.module
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.startswith("usher.ports.repository.")
                and node.module in names
            }
        )
        if reached:
            offenders[module.__name__] = reached
    assert not offenders, (
        "an aggregate port module imported another one, which is the cycle "
        "usher.ports.repository._results exists to prevent -- a shared return "
        f"type goes in _results.py, not in whichever port returns it first: {offenders!r}"
    )


def test_every_port_and_abstract_method_in_the_package_carries_a_docstring() -> None:
    """A docstring lost in a 3,434-line move is invisible to every other test in
    this repository: strip the docstrings from `ports/repository.py` and
    `ast.unparse` leaves **619 of 3,434 lines**, so roughly four lines in five
    are the prose the ports are mostly *for*. Nothing here can prove the move
    preserved a docstring's wording -- `getsource` did that, once, against
    `git show HEAD:` -- but this is what stops the next port arriving without
    one, and what would have caught a whole class quietly dropped.

    The counts are floors and the constants say what they were measured at. A
    dataclass with no docstring of its own inherits a synthesised
    `Thing(field: int, ...)` from `@dataclass`, which `inspect.getdoc` reports
    as prose, so the check is against `__doc__` in the class's own `__dict__`
    with the synthesised signature rejected by name.
    """
    ports: list[type[ABC]] = []
    supporting: list[type] = []
    missing: list[str] = []
    abstract_methods = 0

    for namespace in _package_modules():
        for name, value in _public_objects(namespace).items():
            own = value.__dict__.get("__doc__")
            if not own or not own.strip() or own.startswith(f"{name}("):
                missing.append(f"{namespace.__name__}.{name}")
            if issubclass(value, ABC) and getattr(value, "__abstractmethods__", None):
                ports.append(value)
                for method in sorted(value.__abstractmethods__):
                    abstract_methods += 1
                    doc = getattr(getattr(value, method), "__doc__", None)
                    if not doc or not doc.strip():
                        missing.append(f"{namespace.__name__}.{name}.{method}")
            else:
                supporting.append(value)

    assert ports, "the port-module scan found nothing, so it proves nothing"
    assert not missing, f"public objects in the ports package with no docstring: {missing}"
    assert len(ports) >= PORTS_AT_THE_SPLIT
    assert len(supporting) >= SUPPORTING_TYPES_AT_THE_SPLIT
    assert abstract_methods >= ABSTRACT_METHODS_AT_THE_SPLIT
