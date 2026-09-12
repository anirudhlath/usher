"""`.env.example`, `compose.yml` and `Settings`, checked against each other."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from usher.config import COMPOSE_ONLY_PREFIX, Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_COMPOSE = _REPO_ROOT / "compose.yml"

# Obviously synthetic, and long enough for `secret_key`'s `min_length=32`.
# `.env.example` ships the key blank, so every case that builds a real
# `Settings` has to supply one -- which is exactly what the README's second
# line tells an operator to do.
_SECRET_KEY = "0" * 64
_DATABASE_URL = "postgresql+asyncpg://usher:usher@localhost:5432/usher"

# The only variables `compose.yml` may set through `environment:`, each because the
# compose *topology* owns it rather than the operator: USHER_DATABASE_URL the service's
# hostname on the compose network.
_TOPOLOGY_OWNED = frozenset(
    {
        "USHER_DATABASE_URL",
        "USHER_HOST",
        "USHER_PORT",
        "USHER_SECRET_KEY",
        "USHER_IMAGE_CACHE_DIR",
        "USHER_BULK_DATA_DIR",
    }
)


def _env_file(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".env"
    path.write_text(body)
    return path


def _env_example_entries() -> dict[str, str]:
    """`.env.example` as compose's own dotenv parser reads it: `KEY=value`
    lines, full-line `#` comments skipped, the value taken verbatim."""
    entries: dict[str, str] = {}
    for line in _ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        entries[key.strip()] = value
    return entries


def _settings_variables() -> set[str]:
    """The environment variable name behind every `Settings` field.

    Read off `model_fields` rather than transcribed, so a field added later
    is covered without anyone remembering to add it here. The two OTel
    fields carry an explicit `alias` and are therefore *not* `USHER_`-
    prefixed; that asymmetry is the whole reason this is computed.
    """
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        alias = field.alias
        names.add(alias if isinstance(alias, str) else f"USHER_{name}".upper())
    return names


def _compose_document() -> dict[str, Any]:
    loaded = yaml.safe_load(_COMPOSE.read_text())
    assert isinstance(loaded, dict), "compose.yml did not parse as a mapping"
    return loaded


def _usher_service() -> dict[str, Any]:
    services = _compose_document()["services"]
    assert "usher" in services, f"compose.yml has no `usher` service: {sorted(services)}"
    service: dict[str, Any] = services["usher"]
    return service


def _compose_env_files() -> list[str]:
    """The paths under the `usher` service's `env_file:`, in either the short
    form (a bare string) or the long one (`{path, required}`)."""
    declared = _usher_service().get("env_file", [])
    entries = [declared] if isinstance(declared, str) else declared
    return [entry if isinstance(entry, str) else str(entry["path"]) for entry in entries]


def _compose_substitutions() -> set[str]:
    """Every `${VAR}` in the whole file, not just the ones under a key this
    test knows to look at -- a compose variable added to a `volumes:` or an
    `image:` line is the same hazard as one added to `ports:`."""
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", _COMPOSE.read_text()))


# -- finding 1: `cp .env.example .env` is the documented first step ---------


def test_the_readmes_first_step_produces_working_settings(tmp_path: Path) -> None:
    """`cp .env.example .env` and fill in the secret key -- verbatim from
    `README.md` -- and every entry point must still start.

    Before `USHER_COMPOSE_` existed this raised
    `ValidationError: usher_host_port -- Extra inputs are not permitted`, out
    of `uv run pytest` (461 errors), `usher bootstrap-status` and
    `usher push --probe` alike.
    """
    body = _ENV_EXAMPLE.read_text().replace(
        "USHER_SECRET_KEY=\n", f"USHER_SECRET_KEY={_SECRET_KEY}\n"
    )
    assert _SECRET_KEY in body, "`.env.example` no longer ships a blank USHER_SECRET_KEY line"

    settings = Settings(_env_file=str(_env_file(tmp_path, body)))

    assert settings.log_level == "INFO"
    assert settings.worker_enabled is True


def test_a_compose_only_variable_does_not_break_the_application(tmp_path: Path) -> None:
    """The property, stated over a name nothing in this repository uses.

    Pinning `USHER_COMPOSE_HOST_PORT` alone would pass against a fix that
    special-cased today's one key, which is the fix that lets the next
    compose variable reintroduce the outage.
    """
    body = (
        f"USHER_DATABASE_URL={_DATABASE_URL}\n"
        f"USHER_SECRET_KEY={_SECRET_KEY}\n"
        "USHER_COMPOSE_SOMETHING_NOBODY_HAS_INVENTED_YET=whatever\n"
    )

    settings = Settings(_env_file=str(_env_file(tmp_path, body)))

    assert settings.port == 8000


def test_a_misspelled_setting_is_still_refused(tmp_path: Path) -> None:
    """The other half, and the reason the fix is a reserved namespace rather
    than `extra="ignore"`.

    `extra="forbid"` is what turns `USHER_LOG_LEVL=DEBUG` into a startup
    failure instead of a line in `.env` that silently does nothing -- the
    same "dead config that looks like a control" shape as finding 2, one
    layer down. A fix that dropped every unknown key would pass the case
    above and lose this.
    """
    body = (
        f"USHER_DATABASE_URL={_DATABASE_URL}\n"
        f"USHER_SECRET_KEY={_SECRET_KEY}\n"
        "USHER_LOG_LEVL=DEBUG\n"
    )

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=str(_env_file(tmp_path, body)))

    assert "usher_log_levl" in str(caught.value)


def test_no_setting_hides_inside_the_reserved_namespace() -> None:
    """A field named `compose_*` would be dropped before validation and would
    then read as a setting that validates and influences nothing."""
    offenders = sorted(
        name for name in _settings_variables() if name.startswith(COMPOSE_ONLY_PREFIX)
    )
    assert offenders == [], (
        f"{offenders} sit inside the namespace `Settings` deliberately ignores; "
        "rename them or the deployment silently loses them"
    )


def test_every_usher_variable_in_env_example_is_a_setting_or_compose_reserved() -> None:
    """The guard that fails if a future compose variable is added to
    `.env.example` in the application's own namespace."""
    known = _settings_variables()
    offenders = sorted(
        key
        for key in _env_example_entries()
        if key.startswith("USHER_") and key not in known and not key.startswith(COMPOSE_ONLY_PREFIX)
    )
    assert offenders == [], (
        f"{offenders} are in `.env.example` but are not `Settings` fields, so "
        f"`cp .env.example .env` fails validation. Name a compose-only variable "
        f"`{COMPOSE_ONLY_PREFIX}*`."
    )


def test_every_variable_compose_substitutes_is_a_setting_or_compose_reserved() -> None:
    """The same guard from `compose.yml`'s side, over the whole file.

    `.env.example` and `compose.yml` are edited independently -- the M1
    commit that introduced `USHER_HOST_PORT` touched both -- so checking one
    of them would leave the other free to reintroduce the failure.
    """
    known = _settings_variables()
    substituted = _compose_substitutions()
    assert substituted, "no `${...}` substitution found in compose.yml -- did the parse break?"
    offenders = sorted(
        name
        for name in substituted
        if name.startswith("USHER_")
        and name not in known
        and not name.startswith(COMPOSE_ONLY_PREFIX)
    )
    assert offenders == [], (
        f"{offenders} are substituted by compose.yml but are not `Settings` fields. "
        f"An operator who sets one in `.env` -- which is where compose reads them "
        f"from -- gets a `ValidationError` from every entry point. Name it "
        f"`{COMPOSE_ONLY_PREFIX}*`."
    )


# -- finding 2: a documented setting has to reach the container ------------


def test_env_example_documents_every_setting() -> None:
    """Both directions, because both failures are silent.

    A setting missing from `.env.example` is one an operator cannot discover
    and -- now that `env_file:` is what delivers them -- one they cannot set
    without knowing it exists. A key in `.env.example` that is not a setting
    is finding 1 again.
    """
    documented = {key for key in _env_example_entries() if not key.startswith(COMPOSE_ONLY_PREFIX)}
    assert documented == _settings_variables()


def test_env_example_ships_the_defaults(tmp_path: Path) -> None:
    """A copied `.env.example` must not change how the deployment behaves.

    Every value in it is meant to be the field's own default, so the file is
    a *reference* an operator edits rather than a second set of defaults that
    drifts from `config.py`. Without this, changing a default in `config.py`
    silently leaves every deployment that copied the example on the old one.

    The three `SecretStr` fields are excluded rather than compared: two of
    them have no default to compare against, and a failing `assert` here
    renders both sides. `SecretStr.__repr__` masks the value, but the rule in
    CLAUDE.md is that a secret never reaches a failure diff at all, and the
    cheapest way to keep it is not to put one there.
    """
    secrets = {"database_url", "secret_key", "tmdb_api_key"}
    body = _ENV_EXAMPLE.read_text().replace(
        "USHER_SECRET_KEY=\n", f"USHER_SECRET_KEY={_SECRET_KEY}\n"
    )
    from_example = Settings(_env_file=str(_env_file(tmp_path, body)))
    minimal = Settings(
        _env_file=str(
            _env_file(
                tmp_path / "minimal",
                f"USHER_DATABASE_URL={_DATABASE_URL}\nUSHER_SECRET_KEY={_SECRET_KEY}\n",
            )
        )
    )

    assert from_example.model_dump(exclude=secrets) == minimal.model_dump(exclude=secrets)


def test_the_container_is_given_the_env_file_whole() -> None:
    """`env_file:`, not a hand-maintained `environment:` list.

    The two are different mechanisms: `environment:` names one variable at a
    time and compose substitutes each from `.env`; `env_file:` hands the file
    to the container. The first is why 24 of 30 documented settings were
    unreachable -- every one of them needed a line somebody had to remember
    to write, and twelve of the missing were M5's own.
    """
    assert _compose_env_files() == [".env"]


def test_compose_overrides_only_what_the_topology_owns() -> None:
    """`environment:` wins over `env_file:`, so anything left in it is a
    setting an operator cannot change from `.env`.

    Keeping that list to the six the compose topology genuinely owns is what
    stops `environment:` quietly becoming the dead-config list again -- each
    of the six is named in `_TOPOLOGY_OWNED` above with the reason it is
    not the operator's.
    """
    declared = set(_usher_service().get("environment", {}))
    assert declared == set(_TOPOLOGY_OWNED)


# A relative `Path` default that the image ships at the same place under `/app`, so the
# container agrees with a dev shell and no override is wanted.
_SHIPPED_IN_THE_IMAGE = frozenset({"console_dist_dir"})


def test_a_relative_path_setting_is_overridden_for_the_container() -> None:
    """A relative default is right for a dev shell and resolves against the
    container's `WORKDIR` of `/app` -- so every one of them is either
    overridden here or shipped there, and a new one is a decision rather than
    a silent repeat.

    This is the check that did not exist when `bulk_data_dir` was added.
    `image_cache_dir` got its override in M9 and `bulk_data_dir` did not, and
    nothing related the two: the compose scan reads `${...}` substitutions and
    neither is written that way, so a second relative writable path was
    invisible for five milestones. It surfaced as `PermissionError(13)` from
    `adapters/bulk/download.py` on the first bootstrap the *container* ever
    ran -- every earlier one was a dev shell, where the path is correct.

    Derived from `Settings.model_fields` rather than from a list, so the
    seventh path setting fails here until somebody classifies it.
    """
    environment = _usher_service().get("environment", {})

    relative = {
        name: field.default
        for name, field in Settings.model_fields.items()
        if isinstance(field.default, Path) and not field.default.is_absolute()
    }
    assert relative, "the scan found no relative Path setting, so it is reading the wrong thing"
    assert "image_cache_dir" in relative, "the scan ran but missed a known relative path setting"

    for name, default in sorted(relative.items()):
        if name in _SHIPPED_IN_THE_IMAGE:
            continue
        variable = f"USHER_{name.upper()}"
        assert variable in environment, (
            f"{variable} defaults to the relative {default!r}, which inside the container "
            f"resolves against WORKDIR /app -- a root-owned directory uid 1000 cannot write. "
            f"Give it an absolute path in compose's `environment:` and a `volumes:` entry, "
            f"or add it to _SHIPPED_IN_THE_IMAGE with the reason it needs neither."
        )
        assert Path(environment[variable]).is_absolute(), (
            f"{variable} is overridden as {environment[variable]!r}, which is still relative"
        )


def test_the_worker_switch_reaches_the_container() -> None:
    """The setting the finding is really about.

    `USHER_WORKER_ENABLED` is documented in `README.md` and `.env.example`
    and it works when delivered directly -- `/health/ready` reports
    `"worker": false` and the lane stops. Setting it in `.env` did nothing,
    so an operator following the README leaves `worker: true` and then starts
    `usher work` in a second container: the double-worker state where
    `JobWorker.startup()` requeued everything `running` and each stole the
    other's live claims. *(M9's W1 closed that consequence -- recovery is a
    lease now -- and the setting still matters, because two workers spend
    `USHER_JOB_CONCURRENCY` and `USHER_TMDB_REQUESTS_PER_SECOND` twice against
    limits that are per process.)*
    """
    assert "USHER_WORKER_ENABLED" not in _usher_service().get("environment", {})
    assert "USHER_WORKER_ENABLED" in _env_example_entries()
    assert _compose_env_files() == [".env"]
