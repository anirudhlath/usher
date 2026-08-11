"""**No port takes a cursor**, as a test rather than as a convention.

The cursor is `usher.api`'s artefact and the decode stays at the edge. A port
that accepted one would have to decode it, which means knowing the sort
vocabulary of the layer above it -- and a cursor a port accepts is a cursor
that has leaked into the domain, which is the thing ADR-0034's design exists
to prevent. Repositories keep taking typed keyset values, exactly as
`RawPayloadStore.iterate` and `TitleEmbeddingRepository.list_stale` already
do with `after: uuid.UUID`.

**Half of that is already structural and this file is the other half.**
import-linter's "hexagonal layering" contract puts `usher.api` above
`usher.ports`, so a port cannot *import* the codec at all. What it cannot see
is the spelling that needs no import: a string annotation, or a parameter
called `cursor` typed `str`. Three groups add paged routes this milestone and
each will be holding a cursor when it writes its port method; the cheap
mistake is passing it straight through.

So the walk reads annotations as **text**, not as resolved objects. That is
not fastidiousness: `usher.ports.repository.sync` carries `from __future__
import annotations`, so its signatures already hand back strings while every
other module hands back types, and a walk that only understood one of those
would silently cover part of the package.
"""

import importlib
import inspect
import pkgutil
import re
from abc import ABC
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import usher.ports
from usher.api.cursor import __all__ as CODEC_NAMES
from usher.api.dto.page import Page

#: Every identifier that naming in a port signature means the cursor has
#: crossed the boundary. Derived from the codec's own `__all__` rather than
#: hand-listed, so a name added there is covered without anyone editing this
#: file -- the same argument `test_api_dto.py` makes for enumerating the DTO
#: package instead of naming models.
FORBIDDEN_TYPE_NAMES: frozenset[str] = frozenset({*CODEC_NAMES, Page.__name__})

#: The one abstract-method parameter named `cursor` in `usher.ports`, and the
#: reason it is not this test's subject. A mapping rather than a set because
#: the reason is the point: an exemption with no recorded cause is
#: indistinguishable from an oversight, which is the shape
#: `PROBLEM_EXEMPTIONS` and `NOT_REPOSITORY_PORTS` already use one layer over.
UPSTREAM_CURSORS: Mapping[str, str] = MappingProxyType(
    {
        "MetadataProvider.changed_since": (
            "ADR-0017's cursor travels the other way. It is TMDb's own page token -- minted "
            "by the provider, handed back to the provider, never rendered to an Usher client "
            "and never decoded by anything here. `usher.api.cursor`'s cursor is minted by "
            "Usher for an Usher client; the two share a noun and no direction."
        )
    }
)


def _abstract_methods() -> list[tuple[str, str, inspect.Signature]]:
    """Every abstract method under `usher.ports`, as `(port, method, sig)`.

    `walk_packages` rather than `iter_modules` for the reason
    `test_ports.py::test_every_port_abc_is_registered_in_all_ports` records at
    length: `usher.ports.repository` is a package, and the naive spelling
    finds 13 ports where the descending one finds 32 -- a scan whose subject
    silently narrowed while every guard on it stayed true.
    """
    found: list[tuple[str, str, inspect.Signature]] = []
    seen: set[type[ABC]] = set()
    for module_info in pkgutil.walk_packages(usher.ports.__path__, prefix="usher.ports."):
        namespace = importlib.import_module(module_info.name)
        for value in vars(namespace).values():
            if (
                not isinstance(value, type)
                or not issubclass(value, ABC)
                or value.__module__ != namespace.__name__
                or not getattr(value, "__abstractmethods__", None)
                or value in seen
            ):
                continue
            seen.add(value)
            for method_name in sorted(value.__abstractmethods__):
                attribute = getattr(value, method_name)
                # An abstract *property* is a `property` object, not a
                # function; `MetadataProvider.name` is one, and a walk that
                # only handled functions would skip it without saying so.
                target = attribute.fget if isinstance(attribute, property) else attribute
                if not callable(target):
                    continue
                found.append((value.__name__, method_name, inspect.signature(target)))
    return found


def _annotation_text(annotation: Any) -> str:
    """An annotation as searchable text, whichever form it arrived in.

    Both spellings are joined rather than one chosen: `__name__` is `"UUID"`
    where `str()` is `"<class 'uuid.UUID'>"`, and for a union there is no
    `__name__` at all.
    """
    if annotation is inspect.Parameter.empty:
        return ""
    if isinstance(annotation, str):
        return annotation
    return f"{getattr(annotation, '__name__', '')} {annotation}"


def _names_a_codec_type(text: str) -> list[str]:
    """Word-boundary matching, because `BulkCursor` and `ChangedPage` are not
    this codec's types and a substring test would call both of them
    violations."""
    return [name for name in sorted(FORBIDDEN_TYPE_NAMES) if re.search(rf"\b{name}\b", text)]


def test_the_walk_finds_the_ports_it_is_a_claim_about() -> None:
    """The premise for every assertion below. An empty walk -- or one that
    descended into `usher.ports` but not into `usher.ports.repository` --
    passes identically to a clean sweep, which is the failure mode this repo
    has now hit twice."""
    walked = _abstract_methods()
    assert len(walked) >= 100, f"the walk found only {len(walked)} abstract methods"
    ports = {port for port, _, _ in walked}
    assert "TitleEmbeddingRepository" in ports, "the descent into the repository package stopped"
    assert "MetadataProvider" in ports
    assert "SourceAdapter" in ports


def test_the_keyset_habit_the_cursor_replaces_is_still_there() -> None:
    """The positive control, and it is the whole reason this file is not a
    vacuous "assert nothing matches".

    `TitleEmbeddingRepository.list_stale` is the shape a paged port keeps
    having: a typed `after`, not an opaque token. If this walk ever stops
    seeing it, every "no port takes a cursor" assertion below is still green
    and no longer means anything.
    """
    signatures = {(port, method): signature for port, method, signature in _abstract_methods()}
    list_stale = signatures[("TitleEmbeddingRepository", "list_stale")]
    assert "after" in list_stale.parameters
    assert "UUID" in _annotation_text(list_stale.parameters["after"].annotation)


def test_no_port_takes_a_parameter_named_cursor() -> None:
    """The name is checked as well as the type because the cheap mistake has
    no type: `cursor: str | None = None` passed straight from a route through
    a service into a repository, which type-checks, imports nothing, and
    breaks no contract."""
    offending = [
        f"{port}.{method}({parameter})"
        for port, method, signature in _abstract_methods()
        for parameter in signature.parameters
        if (parameter == "cursor" or parameter.endswith("_cursor"))
        and f"{port}.{method}" not in UPSTREAM_CURSORS
    ]
    assert not offending, f"a port took a cursor: {offending}"


def test_no_port_names_the_codecs_types() -> None:
    """Parameters *and* the return annotation. A port that returned a `Page`
    or a `CursorSpec` has leaked the wire contract downward just as surely as
    one that accepted a cursor -- and it is the likelier half, because a
    service assembling a page is the thing that wants somewhere to put it."""
    offending: list[str] = []
    for port, method, signature in _abstract_methods():
        for name, parameter in signature.parameters.items():
            named = _names_a_codec_type(_annotation_text(parameter.annotation))
            offending += [f"{port}.{method}({name}: {one})" for one in named]
        named_return = _names_a_codec_type(_annotation_text(signature.return_annotation))
        offending += [f"{port}.{method} -> {one}" for one in named_return]
    assert not offending, f"a port named the HTTP cursor codec: {offending}"


def test_the_word_boundary_is_what_keeps_the_upstream_types_legal() -> None:
    """`BulkCursor` and `ChangedPage` are the two names a substring test would
    convict, and both are correct code: one is a bulk importer's resume token
    and the other is a provider's change feed. This is the case that would
    notice the day `_names_a_codec_type` is loosened to `in`."""
    assert not _names_a_codec_type("BulkCursor | None")
    assert not _names_a_codec_type("ChangedPage")
    assert _names_a_codec_type("CursorSpec") == ["CursorSpec"]
    assert _names_a_codec_type("Page[TitleSummary]") == ["Page"]


def test_every_exemption_names_a_real_method_that_really_takes_a_cursor() -> None:
    """The plant-is-present rule, applied to an exemption.

    An exemption for a method that no longer exists, or that no longer takes
    the parameter it was excused for, is an exemption that has quietly become
    a hole -- and it looks exactly like one that is still doing its job. So
    this asserts the thing being excused is genuinely there: without
    `UPSTREAM_CURSORS`, the case above would fail naming
    `MetadataProvider.changed_since`.
    """
    parameters = {
        f"{port}.{method}": set(signature.parameters)
        for port, method, signature in _abstract_methods()
    }
    for qualified, reason in UPSTREAM_CURSORS.items():
        assert qualified in parameters, f"{qualified} does not exist; delete its exemption"
        excused = {
            name for name in parameters[qualified] if name == "cursor" or name.endswith("_cursor")
        }
        assert excused, f"{qualified} no longer takes a cursor; delete its exemption"
        assert len(reason.split()) >= 10, f"{qualified}'s exemption does not carry a reason"
