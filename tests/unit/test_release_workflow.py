"""The tag-triggered release workflow."""

import pathlib
from typing import Any

import yaml

_WORKFLOWS = pathlib.Path(__file__).parents[2] / ".github" / "workflows"

#: PyYAML resolves a bare `on:` key to the boolean `True` (YAML 1.1's
#: "Norway problem"), so a workflow's trigger is not reachable under the
#: string `"on"`. Naming it once here beats three surprised readers.
_ON = True


def _workflow(name: str) -> dict[Any, Any]:
    loaded = yaml.safe_load((_WORKFLOWS / name).read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_the_release_workflow_triggers_only_on_a_version_tag() -> None:
    """Separate from `ci.yml` on purpose: a `tags:` entry there would run the
    whole suite on the tag *and* on the merge that preceded it."""
    release = _workflow("release.yml")

    assert release[_ON] == {"push": {"tags": ["v*"]}}

    ci = _workflow("ci.yml")
    assert "tags" not in str(ci[_ON]), "ci.yml grew a tag trigger; the two would now double up"


def test_the_release_job_can_write_a_package_and_an_attestation() -> None:
    """Three of these four are not granted by default, so omitting one fails
    at push or at attestation rather than at parse time."""
    permissions = _workflow("release.yml")["permissions"]

    assert permissions["packages"] == "write"
    assert permissions["id-token"] == "write"
    assert permissions["attestations"] == "write"


def test_no_action_is_pinned_to_a_floating_major_that_does_not_exist() -> None:
    """🔴 **`astral-sh/setup-uv`'s floating major tags stop at `v7`** while its
    releases have reached v10, so `@v8`, `@v9` and `@v10` all resolve to
    nothing. `ci.yml` pins an exact release tag, which is why it works.

    This case is offline: it pins the *rule* rather than re-querying GitHub,
    because a unit suite that needs the network is one that fails on a train.
    The floating-tag inventory was measured with
    `gh api repos/<owner>/<repo>/git/ref/tags/<tag>` on 2026-09-07.

    The control is the `uses` count -- a scan that found no action references
    would satisfy every assertion below.
    """
    uses = [
        step["uses"]
        for name in ("release.yml", "ci.yml")
        for job in _workflow(name)["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]

    assert len(uses) >= 5, f"the workflow scan found {len(uses)} action references: {uses}"

    for reference in uses:
        action, _, version = reference.partition("@")
        assert version, reference
        if action == "astral-sh/setup-uv":
            assert version.count(".") == 2, (
                f"{reference} pins a floating major; setup-uv has none above v7, "
                "so v8/v9/v10 resolve to nothing and the job dies at 'Set up job'"
            )
