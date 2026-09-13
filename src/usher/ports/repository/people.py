"""People and credits, which are two ports over one re-derivation."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import AwareDatetime

from usher.domain.people import Credit, CreditKind, Person
from usher.ports.repository._results import BulkWriteResult

__all__ = [
    "CreditRepository",
    "CreditedPerson",
    "PersonCredit",
    "PersonRepository",
    "RecurringPerson",
]


@dataclass(frozen=True, slots=True)
class CreditedPerson:
    """One credit and the person it names, in one row rather than two reads.

    A bare `Credit` carries a `person_id` and nothing renderable, so a port
    returning them hands every caller the same second query. That is the N+1
    this milestone's front matter names, relocated into the port rather than
    removed -- and a port that *offers* an N+1 is worse than one a caller
    invents, because it looks sanctioned.
    """

    person_id: uuid.UUID
    name: str
    kind: CreditKind
    character: str | None
    job: str | None
    department: str | None
    billing_order: int | None


@dataclass(frozen=True, slots=True)
class PersonCredit:
    """One of a person's credits, with the title it is on.

    The mirror of `CreditedPerson`: the person is the thing already known, so
    what travels is the title id. Hydration into a `RowCard` is
    `TitleRepository`'s, which is what keeps this port from growing a second
    opinion about what a title is.
    """

    title_id: uuid.UUID
    kind: CreditKind
    character: str | None
    job: str | None
    billing_order: int | None


@dataclass(frozen=True, slots=True)
class RecurringPerson:
    """A person who recurs across the titles one user has actually played.

    `watched_title_count` is a count of **distinct titles**, never of credits.
    A person credited twice on one film -- two jobs, or two characters, both
    of which TMDb genuinely emits -- would otherwise read as two titles, and a
    one-film person would out-rank a four-film one. The row this feeds says
    "you keep watching this person"; counting credits makes it say something
    else with total confidence, which is exactly the failure this milestone
    opens by describing.

    `kind` and `job` travel because the row's own text needs them: "More from
    <name>" is a worse row than "Directed by <name>", and a provider holding
    only a name cannot tell the two apart.
    """

    person_id: uuid.UUID
    name: str
    kind: CreditKind
    job: str | None
    watched_title_count: int
    # **The most recent watch that credits them, and it is a tiebreak the row cannot
    # compute for itself.** Two directors at four titles each, one from last month and
    # one from 2019, is the front matter's opening failure with a person's name on it --
    # a beautifully constructed row about a film watched three years ago -- and
    # `watched_title_count` alone cannot separate them, so "whatever the aggregate
    last_watched_at: AwareDatetime | None


class PersonRepository(ABC):
    """Persistence for canonical people (PRD 02's `Person`)."""

    @abstractmethod
    async def get(self, person_id: uuid.UUID) -> Person | None:
        """One person by id, or `None` when the catalog does not hold them.

        `None` rather than a raise, on `TitleRepository.get`'s terms: an id a
        client supplied naming no row is an ordinary request, and the route
        above turns it into a 404 with a `code` from the vocabulary. An
        implementation that raised would make it a 500.

        **Scoped to the id, which is the thing worth asserting.** A `WHERE`
        that lost its predicate returns a `Person` -- populated, correctly
        typed, and about somebody else -- and `GET /people/{id}` renders that
        person's filmography under the requested person's name. The contract
        case seeds two people for exactly that reason.

        `imdb_id`, `birth_year`, `death_year` and `biography` are **not**
        carried, because `Person` does not have them: they live on TMDb's
        `/person/{id}`, one request per person, and are still unassigned
        (PRD 09's M7 named orphan). This method returns the stored row, so
        the route's answer is narrow rather than null-padded.
        """

    @abstractmethod
    async def upsert_many(self, people: Sequence[Person]) -> BulkWriteResult:
        """Insert or update, keyed on `tmdb_id`."""

    @abstractmethod
    async def resolve_tmdb_ids(self, tmdb_ids: Sequence[int]) -> dict[int, uuid.UUID]:
        """`tmdb_id` -> person id, in one round trip.

        Exists for `EpisodeRepository.resolve_seasons`' reason, restated
        because it is the same defect: `upsert_many` reports counts rather
        than ids, and it cannot report the caller's -- the derivation mints a
        fresh UUIDv7 per sighting and a person the catalog already holds keeps
        the id it was inserted with. So the id a `Credit.person_id` must carry
        is knowable only by reading it back.

        **A batch rather than one, and the number is the argument.** A single
        enriched movie names tens of people; the enriched tier is 2k-10k
        titles. A lookup per person is the round-trip-per-item shape batching
        exists to remove.

        Absent keys mean "no such person", never "not asked", so a caller
        iterates its own probes rather than reading a short answer as a full
        one.
        """

    @abstractmethod
    async def count(self) -> int:
        """How many people the catalog holds.

        `usher derive`'s report, and the one number that tells an operator a derivation
        ran at all.
        """

    @abstractmethod
    async def list_recurring_for_user(
        self, user_id: uuid.UUID, *, min_titles: int = 2, limit: int = 10
    ) -> list[RecurringPerson]:
        """People who recur across the titles this user has played, most first."""


class CreditRepository(ABC):
    """Persistence for `credits` -- the join that makes "more from this director" a lookup.

    **And for the two denormalisations of it that no generated column can
    reach**: `titles.credit_names`, which is weight class B's input, and the
    `person` rows of `title_search_names`, which are the two-tier suggest's.
    Both are written by `replace_for_titles` and by nothing else, which is the
    whole of what keeps three copies of one fact honest.

    **The write is a replace, not an upsert, and that is the port's central
    decision.** A title's credit set changes upstream: a name is corrected, a
    role is removed, a mis-attributed actor is deleted. An upsert can express
    every one of those except the last, and the last is the one that leaves a
    permanently wrong row -- so the unit of work is "this title's credits are
    now exactly these", which only a scoped replace can say.

    Same session ownership as every other repository here: flushes, never
    commits.
    """

    @abstractmethod
    async def replace_for_titles(
        self,
        title_ids: Sequence[uuid.UUID],
        credits: Sequence[Credit],
        *,
        credit_names: Mapping[uuid.UUID, Sequence[str]],
    ) -> int:
        """Replace every stored credit for `title_ids` with `credits`.

        and write `titles.credit_names` **and the credited-person half of
        `title_search_names`** for the same scope in the same call.
        """

    @abstractmethod
    async def list_for_title(
        self, title_id: uuid.UUID, *, kind: CreditKind | None = None, limit: int = 20
    ) -> list[CreditedPerson]:
        """One title's credits, top-billed first, with the person joined in."""

    @abstractmethod
    async def count_titles_with_credits(self) -> int:
        """How many **distinct titles** hold at least one credit.

        Titles, never credit rows: a report counting rows says "412,000
        credits" where an operator asked "did my library get derived", and one
        heavily-credited film moves it by fifty. This is the numerator beside
        `RawPayloadStore.count`'s denominator, and the two are printed
        unreduced.
        """

    @abstractmethod
    async def list_for_person(self, person_id: uuid.UUID, *, limit: int = 50) -> list[PersonCredit]:
        """Everything one person is credited on -- `PeopleProvider`'s cards.

        Scoped to the person, and an implementation that forgets the filter
        returns the whole table in physical order, which satisfies every
        membership assertion and no positional one. The contract case seeds a
        second person's credits for exactly that reason.

        One call per person and **not** an N+1: `PeopleProvider` emits 0-2
        rows (PRD 06's own table), so this is at most two statements. The
        unbounded question -- *which* people -- is
        `PersonRepository.list_recurring_for_user`, in one statement, which is
        where the fan-out actually lived.

        Ordered by `billing_order` nulls last then `title_id`, so a person's
        headline roles lead and two reads agree.
        """
