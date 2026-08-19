"""The harness's refusals, and the verdicts that are not failures."""

import pytest

from usher.eval.errors import EvalDependencyMissing, EvalRefused


def test_a_missing_extra_names_the_command_that_installs_it() -> None:
    """A bare ImportError tells an operator a module is absent. It does not
    tell them the module is optional, which extra carries it, or what to
    type. The message is the whole point of this class existing."""
    problem = EvalDependencyMissing("ranx")
    assert "uv sync --extra eval" in str(problem)
    assert "ranx" in str(problem)


def test_a_refusal_is_not_a_score() -> None:
    """`EvalRefused` is raised where a plausible number would be produced
    over the wrong population -- a drifted sampling frame, an empty catalog.
    It is a distinct type so no caller can catch a scoring error and a
    'this measurement is void' with one clause."""
    with pytest.raises(EvalRefused, match="sampling frame"):
        raise EvalRefused("the sampling frame does not reproduce the gate's")
