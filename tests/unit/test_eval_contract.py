"""Two structural guarantees about `usher.eval` that no runtime test can see.

Both are absence claims, and an absence is exactly what rots silently: a
package that acquires an importer, and a schema that acquires a migration.
"""

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_the_eval_package_is_named_by_an_import_contract() -> None:
    """The allowlist note in `[tool.importlinter]` says a new top-level
    package must be named by some contract or it escapes all of them. This
    asserts `usher.eval` is named, so deleting the contract fails here rather
    than silently widening what may import a dev-only extra."""
    config = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    contracts = config["tool"]["importlinter"]["contracts"]
    naming = [c for c in contracts if "usher.eval" in c.get("forbidden_modules", [])]
    assert naming, "no contract forbids importing usher.eval"
    contract = naming[0]
    assert "usher.cli" not in contract["source_modules"], (
        "usher.cli is the eval package's composition root and must stay exempt"
    )
    for layer in ("usher.domain", "usher.services", "usher.api", "usher.composition"):
        assert layer in contract["source_modules"], f"{layer} may not import usher.eval"
