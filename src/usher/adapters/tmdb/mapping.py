"""TMDb payloads -> canonical state. Pure functions, no client, no clock.

Mirrors `usher.adapters.emby.mapping`: the wire format stops here, and
nothing above this module reads a provider's key.

**TMDb keys movies and series in two different id spaces *and* two different
vocabularies, and the second half is what this module exists for.** The same
concept has a different field name depending on which space an entity lives
in, and the divergence is not cosmetic — a mapper that handled one spelling
produces silently empty data for the other half of a catalog rather than an
error:

| Concept | Movie | Series |
|---|---|---|
| name | `title` | `name` |
| original name | `original_title` | `original_name` |
| first release | `release_date` | `first_air_date` |
| runtime | `runtime` (minutes) | `episode_run_time` (array) |
| keywords | `keywords.keywords` | `keywords.results` |
| certification | `release_dates.…[].certification` | `content_ratings.…[].rating` |
| IMDb id | top-level `imdb_id` | `external_ids.imdb_id` |
| append namespace | `release_dates` | `content_ratings` |
| collection | `belongs_to_collection` (object or `null`) | **key absent entirely** |
| creator | *(none — no such concept)* | **top-level `created_by[]`, not `credits.crew`** |

The last two rows were added by M7's derivation and were **read out of the
recorded fixtures rather than assumed**: `movie.json` has
`belongs_to_collection` and no `created_by`; `series.json` has `created_by`
and **no `belongs_to_collection` key at all**, and its `credits.crew` is `[]`
while its `created_by` holds the creator. So a mapper that read creators out
of `credits.crew` — the obvious place, because that is where a *movie's*
director lives — returns nothing for every series in the catalog, silently,
which is exactly what this table exists to prevent.

The first eight rows were read from TMDb's published reference on 2026-07-31 (see
`tests/fixtures/tmdb/README.md` for the endpoint list), and **every one was
then confirmed against the live API on 2026-08-01** over 29 movie and 30
series detail responses: each movie carried `title`/`release_date`/`runtime`/
`keywords.keywords`/a top-level `imdb_id` and none carried `name`,
`first_air_date`, `episode_run_time` or `title`'s TV counterparts; each
series carried the mirror set and none carried a top-level `imdb_id` at all.

One row is a trap the survey found rather than confirmed: **`episode_run_time`
is an empty array on 26 of those 30 series** (86.7%), so `_runtime` correctly
returns `None` for the great majority of television and `Title.runtime_minutes`
is simply not a fact TMDb has about a series any more. A test that asserts a
series runtime is asserting about the 13% case.

**Nothing TMDb can put in a payload may raise.** `Title` pattern-validates
`imdb_id`, bounds `community_rating` to 0-10 and `year`/`runtime_minutes`/
`vote_count`/`popularity` to non-negative, and a `pydantic.ValidationError`
is **not** a `UsherPortError` — so a single odd value would escape
`EnrichService`'s except clause and crash the worker instead of parking the
job. Every value is filtered to the shape the model accepts *before* the
constructor, exactly as `usher.services.matching._usable_ids` does one stage
earlier. The two failures that are *not* filtered — no `id`, and no usable
name — are `PortDataMalformed`, because a canonical title cannot be built
from either.
"""

import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from usher.domain.collection import Collection
from usher.domain.enums import ImageKind, ProductionStatus, TitleKind
from usher.domain.episode import Episode, Season
from usher.domain.image import Image
from usher.domain.people import Credit, CreditKind, CreditSource, Person, person_sort_name
from usher.domain.title import Title
from usher.ports.errors import PortDataMalformed
from usher.ports.metadata import MetadataCandidate

# `Title.imdb_id`'s own pattern, restated for the same reason
# `usher.services.matching` restates it: importing a private pydantic field
# constraint would couple this to how that model happens to be spelled.
_IMDB_ID = re.compile(r"^tt\d{7,8}$")

# TMDb's two status vocabularies, lowercased. Movies draw from the first six
# and series from the last four; nothing here enforces the pairing, matching
# `ProductionStatus`'s own docstring. An undocumented value maps to `None`
# rather than raising -- a status Usher does not recognise is not a reason to
# park an enrichment job.
_STATUS: dict[str, ProductionStatus] = {
    "released": ProductionStatus.RELEASED,
    "in production": ProductionStatus.IN_PRODUCTION,
    "post production": ProductionStatus.POST_PRODUCTION,
    "planned": ProductionStatus.PLANNED,
    "canceled": ProductionStatus.CANCELED,
    # TMDb spells it with one `l`; the two-`l` spelling is here because it
    # costs nothing and a status nobody maps is a field silently lost.
    "cancelled": ProductionStatus.CANCELED,
    "rumored": ProductionStatus.RUMORED,
    "ended": ProductionStatus.ENDED,
    "returning series": ProductionStatus.RETURNING,
    "pilot": ProductionStatus.PILOT,
}

# The statuses that make `last_air_date` an *end* year rather than "the most
# recent episode so far". A returning series has a `last_air_date` too, and
# rendering it as an end year shows "2011-2026" for a show still on the air.
_FINISHED = (ProductionStatus.ENDED, ProductionStatus.CANCELED)

# TMDb's whole genre vocabulary — `/genre/movie/list` (19) and
# `/genre/tv/list` (16), transcribed rather than fetched. **A list of names,
# not of ids**, because `title_from_payload` reads `genres[].name` and the
# question this answers is about the *words* TMDb has.
#
# Transcribed and not fetched for the reason `_STATUS` is: two extra HTTP
# calls per process to learn a list that has not moved in a decade, on a path
# whose whole cost model is request budget. All seven of the television-only
# names are in this catalog (`Sci-Fi & Fantasy` 165, `Action & Adventure` 154,
# `Reality` 57, `War & Politics` 25, `Kids` 19, `Soap` 19, `Talk` 4, measured
# 2026-08-19), so this is not a vocabulary the adapter maps away — it reaches
# `titles.genres` verbatim.
#
# **It is not the vocabulary `EnrichService` asks about.** That is
# `genre_vocabulary`, which is this list run through
# `usher.domain.genres.canonicalise_genres` — 35 TMDb names collapse to 24
# canonical concepts, and the 7 canonical concepts *not* in that set
# (`Adult`, `Biography`, `Film-Noir`, `Game-Show`, `Musical`, `Short`,
# `Sport`) are the ones enrichment must stop deleting. A genre TMDb mints
# after this was written is simply stored: it is outside `CANONICAL_GENRES`,
# so it is its own concept and nothing here has an opinion about it.
TMDB_GENRE_NAMES: frozenset[str] = frozenset(
    {
        # /genre/movie/list
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
        "Music",
        "Mystery",
        "Romance",
        "Science Fiction",
        "TV Movie",
        "Thriller",
        "War",
        "Western",
        # /genre/tv/list — the seven that are not also movie genres
        "Action & Adventure",
        "Kids",
        "News",
        "Reality",
        "Sci-Fi & Fantasy",
        "Soap",
        "Talk",
        "War & Politics",
    }
)

# Which crew jobs become `credits` rows. **The filter is the load-bearing
# half of the derivation's bound.** Unfiltered crew is every gaffer, best boy
# and assistant art director, and both consumers of that table --
# `PeopleProvider`'s "more from this director" and the search document's
# weight class B -- want the people a viewer could name. Below the line,
# crews repeat because studios repeat, so an unfiltered set makes
# "recurring" mean "worked at the same studio".
#
# A job absent from this set maps to nothing rather than raising, exactly as
# `_STATUS` treats a status TMDb invents. `Creator` is here because
# `created_by[]` entries carry no `job` at all and this module supplies one;
# it is not a value TMDb ever sends in `credits.crew`.
CREDITED_JOBS: frozenset[str] = frozenset(
    {"Director", "Writer", "Screenplay", "Story", "Novel", "Creator"}
)

# The `job` written for a `created_by[]` entry, which carries none.
_CREATOR_JOB = "Creator"
# `created_by[]` carries no `department` either; "Writing" is TMDb's own
# department for the Creator job where it does appear in `credits.crew`.
_CREATOR_DEPARTMENT = "Writing"

# The cast bound, on `order` rather than on array position -- see
# `_cast_credits`. A large film's `credits.cast` runs into the low hundreds;
# at the enriched tier boundary call 4 targets (2k-10k titles) an unbounded
# cast is roughly 10k x 150 ~ 1.5M credit rows against a database PRD 08
# budgets at 8-12 GB *total*, and at 50 it is ~500k.
#
# **50 is chosen, not measured**, and it is labelled that way for the reason
# `services/search.py` labels `_POPULARITY_MIDPOINT`: the consequence of a
# wrong cutoff is bounded, because it drops the 51st-billed actor from a
# filmography and changes nothing else. A live run over a real enriched tier
# is what would turn it into a number.
_CAST_LIMIT = 50

# Which `images` array maps to which `ImageKind`. **The array a path was found
# in is the only thing that says what it is** -- an entry carries
# `file_path`, `width`, `height`, `iso_639_1` and vote counts, and no field
# naming its kind -- so a mapper that read one array and labelled everything
# `poster` would paint a 16:9 backdrop into a 2:3 slot with nothing reporting
# an error. Three of `ImageKind`'s five: `still` hangs off an episode and
# `profile` off a person, and M9 writes neither (group C's boundary call).
_IMAGE_ARRAYS: tuple[tuple[str, ImageKind], ...] = (
    ("posters", ImageKind.POSTER),
    ("backdrops", ImageKind.BACKDROP),
    ("logos", ImageKind.LOGO),
)

# The two top-level paths, which are TMDb's *own* pick and the only primary
# signal a detail payload carries. `images.posters[]` is a vote-ordered list
# with no flag on it, so without these two keys nothing in a payload says
# which poster a card should render.
#
# There is no top-level logo path, which is why `logo` never gets a primary
# from here -- `ImageRepository.primary_for_titles` falls back to the first in
# read order for exactly that shape.
_PRIMARY_PATHS: tuple[tuple[str, ImageKind], ...] = (
    ("poster_path", ImageKind.POSTER),
    ("backdrop_path", ImageKind.BACKDROP),
)

# How many entries of each kind become `images` rows, applied to the arrays
# before the top-level pair is folded in -- so TMDb's own primary is never the
# row the cap drops, however far down its array it sits.
#
# **Ten is chosen, not measured**, on the bargain `services/search.py` states
# for `_POPULARITY_MIDPOINT` and `_CAST_LIMIT` restates one array over. A
# popular film's `posters[]` runs to hundreds and is dominated by
# language variants of one artwork -- the same image with a different title
# burned in -- which is a distinction no consumer in M9 draws: `RowCard.artwork`
# renders one poster and `GET /titles/{id}` renders a list nobody paginates. The
# consequence of a wrong cutoff is bounded and one-directional, because the
# primary is folded in afterwards: too low drops language variants a client
# cannot ask for anyway, and too high writes rows nothing reads.
#
# What would move it is a consumer that *chooses* by language -- a household
# locale reaching the proxy -- not an argument. At that point the cap becomes
# per (kind, language) and this constant is the wrong shape rather than the
# wrong number.
_IMAGES_PER_KIND_LIMIT = 10


def kind_of_payload(payload: Mapping[str, Any]) -> TitleKind:
    """Which of TMDb's two id spaces this payload came from.

    Inferred rather than passed in, because `MetadataProvider.to_result` is a
    pure function of a payload that may have come out of `raw_payloads`
    months after the fetch that produced it, with no ref alongside it.

    **Exactly one of `title`/`name` must be present.** Neither is a payload
    that is not an entity; both is ambiguous, and guessing picks between two
    id spaces that overlap on 26,968 measured ids (ADR-0011) — which is a
    series' metadata written onto a film, silently.
    """
    has_title = "title" in payload
    has_name = "name" in payload
    if has_title and has_name:
        raise PortDataMalformed(
            "TMDb payload carries both `title` and `name`, so its id space is ambiguous",
            detail=str(payload.get("id", "<no id>")),
        )
    if not has_title and not has_name:
        raise PortDataMalformed(
            "TMDb payload carries neither `title` nor `name`",
            detail=str(payload.get("id", "<no id>")),
        )
    return TitleKind.MOVIE if has_title else TitleKind.SERIES


def title_from_payload(
    payload: Mapping[str, Any], title_id: uuid.UUID, *, provider: str, region: str
) -> Title:
    """One TMDb detail response -> one canonical `Title`.

    `title_id` is passed in and never minted (ADR-0003). `enrichment_state`
    is left at the model default and is `EnrichService`'s to raise through
    `ENRICHMENT_RANK` (ADR-0008) — a mapper that stamped `ENRICHED` would
    promote a title on a payload carrying nothing but an id.
    """
    kind = kind_of_payload(payload)
    tmdb_id = _as_int(payload.get("id"))
    if tmdb_id is None:
        raise PortDataMalformed("TMDb payload has no usable `id`", detail=str(payload.get("id")))
    name = _text(payload.get("name") if kind is TitleKind.SERIES else payload.get("title"))
    if name is None:
        raise PortDataMalformed("TMDb payload has no usable name", detail=str(tmdb_id))

    released = _date(
        payload.get("first_air_date") if kind is TitleKind.SERIES else payload.get("release_date")
    )
    status = _STATUS.get(str(payload.get("status") or "").strip().lower())
    last_air = _date(payload.get("last_air_date"))
    fields: dict[str, Any] = {
        "original_name": _text(
            payload.get("original_name")
            if kind is TitleKind.SERIES
            else payload.get("original_title")
        ),
        "year": released.year if released else None,
        "release_date": released,
        "end_year": last_air.year if last_air and status in _FINISHED else None,
        "overview": _text(payload.get("overview")),
        "tagline": _text(payload.get("tagline")),
        "runtime_minutes": _runtime(payload, kind),
        "status": status,
        "genres": _names(payload.get("genres")),
        "keywords": _keywords(payload, kind),
        "original_language": _text(payload.get("original_language")),
        "spoken_languages": _codes(payload.get("spoken_languages"), "iso_639_1"),
        "origin_countries": _strings(payload.get("origin_country")),
        "content_rating": _content_rating(payload, kind, region),
        "community_rating": _bounded(payload.get("vote_average"), 0.0, 10.0),
        "vote_count": _non_negative_int(payload.get("vote_count")),
        "popularity": _non_negative_float(payload.get("popularity")),
        "imdb_id": _imdb_id(payload),
        "tvdb_id": _as_int(_external_ids(payload).get("tvdb_id")),
    }
    return Title(
        id=title_id,
        kind=kind,
        tmdb_id=tmdb_id,
        name=name,
        # No normalisation: `Title.sort_name` has an explicit
        # no-normalisation contract, and inventing one here would be the
        # adapter-side convention that model deliberately refused.
        sort_name=name,
        field_provenance=_provenance({"name": name, "tmdb_id": tmdb_id, **fields}, provider),
        **fields,
    )


def seasons_and_episodes(
    payload: Mapping[str, Any], title_id: uuid.UUID
) -> tuple[tuple[Season, ...], tuple[Episode, ...]]:
    """The hierarchy under a series, empty for a movie.

    Reads `seasons[]` as `TmdbMetadataProvider.fetch` composes it: each entry
    is the series detail's own season summary with that season's
    `/tv/{id}/season/{n}` response merged over it, so `episodes` is present
    for a season that was fetched and absent for one that was not. A season
    whose episodes were never fetched still produces its `Season` row —
    losing the row as well would leave the episodes unattachable when a
    later run does fetch them.
    """
    seasons: list[Season] = []
    episodes: list[Episode] = []
    for entry in _mappings(payload.get("seasons")):
        number = _non_negative_int(entry.get("season_number"))
        if number is None:
            # TMDb has no season without a number; a payload that lost one is
            # not worth failing the whole series over.
            continue
        air_date = _date(entry.get("air_date"))
        season = Season(
            title_id=title_id,
            season_number=number,
            name=_text(entry.get("name")),
            overview=_text(entry.get("overview")),
            air_date=air_date,
            episode_count=_non_negative_int(entry.get("episode_count")),
            tmdb_id=_as_int(entry.get("id")),
        )
        seasons.append(season)
        episodes.extend(_episodes_of(entry, season, title_id))
    return tuple(seasons), tuple(episodes)


def people_and_credits(
    payload: Mapping[str, Any], title_id: uuid.UUID
) -> tuple[list[Person], list[Credit]]:
    """One TMDb detail response -> that title's people and their credits.

    Pure, and a pure function of a payload that may have come out of
    `raw_payloads` months after the fetch that produced it -- `to_result`'s
    property, restated because this one is reached the same way.

    **Three sources, and the third is the per-kind divergence.**
    `credits.cast[]` is billed cast, `credits.crew[]` is crew filtered to
    `CREDITED_JOBS`, and `created_by[]` is a series' creators -- a top-level
    array, **not** part of `credits.crew`, which is `[]` on the recorded
    series payload. A mapper that read creators out of the crew returns
    nothing for every series in the catalog.

    **Ids are minted here and are placeholders.** `Person.id` is a fresh
    UUIDv7 per sighting, exactly as ingest mints one per season, and every
    `Credit.person_id` names the `Person` minted beside it in this same call.
    A person the catalog already holds keeps the id it was inserted with, so
    the caller upserts on `tmdb_id`, reads the real ids back through
    `PersonRepository.resolve_tmdb_ids`, and re-points the credits. Nothing
    here can know that id.

    **An entry with no usable `id` is dropped rather than raised on.** The
    standing rule this module opens with -- nothing TMDb can put in a payload
    may raise, because a `pydantic.ValidationError` is not a `UsherPortError`
    and would kill the worker instead of parking one job -- and here there is
    a second, independent reason: a person with a NULL `tmdb_id` is inserted
    rather than merged (the unique index is partial), so its stored id can
    never be read back and any credit naming it would be permanently
    orphaned.

    One person may hold several credits on one title -- a director who also
    wrote it, an actor who also created the series -- and they are separate
    rows. `(title_id, person_id, kind, job)` is the natural key precisely so
    they do not collapse; a mapper deduplicating on `(title_id, person_id)`
    keeps whichever it saw second.
    """
    entries = _credit_entries(payload)

    people: dict[int, Person] = {}
    for one in entries:
        existing = people.get(one.tmdb_id)
        if existing is None:
            people[one.tmdb_id] = Person(
                tmdb_id=one.tmdb_id,
                name=one.name,
                # In `domain/`, never spelled here: an adapter-side spelling
                # is what makes a sort order irreproducible between callers.
                sort_name=person_sort_name(one.name),
                known_for_department=one.known_for_department,
            )
        elif existing.known_for_department is None and one.known_for_department is not None:
            # `PersonRepository.upsert_many`'s COALESCE rule arriving one
            # layer early, and it is reachable *inside a single payload*: a
            # `created_by[]` entry carries no `known_for_department` and a
            # `credits.cast[]` entry does, so the same person arrives with it
            # and without it in one pass. Frozen models, so this is a new
            # instance -- which is why the credits are built in a second pass,
            # against the finished map.
            people[one.tmdb_id] = existing.evolve(known_for_department=one.known_for_department)

    credits = [
        Credit(
            person_id=people[one.tmdb_id].id,
            title_id=title_id,
            kind=one.kind,
            # Named here rather than defaulted on the model, which is
            # ADR-0036's whole point: this adapter is the only thing in `src/`
            # that constructs a `Credit`, and it is the only thing that knows
            # which source it read. A default would let the *next* writer --
            # an IMDb one -- inherit `tmdb` by forgetting.
            source=CreditSource.TMDB,
            tmdb_credit_id=one.tmdb_credit_id,
            character=one.character,
            job=one.job,
            department=one.department,
            billing_order=one.billing_order,
        )
        for one in entries
    ]
    return list(people.values()), credits


def images_from_payload(
    payload: Mapping[str, Any], title_id: uuid.UUID, *, provider: str
) -> list[Image]:
    """One TMDb detail response -> that title's artwork references.

    Pure, and a pure function of a payload that may have come out of
    `raw_payloads` months after the fetch that produced it -- `to_result`'s
    property, restated because this one is reached the same way and because
    **most of a real catalog was cached before `images` joined
    `*_APPEND_TO_RESPONSE`**. Such a payload still carries `poster_path` and
    `backdrop_path` (they are top-level detail fields, not an appended
    namespace), so a re-derivation of it yields two rows rather than none --
    and `series.json`'s shape, three empty arrays, yields the same two.

    **Two sources, and the second decides `is_primary`.** `images.{posters,
    backdrops,logos}[]` is the catalogue; the top-level `poster_path` and
    `backdrop_path` are TMDb's own pick out of it. Nothing in the arrays is
    flagged, so without the second source every card would render whichever
    language variant sorted first.

    **Deduplicated by `provider_path`, which is the natural key's own
    spelling** -- `uq_images_owner_provider_path` is
    `(title_id, episode_id, person_id, provider, provider_path)` and this call
    holds the first four fixed, so the path is the whole of what can collide.
    It is required rather than tidy, and the reason is *not* the one this
    task's plan predicted: `ImageRepository.replace_for_titles` already
    deduplicates last-wins on the same key, so a duplicate does not fail the
    batch. What it does is let emission order decide `is_primary` -- in
    `movie.json` the top-level poster **is** `posters[0]`, so the two rows
    differ in exactly that flag, and last-wins keeps the array's unflagged
    copy. Measured, not reasoned: see this task's sweep ledger.

    So the primary is folded into the row already built for its path rather
    than appended beside it, which keeps the array entry's `width`/`height`/
    `iso_639_1` -- a bare promotion from the top-level key alone has no
    dimensions at all, and a layout engine cannot ask for them again.

    **A path is recorded whatever its extension, and a `logo` is where that
    matters.** The provider publishes some logos as `.svg`, and
    `usher.ports.images.SUPPORTED_MEDIA_TYPES` deliberately has no entry for
    `image/svg+xml` — so a row derived here can name artwork the proxy will
    refuse to cache. That is the right split rather than an oversight: this
    stage records what the provider says it has, from a payload months old,
    with no way to ask what the CDN would answer today; which media types are
    servable is a serve-time fact and belongs to the fetcher that meets one.
    Dropping the row here instead would make `GET /titles/{id}` deny the
    existence of a logo the provider does publish.

    **Nothing TMDb can put in a payload may raise**, this module's standing
    rule, and `Image` bounds four fields: `provider_path` and `provider` are
    `min_length=1` and `width`/`height` are `gt=0`. An entry with no usable
    path is dropped; a zero, negative or unparseable dimension becomes `None`,
    which is the same answer a provider that reports no dimensions gets.
    """
    block = payload.get("images")
    arrays = block if isinstance(block, Mapping) else {}

    # Keyed by path so the fold below finds the row it has to flag, and
    # insertion-ordered so `Image.id` is minted in first-sighting order -- the
    # tiebreak `(is_primary DESC, id)` reads, since `m09c` carries no
    # `sort_order` column.
    by_path: dict[str, Image] = {}
    for field, kind in _IMAGE_ARRAYS:
        taken = 0
        for entry in _mappings(arrays.get(field)):
            path = _text(entry.get("file_path"))
            if path is None or path in by_path:
                continue
            by_path[path] = Image(
                title_id=title_id,
                kind=kind,
                provider=provider,
                provider_path=path,
                width=_positive_int(entry.get("width")),
                height=_positive_int(entry.get("height")),
                # NULL means "no language", which is different from "English".
                # TMDb spells a language-neutral backdrop `null` here.
                language=_text(entry.get("iso_639_1")),
                is_primary=False,
            )
            taken += 1
            if taken == _IMAGES_PER_KIND_LIMIT:
                break

    for field, kind in _PRIMARY_PATHS:
        path = _text(payload.get(field))
        if path is None:
            continue
        standing = by_path.get(path)
        by_path[path] = (
            # `.evolve()`, never `model_copy(update=)`: the flagged row is
            # re-validated from scratch.
            standing.evolve(is_primary=True)
            if standing is not None
            else Image(
                title_id=title_id,
                kind=kind,
                provider=provider,
                provider_path=path,
                is_primary=True,
            )
        )
    return list(by_path.values())


def collection_from_payload(payload: Mapping[str, Any]) -> Collection | None:
    """A movie's `belongs_to_collection` -> a `Collection`, or `None`.

    **Three shapes reach `None` and all three are ordinary.** `null` is the
    common case for a standalone film; the key is **absent entirely** on every
    series, verified against the recorded `series.json`'s top-level key set;
    and an object missing an `id` or a usable `name` is dropped rather than
    raised on, because `Collection.name` is `min_length=1` and a
    `pydantic.ValidationError` is not a `UsherPortError`.

    `tmdb_id` is what makes a re-derivation an update rather than a duplicate.
    No `overview` and no artwork: those live on `/collection/{id}`, a second
    network call boundary call 4 refuses.

    This does **not** check the payload's kind, and does not need to: a series
    has no such key. `CollectionRepository.attach_titles` filters
    `kind = 'movie'` itself rather than trusting its caller, so the property
    holds in two independent places.
    """
    block = payload.get("belongs_to_collection")
    if not isinstance(block, Mapping):
        return None
    tmdb_id = _as_int(block.get("id"))
    name = _text(block.get("name"))
    if tmdb_id is None or name is None:
        return None
    return Collection(tmdb_id=tmdb_id, name=name)


def search_candidates(body: Mapping[str, Any], kind: TitleKind) -> list[MetadataCandidate]:
    """One `/search/movie` or `/search/tv` page -> candidates.

    `kind` is the caller's, not the payload's: TMDb's two search endpoints
    each return one space and neither labels its results. `/search/multi`
    does label them, and supports neither endpoint's year filter — which is
    why this adapter never uses it.

    A result with no usable id is skipped rather than raised on: one broken
    entry must not cost the whole page, on the tier PRD 03 calls a last
    resort.
    """
    candidates: list[MetadataCandidate] = []
    for entry in _mappings(body.get("results")):
        provider_id = _as_int(entry.get("id"))
        name = _text(entry.get("name") if kind is TitleKind.SERIES else entry.get("title"))
        if provider_id is None or name is None:
            continue
        released = _date(
            entry.get("first_air_date") if kind is TitleKind.SERIES else entry.get("release_date")
        )
        candidates.append(
            MetadataCandidate(
                provider_id=provider_id,
                name=name,
                year=released.year if released else None,
                kind=kind,
                popularity=_non_negative_float(entry.get("popularity")) or 0.0,
            )
        )
    return candidates


def changed_ids(body: Mapping[str, Any]) -> tuple[list[int], bool]:
    """One `/movie/changes` or `/tv/changes` page: its ids, and whether more
    pages follow.

    Both feeds have the identical shape (`results[].id`, `page`,
    `total_pages`) — the one place TMDb's two spaces agree — so one reader
    serves both and the *kind* comes from which URL was asked.
    """
    ids = [found for entry in _mappings(body.get("results")) if (found := _as_int(entry.get("id")))]
    page = _as_int(body.get("page")) or 1
    total = _as_int(body.get("total_pages")) or 1
    return ids, page < total


# -- filters ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CreditEntry:
    """One payload entry, already filtered and already read.

    The array an entry came from is what fixes `kind`, `job`, `department`
    and `billing_order` -- none of the four is inferable from the entry
    alone, because a crew entry with no `job` and a cast entry with no
    `character` are the same row shape.

    **Every field here is the parsed value, not the raw one**, so the two
    passes in `people_and_credits` cannot disagree about which entries are
    usable. An entry with no id or no name never becomes one of these at all:
    a person minted without a credit, or a credit naming a person that was
    never minted, are the two shapes that split would produce.
    """

    tmdb_id: int
    name: str
    kind: CreditKind
    known_for_department: str | None
    tmdb_credit_id: str | None
    character: str | None
    job: str | None
    department: str | None
    billing_order: int | None


def _credit_entries(payload: Mapping[str, Any]) -> list[_CreditEntry]:
    """Cast, then filtered crew, then creators -- in that order.

    Order matters and is not cosmetic: `people_and_credits` keeps the first
    populated `known_for_department` it sees, and cast entries carry one
    while `created_by[]` entries never do. Reading the creators first would
    make the COALESCE branch the common path instead of the rare one.
    """
    block = payload.get("credits")
    credits: Mapping[str, Any] = block if isinstance(block, Mapping) else {}
    entries: list[_CreditEntry] = []

    for entry in _mappings(credits.get("cast")):
        billing = _non_negative_int(entry.get("order"))
        # **The cutoff is on `order`, never on the array index.** The two
        # agree on every array TMDb happens to have sorted, which is most of
        # them -- and where they disagree, slicing the array keeps the wrong
        # fifty and renumbers the lead actor. An entry with no `order` at all
        # is kept: TMDb always sends one for cast, and dropping a cast member
        # over a missing sort key would be losing data to tidiness.
        if billing is not None and billing >= _CAST_LIMIT:
            continue
        _append(entries, entry, CreditKind.CAST, job=None, department=None, billing_order=billing)

    for entry in _mappings(credits.get("crew")):
        job = _text(entry.get("job"))
        if job is None or job not in CREDITED_JOBS:
            continue
        _append(
            entries,
            entry,
            CreditKind.CREW,
            job=job,
            department=_text(entry.get("department")),
            # **`crew[]` has no `order` field** -- read out of the fixtures,
            # not assumed. So `billing_order` is `None` here, and a schema
            # that made it `NOT NULL DEFAULT 0` would put every crew member
            # above the star of the film in every "top billed" read, because
            # `list_for_title` orders nulls last.
            billing_order=None,
        )

    # The ninth divergence row. A series' creators are a *top-level* array,
    # not part of `credits.crew` -- which is `[]` on the recorded series
    # payload -- so a mapper that read the crew returns nothing for every
    # series in the catalog, silently. `created_by[]` entries carry no `job`,
    # no `department` and no `order`, so all three are supplied here.
    for entry in _mappings(payload.get("created_by")):
        _append(
            entries,
            entry,
            CreditKind.CREW,
            job=_CREATOR_JOB,
            department=_CREATOR_DEPARTMENT,
            billing_order=None,
        )
    return entries


def _append(
    entries: list[_CreditEntry],
    entry: Mapping[str, Any],
    kind: CreditKind,
    *,
    job: str | None,
    department: str | None,
    billing_order: int | None,
) -> None:
    """One filtered entry, or nothing at all.

    The id and the name are read here, once, so the two passes over the
    result cannot disagree about which entries are usable. An entry missing
    either is dropped rather than raised on -- `mapping.py`'s standing rule,
    plus a second reason specific to people: one with no provider id is
    *inserted* rather than merged (the unique index is partial), so its
    stored id can never be read back and any credit naming it would be
    permanently orphaned.
    """
    tmdb_id = _as_int(entry.get("id"))
    name = _text(entry.get("name"))
    if tmdb_id is None or name is None:
        return
    entries.append(
        _CreditEntry(
            tmdb_id=tmdb_id,
            name=name,
            kind=kind,
            known_for_department=_text(entry.get("known_for_department")),
            tmdb_credit_id=_text(entry.get("credit_id")),
            character=_text(entry.get("character")),
            job=job,
            department=department,
            billing_order=billing_order,
        )
    )


def _episodes_of(entry: Mapping[str, Any], season: Season, title_id: uuid.UUID) -> list[Episode]:
    rows: list[Episode] = []
    for raw in _mappings(entry.get("episodes")):
        number = _non_negative_int(raw.get("episode_number"))
        if number is None:
            continue
        rows.append(
            Episode(
                title_id=title_id,
                # The season row minted just above, never a fresh id:
                # `episodes.season_id` is a real FK and a UUID naming no row
                # fails on `fk_episodes_season_id_seasons`.
                season_id=season.id,
                season_number=season.season_number,
                episode_number=number,
                name=_text(raw.get("name")),
                overview=_text(raw.get("overview")),
                air_date=_date(raw.get("air_date")),
                runtime_minutes=_non_negative_int(raw.get("runtime")),
                tmdb_id=_as_int(raw.get("id")),
                # TMDb's episode payload carries no IMDb id without a second
                # `external_ids` request per episode. 999,827 episodes makes
                # that request count a design defect, not a gap.
                imdb_id=None,
            )
        )
    return rows


def _provenance(fields: Mapping[str, Any], provider: str) -> dict[str, str]:
    """`field -> provider` for what this payload actually supplied.

    An entry for a field the payload left empty is what makes a second
    provider's merge ambiguous later (PRD 02), so an empty tuple and a `None`
    are both "not supplied".
    """
    return {
        field: provider
        for field, value in fields.items()
        if value is not None and value != () and value != ""
    }


def _external_ids(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    found = payload.get("external_ids")
    return found if isinstance(found, Mapping) else {}


def _imdb_id(payload: Mapping[str, Any]) -> str | None:
    """Top-level for a movie, `external_ids` for a series -- and both are
    tried for both, because reading a field a payload does not carry costs
    nothing and TMDb serves `external_ids` for movies too."""
    for candidate in (payload.get("imdb_id"), _external_ids(payload).get("imdb_id")):
        if isinstance(candidate, str) and _IMDB_ID.match(candidate):
            return candidate
    return None


def _runtime(payload: Mapping[str, Any], kind: TitleKind) -> int | None:
    if kind is TitleKind.MOVIE:
        return _non_negative_int(payload.get("runtime"))
    # TMDb has no series-level runtime. `episode_run_time` is an array
    # (multiple lengths for a show whose format changed); the first is the
    # nearest thing to "how long is an episode of this".
    lengths = payload.get("episode_run_time")
    if not isinstance(lengths, Sequence) or isinstance(lengths, str | bytes) or not lengths:
        return None
    return _non_negative_int(lengths[0])


def _keywords(payload: Mapping[str, Any], kind: TitleKind) -> tuple[str, ...]:
    """`keywords.keywords` for a movie, `keywords.results` for a series.

    Both spellings are read regardless of kind: the cost is one dictionary
    miss and the benefit is that a payload composed the other way round --
    a cached one from before this was understood, say -- still yields its
    keywords rather than an empty tuple nobody notices.
    """
    block = payload.get("keywords")
    if not isinstance(block, Mapping):
        return ()
    return _names(block.get("keywords")) or _names(block.get("results"))


def _content_rating(payload: Mapping[str, Any], kind: TitleKind, region: str) -> str | None:
    """The configured region's certification, or `None`.

    Never another country's. TMDb returns every country it knows and a
    household outside the configured region would otherwise be shown a
    rating that means nothing where they live -- PRD 02 renders this string
    to clients.
    """
    wanted = region.upper()
    if kind is TitleKind.SERIES:
        block = payload.get("content_ratings")
        results = _mappings(block.get("results")) if isinstance(block, Mapping) else []
        for entry in results:
            if str(entry.get("iso_3166_1", "")).upper() == wanted:
                return _text(entry.get("rating"))
        return None
    block = payload.get("release_dates")
    results = _mappings(block.get("results")) if isinstance(block, Mapping) else []
    for entry in results:
        if str(entry.get("iso_3166_1", "")).upper() != wanted:
            continue
        for release in _mappings(entry.get("release_dates")):
            # Skipping the empty ones is load-bearing rather than tidy: TMDb
            # attaches an uncertificated festival entry (`type: 1`) ahead of
            # the theatrical one for a great many films, and taking the first
            # entry regardless reports no certification for all of them.
            certification = _text(release.get("certification"))
            if certification is not None:
                return certification
    return None


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [one for one in value if isinstance(one, Mapping)]


def _names(value: Any) -> tuple[str, ...]:
    return tuple(name for one in _mappings(value) if (name := _text(one.get("name"))) is not None)


def _codes(value: Any, field: str) -> tuple[str, ...]:
    return tuple(code for one in _mappings(value) if (code := _text(one.get(field))) is not None)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(text for one in value if (text := _text(one)) is not None)


def _text(value: Any) -> str | None:
    """A non-empty string, or `None`. TMDb spells "we do not know" as `""`
    for almost every string field, and `Title.name` is `min_length=1`."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _date(value: Any) -> date | None:
    """`"1999-10-15"` -> a date; `""`, `None`, and anything unparseable ->
    `None`. TMDb really does send `""` for an unreleased film."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _non_negative_int(value: Any) -> int | None:
    number = _as_int(value)
    return number if number is not None and number >= 0 else None


def _positive_int(value: Any) -> int | None:
    """`Image.width`/`height` are `gt=0`, not `ge=0`: a stored `0` is a
    placeholder a layout engine divides by, and `None` is the honest answer for
    a dimension the provider did not report."""
    number = _as_int(value)
    return number if number is not None and number > 0 else None


def _non_negative_float(value: Any) -> float | None:
    """`None` for anything `Title.popularity` will not take, **including a
    non-finite one**.

    `math.isfinite` is not decoration beside `value >= 0`: `float("inf") >= 0`
    is `True`, and `json.loads` maps any JSON number that overflows binary64 --
    `1e400`, which is well-formed JSON -- straight onto `inf` with no error.
    Before M10's F9 that value reached `titles.popularity` (`double
    precision`, where IEEE `Infinity` is legal and satisfies the column's own
    `>= 0` CHECK) and sorted above every real title forever. `Title.popularity`
    now carries `allow_inf_nan=False`, so without this filter the same payload
    would raise `pydantic.ValidationError` out of the constructor below --
    which is not a `UsherPortError`, and this module's contract is that nothing
    TMDb can put in a payload may raise.

    `_bounded` needs no such clause and is left alone: `low <= inf <= high` is
    `False` and every comparison against `NaN` is `False`, so a ceiling
    excludes both already. That is why `community_rating` never had this
    defect and `popularity` did.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _bounded(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if low <= value <= high else None
