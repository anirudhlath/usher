"""GitHub issue forms, and the fields that earn their place."""

import pathlib
from typing import Any

import yaml

_DIR = pathlib.Path(__file__).parents[2] / ".github" / "ISSUE_TEMPLATE"

#: `config.yml` is the chooser, not a form: it has no `body` and would fail
#: every shape assertion below. Excluded by name rather than by a `try`, so a
#: form that failed to parse cannot be mistaken for the chooser.
_CHOOSER = "config.yml"


def _forms() -> dict[str, dict[str, Any]]:
    return {
        path.name: yaml.safe_load(path.read_text())
        for path in sorted(_DIR.glob("*.yml"))
        if path.name != _CHOOSER
    }


def test_every_issue_form_parses_and_asks_for_the_version() -> None:
    """The controls are the two assertions before the loop.

    A glob that matched nothing passes exactly like a glob that passed --
    this repository's most-repeated finding -- so the count is asserted, and
    then a form is named, because a directory holding one unexpected file
    would satisfy a bare count.
    """
    forms = _forms()

    assert forms, "the template scan found nothing, so every assertion below is vacuous"
    assert "bug_report.yml" in forms, sorted(forms)

    for name, form in forms.items():
        assert {"name", "description", "body"} <= set(form), name
        body = form["body"]
        assert isinstance(body, list) and body, name
        ids = [element["id"] for element in body if "id" in element]
        assert len(ids) == len(set(ids)), f"{name} reuses a body id: {ids}"

    required_ids = {
        element["id"]
        for element in forms["bug_report.yml"]["body"]
        if element.get("validations", {}).get("required")
    }
    assert "version" in required_ids, sorted(required_ids)


def test_the_chooser_forbids_a_blank_issue_and_routes_a_vulnerability_away() -> None:
    """**A vulnerability must not arrive as a public issue**, and the chooser
    is the only thing a reporter is looking at in the moment they would file
    one. `blank_issues_enabled: false` is what stops them walking past it.
    """
    chooser = yaml.safe_load((_DIR / _CHOOSER).read_text())

    assert chooser["blank_issues_enabled"] is False
    links = chooser["contact_links"]
    assert links, "no contact links, so the assertion below is vacuous"
    assert any("security/advisories/new" in link["url"] for link in links), links
