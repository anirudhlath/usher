"""Which tables a backup carries, which it refuses to, and why -- per table.

PRD 08's `### Backup -- the asymmetry is the point` stated this split as
prose in a two-column table and then spent fifty lines qualifying it.
**The prose drifted twice, and the drift is the argument for this
module.** M9 added four tables and the section was updated for none of
them: two of the four land in the correct column anyway, because the left
column's *"search index, cached images"* happens to describe
`title_search_names` and `images` -- a phrase written in commit `860b086`
(2026-07-28, the original PRD 07-08 landing, before M1 shipped), so it
predates both tables by nine milestones and classifies them right by
accident. The other two are the ones that were wrong.
`row_provider_settings` went unlisted for a milestone while a ⚠️ four
lines below the table said row provider enable/disable *"belongs in the
precious column the day it exists"*, and `search_queries` -- the household's
own record of what it typed and what it then played, which PRD 09 scopes
issues #15/#16 against -- was never classified at all. **A prose table is
updated by whoever remembers; the two tables nobody remembered are the two
that were wrong.**

**Why `usher.db` and not `usher.services`.** This is knowledge about *this
schema*: table names, column names, and what re-creates each one. A module
under `usher.services` holding a table name would put the database's
vocabulary in the layer that is supposed to be ignorant of it, and
`pyproject.toml`'s third import contract (*"db is driven, not driving"*)
is the mechanical statement of that. The services that will read this
(K3's `usher backup`, K4's `usher restore`) reach it the way they reach
every other `usher.db` fact -- through a repository, from the composition
root.

## The four classes, because the spec's binary split is one class short

`media_items` is the table that proves it. PRD 08 lists *"manual unmatched
resolutions"* as precious; there is no such table. What that names is two
**columns** -- `media_items.title_id` and `media_items.episode_id`, written
by `PostgresMediaItemRepository.attach_title` and reached by the resolve
route in `api/routers/unmatched.py`. Every other column of `media_items` is
rebuilt by the next source walk, and on the household this project measures
that is 1,126,789 rows to carry for the sake of a handful of links.

| class | meaning | restore behaviour |
|---|---|---|
| `PRECIOUS` | no importer and no derivation reproduces it | carried whole |
| `REBUILDABLE` | a documented command reproduces it, at a stated cost | never carried |
| `PARTIAL` | rebuildable except for named operator-authored columns | only those columns, merged |
| `SCHEMA` | Alembic's own bookkeeping | read as a stamp, never written |

⚠️ **`media_items` carries no provenance column, so "manual" is not a
distinction the schema can express.** `services/handlers.py`'s automatic
match handler calls the *same* `attach_title` as the operator's route. A
backup can carry all `(source_id, external_id) -> title` links or none; it
cannot carry only the manual ones. Carrying all of them is the right call
because the harm is asymmetric: a link the match ladder would have
re-derived is re-derived to the same answer, and a link it would not
re-derive is exactly the operator's judgement. K4's merge rule -- write only
where the target's link is `NULL` -- is what keeps that safe, which is what
`RestoreRule.MERGE` names.

## Three rulings this module makes, because PRD 08 leaves all three open

**`genome_scores` and `genome_tags` stay rebuildable, and the reason is
licensing rather than cost.** PRD 08 is right that they are rebuildable
*only from upstream* and that GroupLens can withdraw `ml-latest.zip`, at
which point they are not rebuildable at all -- but the PRD already answers
it: *"a dump of it is a redistribution of MovieLens data ... and still not
something this project's own rule 1 does."*
`tests/unit/test_no_third_party_data.py` enforces that rule mechanically
over `src/`, and a backup artifact holding 15,565 MovieLens vectors is
exactly the object it exists to prevent, one directory over. Not a
judgement about probability; the same refusal the repository already makes.

**`raw_payloads` stays rebuildable, and it is the closest call in the
table.** Against carrying it: it is the third-largest relation in this
database, and third-party TMDb payloads verbatim, so the rule-1 argument
applies with more force than it does to the genome. For carrying it: 1.98 h
and 130,334 requests against a server this project does not own, which is
the Phase-1 politeness argument pointing the other way. The ruling is
rebuildable with the cost stated, and **`usher backup --include-payloads`
is deliberately not built** -- a flag that makes the artifact redistribute
TMDb payloads is a licensing decision, not an operator convenience.

**`user_taste`'s classification does not move either way.** PRD 08 once
said *"`TasteService.centroid` has no caller in `src/`"*; that has been
false since M8 -- `CurationPoolService` calls it, and the guard immediately
above that call exists because it writes -- and the PRD now says so. The
table is empty on this deployment because no curation run with an embedder
configured has touched it, not because nothing can write it; and a mean
over embeddings is rebuildable whoever computes it.

## Three conventions in the data below

**`rebuilt_by` names CLI subcommands and omits the `usher` prefix**, so
each step is something `usher.cli.build_parser()` can parse. Where a
rebuild takes more than one command they are joined by `" then "` in run
order, and `rebuild_commands` splits them.
`test_every_rebuild_step_is_a_command_the_cli_really_accepts` feeds **every
step** through `parse_args`, so an unbuilt command, an unknown flag and a
`--phase` outside `BootstrapPhase`'s closed vocabulary are all reds -- not
only the ones in first position. CLAUDE.md's standing rule: do not invent
commands for tooling that does not exist yet.

**Row counts and relation sizes are `pg_stat_user_tables.n_live_tup` and
`pg_total_relation_size` on `usher-postgres-1`, read 2026-08-13.** Sizes
are therefore *totals* -- heap plus TOAST plus every index -- which is why
`title_embeddings` is one number here and appears in PRD 08's resource
envelope as "707 MB of relation and 340 MB of index", the index being a
component of the total rather than something to add to it. They are here to
make the asymmetry legible (3.2 M neighbour rows on one side of the split,
one `users` row on the other) and they are dated because they drift: `m10a`
was an `ALTER` over `titles` and this database has not been `VACUUM
FULL`ed since.

⚠️ **Durations are *not* from that reading and carry their own
provenance**, which is where this file's one shipped error was -- a
`title_neighbors` figure copied from a draft that the same plan's own
corrections table refuted. Each is named at its entry: the embedding
backfill (105.9 min) is a 2026-08-13 run recorded in PRD 08's resource
envelope, and the neighbour walk is the most recently *completed* one --
97.3 ms/seed over 12,884 s, 2026-08-19, read off `title_neighbors`' own
`min`/`max(computed_at)` rather than timed by a harness, superseding the
91.7 ms/seed over 11,981 s that
`.claude/rules/search-and-embeddings.md` records for 2026-08-13.
⚠️ **A duration here is what an operator budgets a recovery against, so it
tracks the newest completed run** -- which is the opposite of how a
*superseded* figure is treated in a reason string below, where the point is
the record of what was wrong. The payload crawl (1.98 h) is
M9's S3 against the live TMDb API. **No revision label appears in any
reason string**, for the same reason: `alembic_version` stamped `m09f`
when this was designed and `m10a` when it landed.

**Citations name symbols, not line numbers.** PRD 08 carried
`curation_pool.py:173` for a call that is on line 174, and that off-by-one
is the small version of the drift this whole module exists to stop.
"""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

#: Alembic's own bookkeeping table. Named here rather than inlined because
#: it is the one table in this manifest with no `Table` object anywhere in
#: `src/` -- Alembic creates it from `alembic/env.py` -- so `Base.metadata`
#: cannot see it and every check that compares the two has to name it.
ALEMBIC_VERSION_TABLE: Final = "alembic_version"

#: What separates one rebuild step from the next inside `rebuilt_by`.
REBUILD_STEP: Final = " then "


class BackupClass(StrEnum):
    """What kind of thing a table holds, from a backup's point of view."""

    PRECIOUS = "precious"
    """No importer and no derivation reproduces it."""

    REBUILDABLE = "rebuildable"
    """A command reproduces it, at the cost the entry's `reason` states."""

    PARTIAL = "partial"
    """Rebuildable except for the operator-authored columns it names."""

    SCHEMA = "schema"
    """Alembic's bookkeeping -- a stamp to compare, never a row to write."""


class RestoreRule(StrEnum):
    """What restore does with the table. Stored per entry rather than derived
    from the class at read time, so K4 reads one field; `__post_init__`
    refuses an entry whose rule disagrees with its class.
    """

    WHOLE = "whole"
    """Every column of every row the artifact carries."""

    MERGE = "merge"
    """Only the entry's `columns`, and only where the target's is `NULL`."""

    NEVER = "never"
    """Restore does not write this table."""


#: Which rule each class takes. One mapping rather than a rule repeated at
#: 29 call sites, and `__post_init__` is what makes it binding on the
#: entries K3 and the test fixtures build that this file never sees.
_RULE_FOR: Final[MappingProxyType[BackupClass, RestoreRule]] = MappingProxyType(
    {
        BackupClass.PRECIOUS: RestoreRule.WHOLE,
        BackupClass.PARTIAL: RestoreRule.MERGE,
        BackupClass.REBUILDABLE: RestoreRule.NEVER,
        BackupClass.SCHEMA: RestoreRule.NEVER,
    }
)


@dataclass(frozen=True, slots=True)
class BackupEntry:
    """One table's classification, with the argument for it attached.

    `reason` is not decoration. The failure this module exists to prevent
    is a table reclassified by someone who did not know why it was where it
    was, and a class with no reason beside it is the prose table again.

    **The invariants are enforced at construction rather than by a case over
    `MANIFEST`.** A test can only notice for the 29 entries that exist here;
    K3's report, K4's merge and every fixture that builds an entry are
    constructions this file never sees, and a `PARTIAL` entry naming no
    column would mean restore silently merging nothing.
    """

    kind: BackupClass
    restore: RestoreRule
    reason: str
    rebuilt_by: str = ""
    """`REBUILDABLE` only: the command(s) that reproduce it, `" then "`-joined."""

    columns: tuple[str, ...] = ()
    """`PARTIAL` only: the operator-authored columns restore merges."""

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a backup entry needs a reason; the class alone is the prose table")
        if self.restore is not _RULE_FOR[self.kind]:
            raise ValueError(
                f"{self.kind} restores as {_RULE_FOR[self.kind]}, not {self.restore}",
            )
        wants_columns = self.kind is BackupClass.PARTIAL
        if bool(self.columns) is not wants_columns:
            raise ValueError(f"{self.kind} entries name columns iff they are PARTIAL")
        wants_command = self.kind is BackupClass.REBUILDABLE
        if bool(self.rebuilt_by.strip()) is not wants_command:
            raise ValueError(f"{self.kind} entries name a rebuild command iff they are REBUILDABLE")

    @property
    def rebuild_commands(self) -> tuple[str, ...]:
        """`rebuilt_by` split into its steps, in run order; empty when there
        is no rebuild command, which is every class but `REBUILDABLE`.
        """
        return tuple(step for step in self.rebuilt_by.split(REBUILD_STEP) if step.strip())


def _precious(reason: str) -> BackupEntry:
    return BackupEntry(kind=BackupClass.PRECIOUS, restore=RestoreRule.WHOLE, reason=reason)


def _rebuildable(reason: str, rebuilt_by: str) -> BackupEntry:
    return BackupEntry(
        kind=BackupClass.REBUILDABLE,
        restore=RestoreRule.NEVER,
        reason=reason,
        rebuilt_by=rebuilt_by,
    )


def _partial(reason: str, columns: tuple[str, ...]) -> BackupEntry:
    return BackupEntry(
        kind=BackupClass.PARTIAL,
        restore=RestoreRule.MERGE,
        reason=reason,
        columns=columns,
    )


def _schema(reason: str) -> BackupEntry:
    return BackupEntry(kind=BackupClass.SCHEMA, restore=RestoreRule.NEVER, reason=reason)


#: Every table in the live schema, classified. Exhaustiveness is enforced
#: from both ends -- against `Base.metadata` in `tests/unit`, and against a
#: migrated database's `information_schema` in `tests/integration`, which is
#: the only one of the two that can see `alembic_version`.
MANIFEST: Final[MappingProxyType[str, BackupEntry]] = MappingProxyType(
    {
        # --- precious: nothing rebuilds these ------------------------------
        "users": _precious(
            "1 row. Nothing recreates a household. `uq_users_name` is its natural key"
        ),
        "sources": _precious("1 row. Operator-authored: a URL and a name"),
        "source_credentials": _precious(
            "1 row, 48 kB. Ciphertext under `USHER_SECRET_KEY`, carried as ciphertext -- "
            "backup holds no key and does not re-encrypt, so an artifact restored into a "
            "deployment with a different key restores a credential nothing can decrypt"
        ),
        "watch_states": _precious(
            "0 rows here and the load-bearing table on a real deployment. The one thing a "
            "re-walk can partly restore, from the source -- and only while that source "
            "still exists"
        ),
        "llm_calls": _precious(
            "0 rows here, 16 kB. PRD 08: 'the first thing in this project that is not "
            "rebuildable from anything, at any price'. A spend ledger, and no "
            "OpenAI-compatible endpoint offers a per-key call history to read it back from"
        ),
        "row_provider_settings": _precious(
            "0 rows. Operator-authored, like source config: no importer restores a human's "
            "choice. PRD 08 said this table belonged here the day it existed and then went "
            "a milestone without listing it. Keyed by `slug_prefix`, a string -- the one "
            "precious table with no UUID in it at all"
        ),
        "search_queries": _precious(
            "9 rows. Unclassified by PRD 08 entirely, and unreproducible: a record of what "
            "a household typed and what it then played. PRD 09 scopes issues #15/#16 "
            "'post-v1 unless M9's search_queries supplies a real evaluation set', a plan "
            "that depends on this table surviving"
        ),
        # --- partial: rebuildable but for named columns ---------------------
        "media_items": _partial(
            "180 rows here, 1,126,789 on the household this project measures. Every "
            "column is rebuilt by the next source walk except the two links -- which "
            "is what PRD 08's 'manual unmatched resolutions' actually names, since "
            "there is no such table. No provenance column exists, so all links are "
            "carried and K4 writes only where the target's is NULL",
            ("title_id", "episode_id"),
        ),
        # --- rebuildable ----------------------------------------------------
        "titles": _rebuildable(
            "1,272,401 rows, 1050 MB. **Four writers, which is why the command is "
            "`--phase all` rather than a subset**: `imdb` brings the skeleton and, since "
            "it runs basics then ratings, ADR-0040's `imdb_average_rating` and "
            "`imdb_num_votes` (`--phase ratings` is an alias for that second half -- a "
            "refresh, not a rebuild); `credit-names` brings `credit_names`; `crosswalk` "
            "brings `tmdb_id`, `tvdb_id` and `tmdb_popularity`, which is ADR-0040's "
            "refutation 3 -- popularity has a second writer in `link_crosswalk` that "
            "touches neither rating column; and enrichment brings the rest of the TMDb "
            "columns. The phases have ordering constraints between them (`credit-names` "
            "before anything that enriches; `tmdb-ids` before `crosswalk`), so naming a "
            "subset would be a second copy of `FULL_SEQUENCE`. ⚠️ Enriching past what a "
            "walk matches -- M9's 130,647-title priority tier -- is "
            "`scripts/enqueue_tier_enrichment.py`, not a command, so this string does not "
            "claim it",
            "bootstrap --phase all then sync then work",
        ),
        "seasons": _rebuildable(
            "0 rows here. TMDb enrichment, via `append_to_response=season/N`",
            "sync then work",
        ),
        "episodes": _rebuildable(
            "0 rows here. TMDb enrichment, on the same call as `seasons`",
            "sync then work",
        ),
        "people": _rebuildable(
            "887,638 rows, 136 MB. Derived from `raw_payloads` with no network call -- "
            "M4's boundary call 2 paying off: the payload cache is the backup",
            "derive --backfill",
        ),
        "credits": _rebuildable(
            "2,878,307 rows, 706 MB. The same derivation, no network call",
            "derive --backfill",
        ),
        "collections": _rebuildable(
            "5,240 rows, 848 kB. The same derivation, no network call",
            "derive --backfill",
        ),
        "images": _rebuildable(
            "537 rows. The same derivation, M9's C3 -- the rows carry a path, not bytes, "
            "so re-deriving them opens no socket",
            "derive --backfill",
        ),
        "title_embeddings": _rebuildable(
            "130,723 rows, 707 MB (of which 340 MB is the HNSW index). The backfill of "
            "130,720 titles measured 105.9 min on 2026-08-13",
            "index --backfill then work",
        ),
        "title_neighbors": _rebuildable(
            "3,256,676 rows, 1140 MB -- the largest relation in this database, and still "
            "rebuildable. The most recent completed walk, 2026-08-19: 132,442 seeds, "
            "3,311,050 rows, 12,884 s at **97.3 ms/seed = 3.58 h** -- budget against this "
            "one. It supersedes 2026-08-13's 130,720 seeds / 11,981 s / 91.7 ms/seed = "
            "3.33 h, which is a real run over a 1.3% smaller population and is what "
            "established the shape. ⚠️ Neither is 594.7 ms/seed and neither is 21.6 h: "
            "those are `m09e`'s figures and `m09f` repaired them by moving every "
            "`halfvec` column to PLAIN storage. At the true cost this is 1.9x the "
            "embedding backfill above it, not the two orders of magnitude a draft of this "
            "manifest claimed -- an overnight job, which is the conclusion that survived "
            "the number",
            "similar --rebuild",
        ),
        "title_search_names": _rebuildable(
            "1,450,117 rows, 198 MB. The alias half is the bootstrap phase; the person "
            "half is written by the credit derivation",
            "bootstrap --phase aliases then derive --backfill",
        ),
        "tmdb_ids": _rebuildable(
            "1,459,592 rows. TMDb's daily id export", "bootstrap --phase tmdb-ids"
        ),
        "id_crosswalk": _rebuildable(
            "336,769 rows. Live WDQS, so rebuildable only while Wikidata serves it",
            "bootstrap --phase crosswalk",
        ),
        "raw_payloads": _rebuildable(
            "⚠️ 130,749 rows, 995 MB -- the third-largest relation here, after "
            "`title_neighbors` and `titles`, and the closest call in this manifest. "
            "Rebuildable only from TMDb: M9's S3 measured 130,334 requests over 1.98 h to "
            "fill it. Not carried, because it would take the artifact from the kilobytes "
            "the precious set weighs on this deployment to a gigabyte an operator will not "
            "keep, and because it is third-party payloads verbatim. See the module "
            "docstring",
            "sync then work",
        ),
        "genome_scores": _rebuildable(
            "⚠️ 15,565 rows. Upstream only: re-download `ml-latest.zip`. GroupLens can "
            "withdraw it, at which point this is not rebuildable at all -- a risk accepted "
            "knowingly, because carrying it would put MovieLens data in a release artifact "
            "and that is the redistribution `tests/unit/test_no_third_party_data.py` "
            "already refuses one directory over",
            "bootstrap --phase movielens",
        ),
        "genome_tags": _rebuildable(
            "⚠️ 1,128 rows. The same archive, the same phase, the same accepted risk. "
            "Written with `genome_scores` and sharing a `genome_revision`, so the two are "
            "restored together or not at all -- and 'not at all' is this manifest's answer "
            "for both",
            "bootstrap --phase movielens",
        ),
        "curated_rows": _rebuildable(
            "0 rows. One completion per household. PRD 02: 'rebuildable (one completion) "
            "and not restorable' -- no re-run reproduces the same rows, so 'rebuildable' "
            "here means a screen appears, not that the screen comes back",
            "curate",
        ),
        "user_taste": _rebuildable(
            "0 rows. A mean over embeddings, carrying its own fingerprint "
            "(`model_name` + `source_watermark`), so a missing row is indistinguishable "
            "from a stale one and is recomputed by the same predicate rather than restored",
            "curate",
        ),
        "jobs": _rebuildable(
            "212 rows, 58 MB. A queue, not state: every row is work that will be "
            "re-enqueued by whatever noticed it was needed -- `sync` for ingest and "
            "enrichment, `index --backfill` for embeddings, `push` for the lane",
            "sync",
        ),
        "import_runs": _rebuildable(
            "6 rows. Resumption checkpoints for *this* database, and the one entry where "
            "NEVER is load-bearing rather than incidental: for every other rebuildable "
            "table 'never written' follows from 'never carried', but these rows would be "
            "harmful even if an operator carried them by hand -- they tell a resumable "
            "importer a phase is complete over an empty catalog. Recorded here rather "
            "than left to K4",
            "bootstrap",
        ),
        "sync_runs": _rebuildable("2 rows. An audit trail a new walk replaces", "sync"),
        # --- schema ---------------------------------------------------------
        ALEMBIC_VERSION_TABLE: _schema(
            "1 row. Read by backup as the artifact's stamp and compared by restore, "
            "never written by it: a restore that wrote this would claim a schema "
            "version the database does not have. It has no `Table` object in `src/` -- "
            "Alembic creates it -- which is why the coverage check reads "
            "`information_schema` rather than `Base.metadata`"
        ),
    }
)


def tables_of(kind: BackupClass) -> tuple[str, ...]:
    """The tables in one class, in manifest order.

    K3 needs the `PRECIOUS` set, K4 needs `PRECIOUS` and `PARTIAL`, and K7
    needs `PRECIOUS` to know what a re-encryption touches. One accessor
    rather than the same comprehension in three services -- and computed on
    call rather than cached in a second mapping, which would be a thing to
    keep in step with `MANIFEST` and therefore a thing to forget.
    """
    return tuple(table for table, entry in MANIFEST.items() if entry.kind is kind)
