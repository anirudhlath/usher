"""A rule about the whole `api/dto/` package, not about one model.

PRD 08: "Credentials are never returned by any API, including admin.
Write-only." The admin routes satisfy that today because `SourceResponse`
has no field to put one in -- but "today" is the weak part. This module
turns it into a property every present and future response DTO has to keep:
it enumerates the package rather than naming models, so a response type
added in M5 or M9 is covered the moment it is written, without anyone
remembering this file exists.

Two independent checks, because they fail differently. A field *named*
`password` is the obvious mistake; a field *typed* `SecretStr` is the
subtle one -- it renders as `**********` in a log line and serializes to
the real value in a response body, so it is precisely the shape that looks
safe while shipping the secret.
"""

import importlib
import pkgutil
from typing import get_args

from pydantic import BaseModel, SecretStr

import usher.api.dto

# `credentials_ref` is not a secret, but it is the address of one, and
# `usher.services.sources` sizes it as unguessable for that reason -- a
# client that learns a ref has learned a pointer it was never handed.
_FORBIDDEN_NAMES = {"password", "username", "credential", "credentials", "credentials_ref", "token"}


def _response_models() -> list[type[BaseModel]]:
    models: list[type[BaseModel]] = []
    for module_info in pkgutil.iter_modules(usher.api.dto.__path__):
        module = importlib.import_module(f"{usher.api.dto.__name__}.{module_info.name}")
        for name in dir(module):
            candidate = getattr(module, name)
            if (
                isinstance(candidate, type)
                and issubclass(candidate, BaseModel)
                and candidate.__module__ == module.__name__
                and name.endswith("Response")
            ):
                models.append(candidate)
    return models


def test_the_package_actually_has_response_models() -> None:
    """Positive control. Without it, a broken discovery walk (a renamed
    package, a changed suffix convention) would make every assertion below
    vacuously true -- the failure mode of every "assert nothing matches"
    test."""
    names = {model.__name__ for model in _response_models()}
    assert {"LivenessResponse", "ReadinessResponse", "SourceResponse"} <= names


def test_no_response_dto_declares_a_credential_field() -> None:
    for model in _response_models():
        offending = sorted(set(model.model_fields) & _FORBIDDEN_NAMES)
        assert not offending, f"{model.__name__} declares {offending}"


def test_no_response_dto_declares_a_secret_typed_field() -> None:
    """A `SecretStr` on a *request* model is right (it is what keeps a
    parsed credential out of a log line); on a response model it is a
    credential on the wire that merely looks redacted in a traceback."""
    for model in _response_models():
        for field_name, field in model.model_fields.items():
            annotations = (field.annotation, *get_args(field.annotation))
            assert SecretStr not in annotations, f"{model.__name__}.{field_name} is a SecretStr"
