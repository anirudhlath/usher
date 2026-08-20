"""The console's Configuration screen lists every setting, and only real ones.

`web/src/features/operator/Config.settings.ts` is a **catalogue**: a row per
`Settings` field carrying its name, subsystem, default and what it controls.
None of that needs a request — they are properties of the software — which is
why the screen can exist at all when no route returns the running configuration.

The cost of that design is drift, and it is one-directional and silent. Adding a
field to `Settings` does not fail anything on the TypeScript side; the screen
just quietly stops listing it, and an operator reading a page headed "every
setting" is reading a page that is not. **This has already happened once**: four
console settings landed in `Settings` on 2026-08-19 and the screen went on
saying `69` in six places, every one of them wrong.

So the catalogue is pinned here rather than there. This test lives on the Python
side because that is where the fact it checks lives — `Settings.model_fields` is
the authority, and a TypeScript test asserting a number would be the same
written-down constant one language over.

Its sibling is `test_console.py::test_the_client_knows_every_root_segment_the_api_owns`,
which pins the same kind of cross-language vocabulary for routers.
"""

import re
from pathlib import Path

import pytest

from usher.config import Settings

_CATALOGUE = (
    Path(__file__).resolve().parents[2]
    / "web"
    / "src"
    / "features"
    / "operator"
    / "Config.settings.ts"
)

#: `{ key: 'USHER_DATABASE_URL',` — the only place the file spells an env var at
#: the start of a row. Anchored to the object literal so a key *mentioned* in an
#: `about` sentence is not counted as a catalogued row.
_ROW_KEY = re.compile(r"^\s*key: '([A-Z][A-Z0-9_]*)',$", re.MULTILINE)


def _environment_names() -> set[str]:
    """What the environment must be spelled as, per field.

    Two fields carry an explicit `alias` because they are read under
    OpenTelemetry's own names rather than under Usher's prefix; every other
    field is `USHER_` plus its uppercased name. Derived from the model rather
    than listed, so a third aliased field needs no change here.
    """
    prefix = str(Settings.model_config.get("env_prefix", ""))
    names: set[str] = set()
    for name, field in Settings.model_fields.items():
        names.add(field.alias if field.alias else f"{prefix}{name}".upper())
    return names


def _catalogued_names() -> set[str]:
    return set(_ROW_KEY.findall(_CATALOGUE.read_text()))


@pytest.fixture(scope="module")
def catalogued() -> set[str]:
    found = _catalogued_names()
    # The premise, asserted rather than assumed: a regex that matched nothing
    # would make every comparison below trivially pass on an empty set, which
    # is this repository's standing "a plant that did not land looks exactly
    # like a check that passed".
    assert len(found) > 50, (
        f"the row regex matched {len(found)} keys -- it has stopped matching the file"
    )
    return found


def test_every_setting_is_on_the_configuration_screen(catalogued: set[str]) -> None:
    """A field added to `Settings` and not to the catalogue fails here.

    This is the direction that goes wrong: nothing on the TypeScript side can
    notice a field it has never heard of.
    """
    missing = _environment_names() - catalogued
    where = _CATALOGUE.relative_to(Path(__file__).resolve().parents[2])
    assert not missing, (
        f"settings the console's Configuration screen does not list: {sorted(missing)} "
        f"-- add a row to {where}"
    )


def test_the_configuration_screen_invents_no_settings(catalogued: set[str]) -> None:
    """And the other direction, which is worse when it happens.

    A row for a variable Usher does not read is a screen telling an operator to
    set something that will be refused at startup -- `Settings` is
    `extra="forbid"`, so an unknown `USHER_*` key is a hard failure rather than
    a no-op.
    """
    invented = catalogued - _environment_names()
    assert not invented, f"the console lists settings Usher does not read: {sorted(invented)}"


def test_every_secret_is_marked_as_one(catalogued: set[str]) -> None:
    """`secret: true` is the whole of what the catalogue knows about a credential.

    The screen renders a secret as `•••• set` or `not set` and there is no field
    a value could come out of -- but that only holds if the *right* rows carry
    the flag. A new `SecretStr` field catalogued without it would render like
    any other setting, which is how a DSN reaches a screenshot.
    """
    assert catalogued  # the fixture's premise, restated where it is used
    source = _CATALOGUE.read_text()
    prefix = str(Settings.model_config.get("env_prefix", ""))

    secrets = {
        f"{prefix}{name}".upper()
        for name, field in Settings.model_fields.items()
        # `SecretStr` may be wrapped (`SecretStr | None`), so the annotation is
        # matched by name rather than by identity.
        if "SecretStr" in str(field.annotation)
    }
    assert secrets, "no SecretStr fields found -- this check has stopped looking at the right thing"

    for key in sorted(secrets):
        row = re.search(rf"\{{\s*key: '{re.escape(key)}',.*?\n  \}}", source, re.DOTALL)
        assert row is not None, f"{key} is a SecretStr and is not catalogued"
        assert "secret: true" in row.group(0), (
            f"{key} is a SecretStr in Settings and the console does not mark it `secret: true` -- "
            "it would render its value"
        )
