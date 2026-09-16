"""The console's Configuration screen lists every setting, and only real ones."""

import re
from pathlib import Path

import pytest
from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

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

#: A row's key paired with the default it prints, lazily so the `def:` matched
#: is the one inside that row's own object literal. The `def: string` on the
#: interface declaration is never reached: this only ever scans *forward* from
#: a `key:`, and the declaration precedes every row.
_ROW_DEFAULT = re.compile(
    r"^\s*key: '([A-Z][A-Z0-9_]*)',\n(?:.*\n)*?\s*def: '([^']*)',", re.MULTILINE
)

#: What the catalogue prints for the three defaults that have no literal.
#: Spelled out here so the mapping is a decision rather than a coincidence a
#: normalising helper would hide.
_NO_DEFAULT = "required"
_NONE = "unset"
_BLANK = "empty"


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
    """A catalogued row for a variable Usher does not read fails here.

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


def _printed_default(field: FieldInfo) -> str:
    """What the catalogue's `def:` must read for one `Settings` field.

    Three spellings carry no literal and the console prints a word instead:
    a field with no default at all is `required`, a `None` default is `unset`,
    and an empty string -- including an empty `SecretStr` -- is `empty`. A
    secret's default is unwrapped rather than `str()`ed, because
    `SecretStr.__str__` is `**********` for anything non-empty and would let a
    wrong default read as correct.
    """
    default = field.default
    if default is PydanticUndefined:
        return _NO_DEFAULT
    if default is None:
        return _NONE
    if isinstance(default, SecretStr):
        default = default.get_secret_value()
    if isinstance(default, bool):
        # Before the `str()` below: `str(True)` is `'True'`, and the catalogue
        # prints the TypeScript spelling an operator would type into `.env`.
        return "true" if default else "false"
    return _BLANK if default == "" else str(default)


def test_every_catalogued_default_is_the_default_usher_actually_ships(
    catalogued: set[str],
) -> None:
    """The catalogue's `def:` must be the default `Settings` actually ships.

    It is the field on this screen an operator acts on.
    """
    paired = dict(_ROW_DEFAULT.findall(_CATALOGUE.read_text()))
    assert set(paired) == catalogued, (
        "the key/default pairing regex no longer matches one row per key -- rows without a "
        f"paired `def:`: {sorted(catalogued - set(paired))}"
    )

    prefix = str(Settings.model_config.get("env_prefix", ""))
    expected = {
        (field.alias if field.alias else f"{prefix}{name}".upper()): _printed_default(field)
        for name, field in Settings.model_fields.items()
    }
    wrong = {
        key: (printed, expected[key])
        for key, printed in sorted(paired.items())
        if key in expected and printed != expected[key]
    }
    assert not wrong, (
        "the console's Configuration screen prints a default Usher does not ship "
        f"(key: printed vs actual): {wrong}"
    )
