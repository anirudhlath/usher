"""In-memory `MetadataProvider`, for `EnrichService` to be unit-tested
against.

**Where this is more forgiving than the real TMDb, on purpose.** Seven places.
The first three and the seventh are closed by `tests/unit/test_adapters_tmdb_*.py`,
which drive `TmdbMetadataProvider` over `httpx.MockTransport`; the fourth
through sixth are closed by nothing in this repository and are on Task 26's
live-verification list.

- **It never rate-limits, never times out, and answers instantly.** Nothing
  here exercises the token-bucket throttle, the 429 path, or
  `PortRateLimited.retry_after`. `test_adapters_tmdb_client.py` drives all
  three against a fake clock and a mock transport.
- **Its `to_result` is a hand-written stand-in, not TMDb's mapping.** It
  reads a deliberately small subset of the same keys (`title`/`name`,
  `release_date`/`first_air_date`, `genres`, `overview`, `seasons`) so the
  seeded payloads read like the real thing — which means it *looks* like it
  covers the movie/TV divergence and does not: a mapper bug in
  `usher.adapters.tmdb.mapping` is invisible from here. Only
  `test_adapters_tmdb_mapping.py` can see one.
- **A miss is `PortDataMalformed`, unconditionally.** The real provider has
  to decide that from an HTTP status, and getting it wrong (404 →
  `PortUnavailable`) costs five retries and a wrong park reason rather than
  an error. This fake cannot tell the two apart because it has no statuses.
- **Its payloads are hand-written, not shape-recorded.** They carry the keys
  this project believes TMDb sends. `tests/fixtures/tmdb/` at least records a
  shape somebody transcribed from TMDb's published documentation; this file
  does not even do that, so a payload here agreeing with the mapper proves
  only that two guesses agree.
- **`changed_since` pages a list this test seeded**, so it can never produce
  the one behaviour that matters about a real change feed: entries appearing
  *while* it is being walked. A resumable cursor over a moving feed can
  revisit or skip, and nothing here will ever show it.
- **`search` returns exactly what was seeded, in that order**, so "the
  provider's own relevance ordering" is whatever a test wrote down. It
  cannot show that TMDb's ordering puts the obvious answer first, which is
  the assumption every "pick a confident candidate" rule rests on.
- **Its `genre_vocabulary` is TMDb's set transcribed a second time, not
  read off `TMDB_GENRE_NAMES`.** So an `EnrichService` case proves the rule —
  a concept the provider cannot name survives enrichment — and proves nothing
  about whether TMDb's real vocabulary is that set. ADR-0039 and the
  `test_adapters_tmdb_provider.py` case named on the property close the other
  half.
"""

import copy
import uuid
from datetime import date
from typing import Any

from pydantic import AwareDatetime

from usher.domain.collection import Collection
from usher.domain.enums import ImageKind, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.image import Image
from usher.domain.people import Credit, CreditKind, CreditSource, Person, person_sort_name
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed, UsherPortError
from usher.ports.ingest import ProviderRef
from usher.ports.metadata import (
    ChangedPage,
    DerivationResult,
    EnrichmentResult,
    MetadataCandidate,
    MetadataProvider,
)

# Hand-written, TMDb-shaped, value-synthetic. Every value is invented, for
# the licensing reason `tests/fixtures/emby/` records: TMDb's terms forbid
# redistributing its metadata and CLAUDE.md's "ship importers, never data"
# forbids committing it.
_MOVIE_PAYLOAD: dict[str, Any] = {
    "id": 90000550,
    "title": "A Film",
    "original_title": "A Film",
    "release_date": "1988-06-17",
    "overview": "A synthetic overview for a film that does not exist.",
    "tagline": "Invented, like everything else here.",
    "runtime": 111,
    "status": "Released",
    "genres": [{"id": 18, "name": "Drama"}, {"id": 53, "name": "Thriller"}],
    "keywords": {"keywords": [{"id": 1, "name": "invented"}]},
    "original_language": "en",
    "vote_average": 8.4,
    "vote_count": 1_234,
    "popularity": 12.5,
    "credits": {"cast": [{"id": 93000001, "name": "Someone", "character": "Nobody"}]},
}

_SERIES_PAYLOAD: dict[str, Any] = {
    "id": 90001399,
    "name": "A Series",
    "original_name": "A Series",
    "first_air_date": "2004-09-22",
    "last_air_date": "2009-05-13",
    "overview": "A synthetic overview for a series that does not exist.",
    "status": "Ended",
    "genres": [{"id": 10765, "name": "Sci-Fi & Fantasy"}],
    "keywords": {"results": [{"id": 2, "name": "also invented"}]},
    "original_language": "en",
    "vote_average": 8.4,
    "vote_count": 987,
    "popularity": 31.5,
    "credits": {"cast": [{"id": 93000004, "name": "Someone Else", "character": "Nobody Else"}]},
    "seasons": [
        {
            "season_number": 1,
            "name": "Season 1",
            "overview": "The first one.",
            "air_date": "2004-09-22",
            "episode_count": 2,
            "id": 96000001,
            "episodes": [
                {"episode_number": 1, "name": "First", "air_date": "2004-09-22", "id": 97000001},
                {"episode_number": 2, "name": "Second", "air_date": "2004-09-29", "id": 97000002},
            ],
        }
    ],
}

_MOVIE_REF = ProviderRef(provider="tmdb", value="90000550", kind=TitleKind.MOVIE)
_SERIES_REF = ProviderRef(provider="tmdb", value="90001399", kind=TitleKind.SERIES)


class FakeMetadataProvider(MetadataProvider):
    """Seeded payloads in, canonical state out. No network, no clock.

    `fetches`/`searches` and `reset_calls()` are test-double affordances
    rather than port methods: "a cached payload within the ceiling is not
    refetched" is a statement about how many times the upstream was asked,
    and nothing about the answers can express it.
    """

    def __init__(self, provider_name: str = "tmdb") -> None:
        self._name = provider_name
        self._payloads: dict[ProviderRef, dict[str, Any]] = {
            _MOVIE_REF: copy.deepcopy(_MOVIE_PAYLOAD),
            _SERIES_REF: copy.deepcopy(_SERIES_PAYLOAD),
        }
        self._candidates: list[MetadataCandidate] = []
        self._changed: list[ProviderRef] = []
        self._failure: UsherPortError | None = None
        self.fetches = 0
        self.searches = 0

    # -- seams a test drives -------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def genre_vocabulary(self) -> frozenset[str]:
        """The 24 canonical concepts TMDb's 35 genre names collapse to.

        **Written out rather than read off `TMDB_GENRE_NAMES`**, and that is
        the seventh entry in this module's list of ways it is more forgiving
        than the real provider — restating it here means `EnrichService`'s
        cases prove the *rule* (a concept the vocabulary lacks survives) and
        say nothing about whether TMDb's real set is this one.
        `test_adapters_tmdb_provider.py::
        test_the_genre_vocabulary_is_every_tmdb_name_as_a_canonical_concept`
        is what pins that half, and the two together are what make issue #30's
        deletion actually fixed rather than fixed against a fixture.

        The seven canonical concepts deliberately absent — `Adult`,
        `Biography`, `Film-Noir`, `Game-Show`, `Musical`, `Short`, `Sport` —
        are IMDb's vocabulary gap and the whole subject.
        """
        return frozenset(
            {
                "Action",
                "Adventure",
                "Animation",
                "Comedy",
                "Crime",
                "Documentary",
                "Drama",
                "Family",
                "Fantasy",
                "History",
                "Horror",
                "Kids",
                "Music",
                "Mystery",
                "News",
                "Reality",
                "Romance",
                "Science Fiction",
                "Soap",
                "Talk",
                "TV Movie",
                "Thriller",
                "War",
                "Western",
            }
        )

    def seed(self, ref: ProviderRef, payload: dict[str, Any]) -> None:
        self._payloads[ref] = copy.deepcopy(payload)

    def seed_candidates(self, *candidates: MetadataCandidate) -> None:
        self._candidates = list(candidates)

    def seed_changed(self, *refs: ProviderRef) -> None:
        self._changed = list(refs)

    def fail_with(self, exc: UsherPortError) -> None:
        """Every subsequent call raises. Cleared by `recover()`."""
        self._failure = exc

    def recover(self) -> None:
        self._failure = None

    def return_partial(self, ref: ProviderRef = _MOVIE_REF) -> None:
        """Answer with a payload carrying only an id, as TMDb does for an
        entity nobody has filled in. The tier must not move for it."""
        self._payloads[ref] = {"id": int(ref.value)}

    def reset_calls(self) -> None:
        self.fetches = 0
        self.searches = 0

    # -- the port ------------------------------------------------------

    async def search(
        self, name: str, year: int | None, kind: TitleKind | None = None
    ) -> list[MetadataCandidate]:
        self._raise_if_failing()
        self.searches += 1
        return [one for one in self._candidates if kind is None or one.kind is kind]

    async def fetch(self, ref: ProviderRef) -> dict[str, Any]:
        self._raise_if_failing()
        self.fetches += 1
        payload = self._payloads.get(ref)
        if payload is None:
            # Unconditionally malformed rather than unavailable -- see the
            # module docstring. The real provider decides this from a status
            # code and the fake has none.
            raise PortDataMalformed(
                f"{self._name} has no entity for this reference", detail=ref.value
            )
        return copy.deepcopy(payload)

    def to_result(self, payload: dict[str, Any], title_id: uuid.UUID) -> EnrichmentResult:
        # Both spellings, because TMDb really does use two -- but see the
        # module docstring: agreeing with a payload this same file wrote is
        # not evidence about TMDb.
        is_series = "name" in payload or "first_air_date" in payload or "seasons" in payload
        kind = TitleKind.SERIES if is_series else TitleKind.MOVIE
        name = payload.get("name") or payload.get("title") or str(payload.get("id", "unknown"))
        released = payload.get("first_air_date") or payload.get("release_date")
        title = Title(
            id=title_id,
            kind=kind,
            tmdb_id=int(payload["id"]) if "id" in payload else None,
            name=name,
            original_name=payload.get("original_name") or payload.get("original_title"),
            sort_name=name,
            year=int(released[:4]) if released else None,
            release_date=date.fromisoformat(released) if released else None,
            overview=payload.get("overview"),
            tagline=payload.get("tagline"),
            runtime_minutes=payload.get("runtime"),
            genres=tuple(one["name"] for one in payload.get("genres", [])),
            original_language=payload.get("original_language"),
            community_rating=payload.get("vote_average"),
            vote_count=payload.get("vote_count"),
            popularity=payload.get("popularity"),
            # `enrichment_state` is deliberately not set: the tier is
            # `EnrichService`'s to raise, through `ENRICHMENT_RANK` only.
        )
        seasons: list[Season] = []
        episodes: list[Episode] = []
        for raw_season in payload.get("seasons", []):
            season = Season(
                title_id=title_id,
                season_number=raw_season["season_number"],
                name=raw_season.get("name"),
                overview=raw_season.get("overview"),
                air_date=(
                    date.fromisoformat(raw_season["air_date"])
                    if raw_season.get("air_date")
                    else None
                ),
                episode_count=raw_season.get("episode_count"),
                tmdb_id=raw_season.get("id"),
            )
            seasons.append(season)
            episodes.extend(
                Episode(
                    title_id=title_id,
                    season_id=season.id,
                    season_number=season.season_number,
                    episode_number=raw_episode["episode_number"],
                    name=raw_episode.get("name"),
                    air_date=(
                        date.fromisoformat(raw_episode["air_date"])
                        if raw_episode.get("air_date")
                        else None
                    ),
                    tmdb_id=raw_episode.get("id"),
                )
                for raw_episode in raw_season.get("episodes", [])
            )
        return EnrichmentResult(
            title=title.evolve(
                # `field -> provider` for what this payload actually carried.
                # Present because `EnrichService` *merges* provenance rather
                # than assigning it, and a fake producing an empty map would
                # let a service that dropped the stored map entirely pass.
                field_provenance={
                    field: self._name
                    for field in ("name", "overview", "genres", "tmdb_id", "release_date")
                    if getattr(title, field) not in (None, ())
                }
            ),
            seasons=tuple(seasons),
            episodes=tuple(episodes),
            payload=payload,
        )

    def to_derivation(self, payload: dict[str, Any], title_id: uuid.UUID) -> DerivationResult:
        """People, credits and a collection out of a seeded payload.

        **Deliberately a second, simpler reader than
        `usher.adapters.tmdb.mapping`, and the module docstring's warning
        applies with full force here**: agreeing with a payload this same
        file wrote is not evidence about TMDb. What this exists for is the
        service above it -- `DeriveService`'s resolution, scoping and
        ordering -- and for that a reader that is honest about `cast`, `crew`
        and `created_by` is enough.

        Three things it does model, because a service case turns on each:
        the per-kind divergence (`created_by` is top-level, not
        `credits.crew`), one `Person` per distinct provider id however many
        arrays name them, and `billing_order` read from `order` rather than
        from the array index.

        What it does **not** model: the crew job filter, the cast cutoff and
        the per-kind image cap. All three are `mapping.py`'s and all three have
        their own cases there; a fake that reimplemented them would be a second
        copy of the rule, which is the thing a fake exists not to be.

        **Images are modelled to the same depth**, because two things a service
        case turns on live here rather than in the mapper: that the top-level
        `poster_path`/`backdrop_path` pair is what carries `is_primary`, and
        that a path named by both the pair and an array is **one** row. Without
        the second, `DeriveService`'s own count would be right for the wrong
        reason -- `ImageRepository.replace_for_titles` deduplicates on the same
        key, so the fake would be measuring the repository.
        """
        people: dict[int, Person] = {}
        credits: list[Credit] = []
        block = payload.get("credits") or {}
        sources: list[tuple[list[dict[str, Any]], CreditKind, str | None]] = [
            (block.get("cast") or [], CreditKind.CAST, None),
            (block.get("crew") or [], CreditKind.CREW, None),
            (payload.get("created_by") or [], CreditKind.CREW, "Creator"),
        ]
        for entries, kind, forced_job in sources:
            for entry in entries:
                tmdb_id = entry.get("id")
                name = entry.get("name")
                if tmdb_id is None or not name:
                    continue
                if tmdb_id not in people:
                    people[tmdb_id] = Person(
                        tmdb_id=tmdb_id,
                        name=name,
                        sort_name=person_sort_name(name),
                        known_for_department=entry.get("known_for_department"),
                    )
                credits.append(
                    Credit(
                        person_id=people[tmdb_id].id,
                        title_id=title_id,
                        kind=kind,
                        source=CreditSource.TMDB,
                        tmdb_credit_id=entry.get("credit_id"),
                        character=entry.get("character"),
                        job=forced_job or entry.get("job"),
                        department=entry.get("department"),
                        billing_order=entry.get("order"),
                    )
                )
        raw_collection = payload.get("belongs_to_collection")
        collection = (
            Collection(tmdb_id=raw_collection["id"], name=raw_collection["name"])
            if isinstance(raw_collection, dict)
            and raw_collection.get("id") is not None
            and raw_collection.get("name")
            else None
        )
        return DerivationResult(
            people=tuple(people.values()),
            credits=tuple(credits),
            collection=collection,
            images=tuple(_images(payload, title_id)),
        )

    async def changed_since(self, since: AwareDatetime, cursor: str | None = None) -> ChangedPage:
        self._raise_if_failing()
        page = int(cursor or "1")
        start = (page - 1) * 2
        window = self._changed[start : start + 2]
        exhausted = start + 2 >= len(self._changed)
        return ChangedPage(refs=tuple(window), next_cursor=None if exhausted else str(page + 1))

    def _raise_if_failing(self) -> None:
        if self._failure is not None:
            raise self._failure


def _images(payload: dict[str, Any], title_id: uuid.UUID) -> list[Image]:
    """A seeded payload's artwork, keyed by path so a path named twice is one
    row.

    A module function rather than a method for the same reason the credit
    reader is inline: nothing here reads the provider's state, and a helper
    outside the class is what stops a future arm reaching for `self`.
    """
    block = payload.get("images") or {}
    by_path: dict[str, Image] = {}
    for field, kind in (
        ("posters", ImageKind.POSTER),
        ("backdrops", ImageKind.BACKDROP),
        ("logos", ImageKind.LOGO),
    ):
        for entry in block.get(field) or []:
            path = entry.get("file_path")
            if not path or path in by_path:
                continue
            by_path[path] = Image(
                title_id=title_id,
                kind=kind,
                provider="tmdb",
                provider_path=path,
                width=entry.get("width"),
                height=entry.get("height"),
                language=entry.get("iso_639_1"),
                is_primary=False,
            )
    for field, kind in (("poster_path", ImageKind.POSTER), ("backdrop_path", ImageKind.BACKDROP)):
        path = payload.get(field)
        if not path:
            continue
        standing = by_path.get(path)
        by_path[path] = (
            standing.evolve(is_primary=True)
            if standing is not None
            else Image(
                title_id=title_id,
                kind=kind,
                provider="tmdb",
                provider_path=path,
                is_primary=True,
            )
        )
    return list(by_path.values())
