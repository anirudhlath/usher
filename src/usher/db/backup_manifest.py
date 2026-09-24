"""Which tables a backup carries, which it refuses to, and why -- per table."""

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
    """What restore does with the table, read off `BackupEntry.restore`."""

    WHOLE = "whole"
    """Every column of every row the artifact carries."""

    MERGE = "merge"
    """Only the entry's `columns`, and only where the target's is `NULL`."""

    NEVER = "never"
    """Restore does not write this table."""


#: Which rule each class takes, defined once: `BackupEntry.restore` reads it, so an
#: entry built elsewhere -- a service, a test fixture -- cannot carry a rule that
#: disagrees with its class.
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

    `reason` is not decoration: the failure this module exists to prevent is a
    table reclassified by someone who did not know why it was where it was.

    Invariants are enforced at construction rather than by a case over
    `MANIFEST`, because an entry built elsewhere is one this file never sees --
    and a `PARTIAL` entry naming no column means restore silently merges
    nothing.
    """

    kind: BackupClass
    reason: str
    rebuilt_by: str = ""
    """`REBUILDABLE` only: the command(s) that reproduce it, `" then "`-joined."""

    columns: tuple[str, ...] = ()
    """`PARTIAL` only: the operator-authored columns restore merges."""

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a backup entry needs a reason; the class alone is the prose table")
        wants_columns = self.kind is BackupClass.PARTIAL
        if bool(self.columns) is not wants_columns:
            raise ValueError(f"{self.kind} entries name columns iff they are PARTIAL")
        wants_command = self.kind is BackupClass.REBUILDABLE
        if bool(self.rebuilt_by.strip()) is not wants_command:
            raise ValueError(f"{self.kind} entries name a rebuild command iff they are REBUILDABLE")

    @property
    def restore(self) -> RestoreRule:
        """What restore does with this table, which the class alone decides.

        Derived rather than stored so the two cannot disagree: a field would
        be a second spelling of `_RULE_FOR` that every construction site --
        including the fixtures this file never sees -- has to keep true.
        """
        return _RULE_FOR[self.kind]

    @property
    def rebuild_commands(self) -> tuple[str, ...]:
        """`rebuilt_by` split into steps, in run order; empty for every class but `REBUILDABLE`."""
        return tuple(step for step in self.rebuilt_by.split(REBUILD_STEP) if step.strip())


def _precious(reason: str) -> BackupEntry:
    return BackupEntry(kind=BackupClass.PRECIOUS, reason=reason)


def _rebuildable(reason: str, rebuilt_by: str) -> BackupEntry:
    return BackupEntry(
        kind=BackupClass.REBUILDABLE,
        reason=reason,
        rebuilt_by=rebuilt_by,
    )


def _partial(reason: str, columns: tuple[str, ...]) -> BackupEntry:
    return BackupEntry(
        kind=BackupClass.PARTIAL,
        reason=reason,
        columns=columns,
    )


def _schema(reason: str) -> BackupEntry:
    return BackupEntry(kind=BackupClass.SCHEMA, reason=reason)


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
            "0 rows here, 16 kB. PRD 08: 'NO. From nothing.' A spend ledger, the only "
            "record that money was spent, and no OpenAI-compatible endpoint offers a "
            "per-key call history to read it back from"
        ),
        "row_provider_settings": _precious(
            "0 rows. Operator-authored, like source config: no importer restores a human's "
            "choice. Keyed by `slug_prefix`, a string -- the one precious table with no "
            "UUID in it at all"
        ),
        "search_queries": _precious(
            "9 rows. Unreproducible: a record of what a household typed and what it then "
            "played. PRD 09 keeps query expansion 'post-v1 unless `search_queries` "
            "supplies a real evaluation set', a plan that depends on this table surviving"
        ),
        # --- partial: rebuildable but for named columns ---------------------
        "media_items": _partial(
            "One row per item the source holds, so on a real deployment this is large. "
            "Every column is rebuilt by the next source walk except the two links, "
            "which PRD 08 carries alone. No provenance column tells an operator's "
            "match from an automatic one, so all links are carried and K4 writes only "
            "where the target's is NULL",
            ("title_id", "episode_id"),
        ),
        # --- rebuildable ----------------------------------------------------
        "titles": _rebuildable(
            "The catalog. **Four writers, which is why the command is `--phase all` "
            "rather than a subset**: `imdb` brings the skeleton and, because it runs "
            "basics then ratings, `imdb_average_rating` and `imdb_num_votes` too "
            "(`--phase ratings` is an alias for that second half -- a refresh, not a "
            "rebuild); `credit-names` brings `credit_names`; `crosswalk` brings "
            "`tmdb_id`, `tvdb_id` and `tmdb_popularity`, so popularity has a second "
            "writer that touches neither rating column; and enrichment brings the rest "
            "of the TMDb columns. The phases have ordering constraints between them "
            "(`credit-names` before anything that enriches; `tmdb-ids` before "
            "`crosswalk`), so naming a subset would be a second copy of "
            "`FULL_SEQUENCE`. Enriching past what a walk matches is "
            "`scripts/enqueue_tier_enrichment.py`, not a command, so this string does "
            "not claim it",
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
            "Vectors plus an HNSW index, which is half the relation's size. The "
            "backfill is an hour or two of GPU-free CPU work",
            "index --backfill then work",
        ),
        "title_neighbors": _rebuildable(
            "The largest relation in this database, and still rebuildable. Budget an "
            "overnight run: roughly twice the embedding backfill above it, and it needs "
            "that backfill to have finished first",
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
            "⚠️ The third-largest relation here, and the closest call in this "
            "manifest. Rebuildable only by re-fetching every title from TMDb, which is "
            "hours of live requests. Not carried, because it would take the artifact "
            "from kilobytes to a gigabyte an operator will not keep, and because it is "
            "third-party payloads verbatim. See the module docstring",
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
            "0 rows. One completion per household. PRD 02: 'Rebuildable, not restorable' "
            "-- no re-run reproduces the same rows, so 'rebuildable' here means a screen "
            "appears, not that the screen comes back",
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

    One accessor rather than the same comprehension in the three services that
    want it -- and computed on call rather than cached in a second mapping,
    which would be a thing to keep in step with `MANIFEST` and therefore a
    thing to forget.
    """
    return tuple(table for table, entry in MANIFEST.items() if entry.kind is kind)
