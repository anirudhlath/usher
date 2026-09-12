"""The `Embedder` port's shape, asserted against the signature rather than
against the docstring that used to state it."""

import inspect
from collections.abc import Sequence

from usher.ports.embedding import Embedder


def test_the_port_does_not_ask_callers_to_apply_a_query_prefix() -> None:
    """`embed` is symmetric: one batch of plain strings, and no query/document
    distinction for a caller to get wrong.

    The instruction this case was written against lived in prose -- "callers
    are responsible for any query-side instruction prefix" -- and prose cannot
    make a caller apply one. A prefix a caller is responsible for needs
    somewhere to be applied: a parameter, or a second embed. Neither exists,
    and that is what is asserted, because a guard on the sentence is satisfied
    by moving the sentence.
    """
    signature = inspect.signature(Embedder.embed)
    assert list(signature.parameters) == ["self", "texts"], (
        "`embed` grew a parameter, and a query-side prefix flag is exactly the "
        f"shape that parameter takes: {list(signature.parameters)}"
    )
    assert signature.parameters["texts"].annotation == Sequence[str], (
        "`texts` is no longer a plain sequence of strings, so a caller now has "
        "somewhere to say which side of the pair it is embedding"
    )

    surface = {name for name in vars(Embedder) if not name.startswith("_")}
    assert surface == {"model_name", "dimension", "embed", "aclose"}, (
        "the port's surface moved; an asymmetric embedder is spelled as a "
        f"second embed method, which is the shape to refuse: {sorted(surface)}"
    )
