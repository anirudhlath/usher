"""`GET /people/{id}` (PRD 07) -- a filmography, grouped by role.

**Four fields PRD 02's `Person` sketch carries are absent, and none of them is
a null here.** `imdb_id`, `birth_year`, `death_year` and `biography` live on
TMDb's `/person/{id}` -- one request per person -- and are M7's named orphan,
still unassigned in PRD 09. `domain/people.py` has no column for any of them,
so there is nothing to render and nothing to render as `null`: an always-null
field is a client-side branch that never takes its other arm, which is the
argument `dto/title.py` already makes about `images` and `credits`.

**`sort_name` is absent too, for a different reason.** `person_sort_name` is
the identity function today and its docstring says the obvious alternatives
are all wrong for some real name; a wire field whose value equals `name` on
every row is a promise this API would then have to keep.

**Groups are `cast` plus one per crew `job`, and the job strings are TMDb's
own.** Recorded rather than normalised: a normalisation map is a second
opinion nothing measures, and it would be applied at the one place a reader
cannot see what the derivation actually stored. What that costs is that the
crew vocabulary on the wire is whatever TMDb emitted -- `Director`,
`Screenplay`, `Original Music Composer` -- rather than a set this project
controls. It is not a *source*-specific concept in CLAUDE.md's sense (TMDb is
a metadata provider, not a media server), but it is unvalidated free text and
a client must treat it as a label to print rather than a key to branch on.

**`cast` is lower-case and every crew job TMDb emits is title-cased**, so the
one group whose label this module chooses cannot collide with one it was
given. `Casting` is a real TMDb crew job and is a different string.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel

from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.people import CreditKind, Person
from usher.domain.title import Title
from usher.ports.repository import PersonCredit

__all__ = [
    "CAST_ROLE",
    "CREW_ROLE",
    "FilmographyGroupResponse",
    "FilmographyTitleResponse",
    "PersonResponse",
]

# The two labels this module mints rather than reads off a credit.
CAST_ROLE = "cast"
# `credits.job` is nullable and `Credit`'s docstring says why -- "a crew entry
# with no `job` and a cast entry with no `character` are the same row shape".
# `None` is not a JSON object key and not a role a client can print, and
# dropping the credit would lose a title from the filmography silently, so the
# untitled crew credit gets a label of its own.
CREW_ROLE = "crew"


class FilmographyTitleResponse(BaseModel):
    """One title on one of a person's shelves.

    Deliberately narrower than `RowCardResponse`: no `owned`, no progress and
    no `episode_id`. All three are facts about a *household*, and this route
    takes no user -- a filmography is a fact about the person. Adding them
    would be three more reads (`media_items`, `watch_states`, `episodes`) on a
    route whose whole answer is otherwise two statements.
    """

    title_id: uuid.UUID
    kind: TitleKind
    name: str
    year: int | None
    # PRD 07: "Every title-bearing response carries `enrichment_state` so
    # clients render deliberately -- skeleton shimmer on fields known to be
    # missing -- rather than inferring intent from nulls." A filmography is
    # mostly skeleton rows on a bootstrap-only catalog, so this is the field
    # that stops it looking like a page of broken cards.
    enrichment_state: EnrichmentState


class FilmographyGroupResponse(BaseModel):
    """One role, and the titles the person holds it on.

    `role` is a label to print, never a key to branch on -- see the module
    docstring. The titles are newest first with `title_id` breaking a tie, and
    a title appears **once** in a group however many credits put it there: two
    characters in one film is one entry in `cast`.
    """

    role: str
    titles: tuple[FilmographyTitleResponse, ...]


class PersonResponse(BaseModel):
    """A person, and their filmography grouped by role.

    **`groups` is absent rather than empty when there is nothing to group**,
    which is group B's convention for this whole surface and needs
    `response_model_exclude_unset=True` on the route to survive
    serialisation. A client cannot tell `[]` from "this person's credits have
    not been derived yet", and on a catalog whose enriched tier is single-digit
    thousands of titles out of 1.27M the second is the common case -- so an
    empty list would be a page that says "no known credits" about a working
    actor.
    """

    id: uuid.UUID
    name: str
    known_for_department: str | None
    # Defaulted **so that it can go unset**, which is the only mechanism
    # pydantic offers for "absent rather than empty" on a field that is
    # sometimes present. The default is never serialised: `of` either passes a
    # non-empty tuple or passes nothing at all.
    groups: tuple[FilmographyGroupResponse, ...] = ()

    @classmethod
    def of(
        cls, person: Person, credits: Sequence[PersonCredit], titles: Iterable[Title]
    ) -> "PersonResponse":
        """The grouping, which is the whole of what this route decides.

        A pure function of three port answers: the person, their credits, and
        whatever `list_by_ids` still holds for the titles those credits name.
        **Every field except `groups` is passed on both arms**, because the
        route serialises with `exclude_unset=True` and a field this method
        forgot to set would silently vanish from the document rather than
        fail.
        """
        grouped = _group(credits, {title.id: title for title in titles})
        if not grouped:
            return cls(
                id=person.id, name=person.name, known_for_department=person.known_for_department
            )
        return cls(
            id=person.id,
            name=person.name,
            known_for_department=person.known_for_department,
            groups=grouped,
        )


def _role_of(credit: PersonCredit) -> str:
    if credit.kind is CreditKind.CAST:
        return CAST_ROLE
    return credit.job or CREW_ROLE


def _group(
    credits: Sequence[PersonCredit], titles: Mapping[uuid.UUID, Title]
) -> tuple[FilmographyGroupResponse, ...]:
    """Credits into role groups, each group newest-first.

    **A credit naming a title the catalog no longer holds is dropped, not a
    `KeyError`.** `list_by_ids` returns fewer rows than it was asked for --
    the port says so -- and a title deleted between the credit read and the
    hydration is ordinary. `SearchService._rank` and
    `SimilarityService.neighbors_of` both already guard exactly this, and this
    is the third call site.

    **Groups are ordered `cast` first, then the crew labels alphabetically.**
    Not dict insertion order: `list_for_person` orders by `billing_order` nulls
    last then `title_id`, so insertion order here is a property of the credit
    rows rather than of the roles, and a re-derivation that renumbered billing
    would silently reorder a person's page.
    """
    members: dict[str, dict[uuid.UUID, Title]] = {}
    for credit in credits:
        title = titles.get(credit.title_id)
        if title is None:
            continue
        # Keyed by title id inside the group, so a person credited twice in
        # one role on one film is one entry -- while the same person credited
        # in two roles is one entry in each, which is the other side of
        # `RecurringPerson`'s distinct-title rule.
        members.setdefault(_role_of(credit), {})[title.id] = title
    return tuple(
        FilmographyGroupResponse(
            role=role,
            titles=tuple(
                FilmographyTitleResponse(
                    title_id=title.id,
                    kind=title.kind,
                    name=title.name,
                    year=title.year,
                    enrichment_state=title.enrichment_state,
                )
                # Newest first, `title_id` breaking a tie, and unknown years
                # **last** rather than at whichever end the spelling happens to
                # put them: `year` is nullable on every skeleton row the IMDb
                # bootstrap wrote, so a naive key opens a filmography with the
                # titles nobody knows the date of.
                for title in sorted(
                    group.values(),
                    key=lambda one: (one.year is None, -(one.year or 0), one.id),
                )
            ),
        )
        for role, group in sorted(members.items(), key=lambda item: (item[0] != CAST_ROLE, item[0]))
    )
