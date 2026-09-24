"""`docker compose config` over the shipped files, with `.env` seeded from `.env.example`.

The real CLI decides the merge rules and what `.env` means; nothing here starts a container.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SECRET_KEY = "0" * 64
_ENDPOINT = "http://otel-collector:4317"
_SECOND_STACK = (
    "COMPOSE_PROJECT_NAME=usher-scratch",
    "USHER_COMPOSE_NETWORK=usher-scratch_default",
    "USHER_COMPOSE_HOST_PORT=8101",
)


def _set(lines: list[str], assignment: str) -> list[str]:
    """Edit `KEY=` where the file already has it, or append it: never a second copy."""
    key = assignment.partition("=")[0]
    hits = [number for number, line in enumerate(lines) if line.startswith(f"{key}=")]
    assert len(hits) <= 1, f".env.example sets {key} {len(hits)} times"
    if not hits:
        return [*lines, assignment]
    return [assignment if number == hits[0] else line for number, line in enumerate(lines)]


def _dotenv(assignments: Sequence[str], *, above: bool) -> str:
    """`.env.example`, the secret key set in place, and `assignments` in place or pasted above."""
    lines = _set(
        (_REPO_ROOT / ".env.example").read_text().splitlines(), f"USHER_SECRET_KEY={_SECRET_KEY}"
    )
    if above:
        lines = [*assignments, *lines]
    else:
        for assignment in assignments:
            lines = _set(lines, assignment)
    return "\n".join(lines) + "\n"


def _compose_config(
    directory: Path, assignments: Sequence[str], *files: str, above: bool = False
) -> dict[str, Any]:
    """Copy both compose files into `directory`, write `.env`, and render the config.

    The subprocess gets `PATH` and `HOME` alone, so a `COMPOSE_*` or `USHER_*` in the
    calling shell cannot decide the answer.
    """
    docker = shutil.which("docker")
    assert docker, "the docker CLI is not on PATH -- tests/integration/ needs Docker"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("compose.yml", "compose.observability.yml"):
        shutil.copy(_REPO_ROOT / name, directory / name)
    (directory / ".env").write_text(_dotenv(assignments, above=above))
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


def _published(document: dict[str, Any]) -> list[str]:
    return [str(port["published"]) for port in document["services"]["usher"]["ports"]]


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
    document = _compose_config(
        tmp_path,
        [
            "COMPOSE_FILE=compose.yml:compose.observability.yml",
            f"OTEL_EXPORTER_OTLP_ENDPOINT={_ENDPOINT}",
        ],
    )

    assert set(document["services"]["usher"]["networks"]) == {"default", "observability"}
    assert _external(document) == ["observability"]
    assert document["services"]["usher"]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == _ENDPOINT


def test_a_second_stack_is_separated_by_three_env_keys(tmp_path: Path) -> None:
    """Project name, network name and host port: the three things two stacks cannot share.

    A shared project name makes `up` recreate the first stack's containers; a shared
    network puts two `postgres` aliases on it; a shared host port refuses to bind.
    """
    default = _compose_config(tmp_path / "first", [])
    second = _compose_config(tmp_path / "second", _SECOND_STACK)

    assert default["name"] == "first", "the scan did not see compose's directory-derived default"
    assert second["name"] == "usher-scratch"
    assert second["networks"]["default"]["name"] == "usher-scratch_default"
    assert _published(second) == ["8101"]


def test_a_key_pasted_above_env_example_s_own_line_is_ignored(tmp_path: Path) -> None:
    """The README's warning: `.env` sets a key twice and the later line, the example's, wins."""
    document = _compose_config(
        tmp_path, [*_SECOND_STACK, f"OTEL_EXPORTER_OTLP_ENDPOINT={_ENDPOINT}"], above=True
    )

    assert document["name"] == "usher-scratch", "a key the example lacks has nothing to lose to"
    assert document["networks"]["default"]["name"] == "usher_default"
    assert _published(document) == ["8100"]
    assert document["services"]["usher"]["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""
