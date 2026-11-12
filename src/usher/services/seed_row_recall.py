"""The prompt that makes a model name look-alikes for a seed, and the recipe that dates it."""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from usher.domain.seed_rows import MIN_SEED_ROW_CARDS, SEED_ROW_CARDS
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import NamedTitle
from usher.services.seed_row_merge import RRF_K

NAMED_COUNT = 25
CLOSE_MIN_VOTES = 500
CLOSE_LIMIT = 100
RECALL_TEMPERATURE = 0.3

_MAX_FILMS = 30

RECALL_TEMPLATE = """{title_line}
Overview: {overview}
Keywords: {keywords}

Name {count} real films or series a viewer who loved this one would say feel most like
it: the same kind of story, protagonist, themes and tone. Do not pick titles just
because their name shares a word with it. Do not include the title itself. Give the
exact release title in the title field, without the year, and the year in the year
field."""

# No `minItems`: under guided decoding a floor forces a model with fewer good answers
# to pad the list rather than to shorten it.
RECALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["films"],
    "properties": {
        "films": {
            "type": "array",
            "maxItems": _MAX_FILMS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "year"],
                "properties": {"title": {"type": "string"}, "year": {"type": "integer"}},
            },
        }
    },
}

_TRAILING_YEAR = re.compile(r"\s*\(\d{4}\)\s*$")


def recall_prompt(
    *, name: str, year: int | None, overview: str | None, keywords: Sequence[str]
) -> str:
    """The prompt for one seed, with the year left out of the title line when unknown."""
    return RECALL_TEMPLATE.format(
        title_line=f"Title: {name}." if year is None else f"Title: {name} ({year}).",
        overview=overview or "",
        keywords=", ".join(keywords),
        count=NAMED_COUNT,
    )


def parse_recall(payload: Mapping[str, Any]) -> list[NamedTitle]:
    """The titles a model named, in its order.

    A year the model wrote into the title is dropped, an entry that is not a title and a
    year is skipped, and a repeat of a name and year keeps the first.
    """
    films = payload.get("films")
    if not isinstance(films, list):
        raise PortDataMalformed("the model's answer has no list of films", detail="films")
    named: dict[tuple[str, int], NamedTitle] = {}
    for film in films:
        if not isinstance(film, Mapping):
            continue
        title, year = film.get("title"), film.get("year")
        if not isinstance(title, str) or not isinstance(year, int) or isinstance(year, bool):
            continue
        name = _TRAILING_YEAR.sub("", title.strip()).strip()
        if name:
            named.setdefault((name.lower(), year), NamedTitle(name, year))
    return list(named.values())


def seed_row_recipe(*, model: str) -> str:
    """What a stored row was built by, as 32 hex characters.

    Any change to the prompt, the schema, a merge constant or the model makes every
    stored row due for a rebuild.
    """
    payload = json.dumps(
        {
            "template": RECALL_TEMPLATE,
            "schema": RECALL_SCHEMA,
            "temperature": RECALL_TEMPERATURE,
            "rrf_k": RRF_K,
            "row_cards": SEED_ROW_CARDS,
            "min_row_cards": MIN_SEED_ROW_CARDS,
            "close_min_votes": CLOSE_MIN_VOTES,
            "close_limit": CLOSE_LIMIT,
            "named_count": NAMED_COUNT,
            "model": model,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    # A change-detection digest, not a security primitive, as `blend_fingerprint` is.
    return hashlib.md5(payload.encode("utf-8"), usedforsecurity=False).hexdigest()


__all__ = [
    "CLOSE_LIMIT",
    "CLOSE_MIN_VOTES",
    "NAMED_COUNT",
    "RECALL_SCHEMA",
    "RECALL_TEMPERATURE",
    "RECALL_TEMPLATE",
    "parse_recall",
    "recall_prompt",
    "seed_row_recipe",
]
