"""The bar/secrets/redaction preamble, across every arm that measures a live source."""

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]

#: Every script whose `main` reads the operator's secrets file. A new arm that
#: builds its own preamble instead of reusing the shared one is the drift this
#: case exists to catch, so it has to be added here to be covered.
_ARMS = ("measure_source_latency", "measure_source_drift", "measure_source_lane")


def _load(name: str) -> ModuleType:
    qualified = f"scripts.{name}"
    cached = sys.modules.get(qualified)
    if cached is not None:
        return cached
    specification = importlib.util.spec_from_file_location(
        qualified, _ROOT / "scripts" / f"{name}.py"
    )
    assert specification is not None and specification.loader is not None, name
    module = importlib.util.module_from_spec(specification)
    # Registered before execution: the module defines dataclasses, and
    # `dataclasses` resolves `cls.__module__` through `sys.modules`.
    sys.modules[qualified] = module
    specification.loader.exec_module(module)
    return module


async def _explode(*_: Any, **__: Any) -> int:
    raise RuntimeError(
        "GET /Users/user-abc/Items/1 failed: connect to "
        "https://media.example.invalid refused (api_key=tok-abc, device-abc)"
    )


@pytest.mark.parametrize("arm", _ARMS)
def test_a_failed_run_prints_the_redacted_traceback_and_not_just_the_message(
    arm: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure report without the traceback cannot say *where* the run died.

    The message alone is what two of these three arms printed, and a harness
    that spends an operator's request budget and then reports a bare
    `RuntimeError: ...` has bought a failure it cannot act on. The traceback
    carries frames holding the token, so it has to go through `redact` --
    which is why the two properties are pinned in one case.

    Every secret below is a fixture value.
    """
    module = _load(arm)
    bar = tmp_path / "BAR.md"
    bar.write_text("a pre-registered bar", encoding="utf-8")
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text(
        "emby_server: https://media.example.invalid\n"
        "emby_user_id: user-abc\n"
        "emby_device_id: device-abc\n"
        "emby_token: tok-abc\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_run", _explode)
    monkeypatch.setattr(sys, "argv", ["prog", f"--secrets={secrets_file}", f"--bar={bar}"])

    main: Callable[[], int] = module.main
    code = main()
    printed = capsys.readouterr().out

    # The positive control first: a `main` that printed nothing at all
    # satisfies every absence assertion below.
    assert code == 1, f"{arm} reported {code} for a run that raised"
    assert "FAILED" in printed, f"{arm} printed no failure at all: {printed!r}"
    assert "Traceback (most recent call last)" in printed, (
        f"{arm} printed the message without the traceback: {printed!r}"
    )
    assert "_explode" in printed, (
        f"{arm}'s traceback does not reach the frame that raised: {printed!r}"
    )
    for secret in ("user-abc", "tok-abc", "device-abc", "media.example.invalid"):
        assert secret not in printed, f"{secret!r} reached the terminal from {arm}: {printed!r}"
    assert "<token>" in printed and "<user-id>" in printed, (
        f"{arm} must leave a readable placeholder; got {printed!r}"
    )


@pytest.mark.parametrize("arm", _ARMS)
def test_a_missing_bar_refuses_to_measure(
    arm: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bar's only property is that it provably predates the numbers, and a
    run that cannot show one has no way to acquire it afterwards."""
    module = _load(arm)
    secrets_file = tmp_path / "secrets.yaml"
    secrets_file.write_text(
        "emby_server: https://media.example.invalid\n"
        "emby_user_id: user-abc\n"
        "emby_device_id: device-abc\n"
        "emby_token: tok-abc\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_run", _explode)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", f"--secrets={secrets_file}", f"--bar={tmp_path / 'absent.md'}"],
    )

    main: Callable[[], int] = module.main
    with pytest.raises(SystemExit, match="does not exist"):
        main()
