"""`docker compose config` over the shipped files: what compose itself merges and substitutes.

`tests/unit/test_deployment_config.py` reads the YAML, which cannot see compose's own
merge rules (a service listing any network loses the implicit `default`) or which
variables it reads from `.env` (`COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`). These run the
real CLI in a scratch directory, so nothing here creates a container or a network.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SECRET_KEY = "0" * 64


def _compose_config(directory: Path, env_lines: list[str], *files: str) -> dict[str, Any]:
    """Copy both compose files into `directory`, write `.env`, and render the config.

    The subprocess gets `PATH` and `HOME` alone, so a `COMPOSE_*` or `USHER_*` in the
    calling shell cannot decide the answer.
    """
    docker = shutil.which("docker")
    assert docker, "the docker CLI is not on PATH -- tests/integration/ needs Docker"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("compose.yml", "compose.observability.yml"):
        shutil.copy(_REPO_ROOT / name, directory / name)
    (directory / ".env").write_text(
        "\n".join([f"USHER_SECRET_KEY={_SECRET_KEY}", *env_lines]) + "\n"
    )
    flags = [arg for name in files for arg in ("-f", name)]
    environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    # S603: a fixed argv from `shutil.which` and literals.
    rendered = subprocess.run(  # noqa: S603
        [docker, "compose", *flags, "config", "--format", "json"],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, f"`docker compose config` failed: {rendered.stderr.strip()}"
    document: dict[str, Any] = json.loads(rendered.stdout)
    return document


def _external(document: dict[str, Any]) -> list[str]:
    return sorted(
        name for name, spec in document.get("networks", {}).items() if spec.get("external")
    )


def test_the_default_config_needs_no_network_it_did_not_create(tmp_path: Path) -> None:
    document = _compose_config(tmp_path, [])

    assert _external(document) == []
    assert set(document["services"]["usher"]["networks"]) == {"default"}
    assert document["networks"]["default"]["name"] == "usher_default"


def test_the_observability_override_joins_both_networks(tmp_path: Path) -> None:
    document = _compose_config(tmp_path, [], "compose.yml", "compose.observability.yml")

    assert set(document["services"]["usher"]["networks"]) == {"default", "observability"}
    assert _external(document) == ["observability"]
    assert document["networks"]["observability"]["name"] == "observability"


def test_compose_file_in_env_is_the_same_opt_in(tmp_path: Path) -> None:
    """The documented spelling that no later command can forget, unlike `-f`."""
    document = _compose_config(tmp_path, ["COMPOSE_FILE=compose.yml:compose.observability.yml"])

    assert set(document["services"]["usher"]["networks"]) == {"default", "observability"}
    assert _external(document) == ["observability"]


def test_a_second_stack_is_separated_by_three_env_lines(tmp_path: Path) -> None:
    """Project name, network name and host port: the three things two stacks cannot share.

    A shared project name makes `up` recreate the first stack's containers; a shared
    network puts two `postgres` aliases on it; a shared host port refuses to bind.
    """
    default = _compose_config(tmp_path / "first", [])
    second = _compose_config(
        tmp_path / "second",
        [
            "COMPOSE_PROJECT_NAME=usher-scratch",
            "USHER_COMPOSE_NETWORK=usher-scratch_default",
            "USHER_COMPOSE_HOST_PORT=8101",
        ],
    )

    assert default["name"] == "first", "the scan did not see compose's directory-derived default"
    assert second["name"] == "usher-scratch"
    assert second["networks"]["default"]["name"] == "usher-scratch_default"
    assert [port["published"] for port in second["services"]["usher"]["ports"]] == ["8101"]
