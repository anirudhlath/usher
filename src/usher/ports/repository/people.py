"""People and credits, which are two ports over one re-derivation.

Implemented by `usher.db.repositories.people`'s
`PostgresPersonRepository` and `PostgresCreditRepository` -- the pair that
makes the mirror module-for-module rather than port-for-port.
"""

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
    # **The most recent watch that credits them, and it is a tiebreak the row
    # cannot compute for itself.** Two directors at four titles each, one from
    # last month and one from 2019, is the front matter's opening failure with
    # a person's name on it -- a beautifully constructed row about a film
    # watched three years ago -- and `watched_title_count` alone cannot
    # separate them, so "whatever the aggregate returned" would decide.
    #
    # Nullable, because `watch_states.last_played_at` is (ADR-0014: a walk's
    # listing cannot determine it), and a person known only through undatable
    # states is a real state rather than a bug. Readers sort it last.
    last_watched_at: AwareDatetime | None


class PersonRepository(ABC):
    """Persistence for canonical people (PRD 02's `Person`).

    PRD 02: *"People are canonical entities, so 'more from this director' is a
    join rather than a string match."* Identity is Usher's own UUIDv7;
    `tmdb_id` is a nullable indexed attribute and never identity (ADR-0003),
    which is what makes **two directors who share a name two rows**. An
    implementation that dedupes on `name` is the first wrong implementation
    this port's contract suite exists to kill.

    Same session ownership as every other repository here: methods flush so
    conflicts surface immediately, none commits.

    **No `get(person_id)`.** Nothing in M7 reads one person by id --
    `GET /people/{id}` is M9's (PRD 07's endpoint table, boundary call 6) --
    and the only thing a row needs is a name, which `RecurringPerson`
    carries. `SearchIndex`' settled argument applies unchanged: *"A port
    method whose only test is its own test is a liability, and the failure
    mode of a rare path is that it has rotted by the time somebody needs
    it."*
    """

    @abstractmethod
    async def upsert_many(self, people: Sequence[Person]) -> BulkWriteResult:
        """Insert or update, keyed on `tmdb_id`.

        **Keyed on `tmdb_id`, not on `Person.id`.** The derivation mints a
        fresh UUIDv7 per sighting exactly as ingest does for seasons, so an
        upsert keyed on the id inserts a duplicate row per pass and the
        catalog grows a copy of every actor every time `usher derive` runs.

        **Never overwrites a non-null field with a null one.** This is not
        the defensive version of the `COALESCE` rule -- it is required, and
        the payload says why: a `created_by[]` entry carries no
        `known_for_department` while a `credits.cast[]` entry does, so the
        same person arrives with it and without it *inside one derivation
        pass*. An unconditional assignment blanks an actor's department the
        moment they also created a series, silently, on a field
        `PeopleProvider` reads.

        A batch may contain the same `tmdb_id` many times -- one derivation
        pass spans many titles and a working actor is on several of them -- so
        an implementation deduplicates rather than assuming. The last such row
        wins.

        A person with a `None` `tmdb_id` is inserted, never merged: the
        uniqueness index is partial and NULL never collides with NULL. Two
        such people are two rows, which is the only answer available when
        there is no identity to compare.
        """

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
        """How many people the catalog holds. `usher derive`'s report, and the
        one number that tells an operator a derivation ran at all."""

    @abstractmethod
    async def list_recurring_for_user(
        self, user_id: uuid.UUID, *, min_titles: int = 2, limit: int = 10
    ) -> list[RecurringPerson]:
        """People who recur across the titles this user has played, most
        first.

        **This is the method the N+1 hazard is about.** The obvious shape is
        "list the user's watch states, then `list_for_title` each one" -- one
        statement per watched title, against a history the one measured
        deployment sizes at up to 1,126,789 states. This answers it in one
        statement instead, and the port exists in this shape so a provider
        cannot express the other one.

        **`watched_title_count` counts distinct titles, never credits.** A
        person credited twice on one film reads as two titles otherwise, and a
        one-film person out-ranks a four-film one -- a row that is populated,
        ordered, plausible and wrong, which is the failure mode this milestone
        exists to refuse.

        **Episode watch state counts toward its series**, and an
        implementation reading only `watch_states.title_id` misses it. 999,827
        of the one measured source's 1,126,674 items are episodes, so a
        People row built from `title_id` alone is a row about films on a
        library that is mostly television. Twelve watched episodes of one
        series are **one** title in this count, which is the other half of why
        the count is distinct.

        `min_titles` defaults to 2 because "recurring" is PRD 06's word and
        one appearance is not a recurrence. `played` is the predicate, not
        "has a watch state": a row with `played = false, position_seconds = 0`
        is a state a sync created, not something the user watched.

        Ordered by count descending, then by `last_watched_at` descending
        with nulls last, then by `person_id` so two reads of one catalog
        agree -- the `list_for`/`nearest_for` rule with the recency key the
        row above it needs.

        **`billing_order` is deliberately not here and not filterable.** The
        grouping is `(person_id, name, kind, job)`, which is what makes a
        person credited twice on one film one row rather than two, and a
        billing bound would have to be applied *before* that grouping to mean
        anything. So "top billed" is not expressible through this port; what
        is expressible is `kind` and `job`, which is what `PeopleProvider`
        filters on. `mapping._CAST_LIMIT` already bounds a title's stored cast
        at 50, so the population is bounded even though the billing rank is
        not readable. Recorded rather than worked around.
        """


class CreditRepository(ABC):
    """Persistence for `credits` -- the join that makes "more from this
    director" a lookup.

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
        """Replace every stored credit for `title_ids` with `credits`, and
        write `titles.credit_names` for the same scope in the same call.

        **`credit_names` is not a second write and may not become one.** It is
        weight class B's input -- `credits` projected to names and truncated
        to a ranking constant -- and a stored generated column cannot reach
        another table, which is the whole reason it exists as a column at all
        (boundary call 5, measured in migration `fe1d40c8b7a3`). The array and
        the table are two spellings of one fact: split them across two calls
        or two transactions and they diverge, and the symptom is a full-text
        hit on a name `credits` no longer holds. Keyword-only and **without a
        default**, so a caller cannot forget it.

        **Scoped by `title_ids`, exactly as the delete is.** A title in scope
        but absent from the mapping has its array emptied rather than left
        alone -- same argument, same sentence: a title whose credits all
        disappeared upstream contributes no rows, so a scope derived from the
        rows leaves its stale names in place forever.

        Order within each sequence is the ranking and is preserved. It is
        top-billed first, which is what makes the class-B lexemes the ones a
        viewer would search for.

        **`title_ids` is passed separately from the rows and that is not
        redundancy** -- `TitleNeighborRepository.replace`'s argument, arriving
        at a second table for the same reason. A title whose credits all
        disappeared upstream contributes no rows at all, so an implementation
        deriving the delete's scope from `credits` deletes nothing for it and
        leaves its stale credits in place through every future derivation. It
        is the one row shape a re-derivation cannot repair.

        Returns the number of credit rows written, which is what makes
        `usher derive`'s report a number rather than a reassurance.

        A `title_id` or `person_id` naming a row that does not exist raises
        `RepositoryConflict` rather than a raw storage error, and leaves the
        session usable for the caller's other pending work -- the derivation
        commits a batch of credits together with its job checkpoint.

        Idempotent by construction: PRD 08's redelivery rule, and the job
        queue *will* redeliver. Running it twice with the same arguments
        produces the same rows and the same count.

        A batch carrying the same `tmdb_credit_id` twice keeps one of them;
        the partial unique index is what makes a *scoping* bug raise instead
        of doubling a title's cast, and tolerating an in-batch duplicate is
        what stops a payload that lists a credit twice from failing the whole
        derivation.
        """

    @abstractmethod
    async def list_for_title(
        self, title_id: uuid.UUID, *, kind: CreditKind | None = None, limit: int = 20
    ) -> list[CreditedPerson]:
        """One title's credits, top-billed first, with the person joined in.

        **Ordered by `billing_order`, nulls last, ties broken by
        `person_id`.** "Top billed" is what PRD 06's People row means and what
        a client's cast list renders; an implementation that drops
        `billing_order` returns provider-JSON order, which is *usually* right
        and is therefore invisible until it is not. That is the front matter's
        second named wrong implementation for this suite.

        **`kind` filters and may not be ignored.** Asking for cast and
        receiving crew is the third named wrong implementation, and it has the
        property that makes this milestone dangerous: the answer is populated,
        correctly shaped, and about the wrong people. `None` means both, in
        one ordering.

        Called by `usher derive`'s report, and it is the surface every
        `replace_for_titles` case asserts through -- a write port with no read
        can only assert on counts, which cannot tell a correct row from a
        wrong one. M9's `GET /titles/{id}` cast block is its first
        client-facing caller.
        """

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
