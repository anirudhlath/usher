"""The `Embedder` port's contract, asserted where it is *stated* rather than
where it is implemented.

`tests/contract/embedder_contract.py` pins the behaviour every
implementation owes. What is pinned here is the docstring itself, which
sounds absurd until you notice that the clause this milestone deleted --
"callers are responsible for any query-side instruction prefix" -- was a
measurably harmful instruction that survived four milestones because nothing
read it as code. The two cases below are cheap and they fail loudly if the
deleted clause comes back in a "restore the docs" commit.
"""

import inspect

from usher.ports.embedding import Embedder


def test_the_port_does_not_ask_callers_to_apply_a_query_prefix() -> None:
    """**The clause this milestone deleted, kept deleted.**

    Measured over 210 paired observations (24 gold documents + 1,200
    distractors per draw, 5 disjoint draws, 42 queries): the documented BGE
    query prefix moves MRR by -0.0028, CI [-0.0259, +0.0203]. Applying it to
    *both* sides -- which is what one symmetric `embed` loop plus that
    instruction produces -- is -0.0663, CI [-0.1013, -0.0330]. The
    experiment's power control (a deliberately wrong prefix) moves MRR
    -0.2497 at P(>0) = 0.000, so the null is measured rather than blind.

    A caller cannot be blamed for following an instruction. The instruction
    is the defect.

    The substring is the *present-tense* form, because that is what an
    instruction looks like. The port still records that the clause existed
    -- nothing here is deleted silently -- but in reported speech, since a
    verbatim quotation is byte-for-byte the thing being guarded against.
    """
    documentation = inspect.getdoc(Embedder) or ""
    assert "callers are responsible" not in documentation.casefold()
    # ASCII hyphen only: ruff's RUF001 rejects a U+2212 MINUS SIGN literal,
    # so the typographic spelling cannot reach the port docstring either.
    assert "-0.0663" in documentation


def test_the_normalisation_contract_names_the_operator_it_is_for() -> None:
    """Verified against real pgvector: `<=>` is normalisation-*invariant*
    (a vector of norm 5 in the same direction gives the identical cosine
    distance) and `<#>` is not. PRD 05 specifies `halfvec_cosine_ops`, so
    under the shipped index normalisation buys speed, not correctness.

    A contract that says "vectors are normalised" without saying which
    operator that is load-bearing for reads as a correctness requirement the
    shipped index does not have -- and a requirement that makes no
    difference is one somebody eventually deletes, taking the `<#>` case
    with it.
    """
    documentation = inspect.getdoc(Embedder) or ""
    assert "<#>" in documentation
    assert "<=>" in documentation
