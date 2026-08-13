"""Import runs -- the resumable checkpoint a bulk phase records against.

Implemented by
`usher.db.repositories.import_run.PostgresImportRunRepository`.
"""

from abc import ABC, abstractmethod

from usher.domain.bootstrap import ImportRun

__all__ = [
    "ImportRunRepository",
]


class ImportRunRepository(ABC):
    """Checkpoint storage for resumable bulk imports.

    One row per dataset, holding the cursor its last committed batch
    produced. `TitleRepository`'s session/transaction ownership applies here
    too, and matters more: `save` must be flushed inside the *same*
    transaction as the batch it describes, or a crash between the two either
    loses work or claims work that was rolled back.
    """

    @abstractmethod
    async def start(self, dataset: str, revision: str) -> ImportRun:
        """Begin or resume a run for `dataset`.

        Returns the run with its cursor fields preserved when `revision`
        matches what was stored, and reset to zero when it does not — an
        upstream snapshot change restarts the import rather than splicing
        two snapshots. Either way the returned run is `RUNNING` with `error`
        and `finished_at` cleared, and it has already been persisted.
        """

    @abstractmethod
    async def save(self, run: ImportRun) -> None:
        """Persist a run's progress. Flushes, never commits.

        Raises `RepositoryConflict` if another row already claims this
        run's `dataset` — two processes bootstrapping the same dataset at
        once is an operator mistake, and it must surface as a port error
        rather than a raw storage exception (ADR-0009).

        Whether the *session* remains usable for further work after a
        caught `RepositoryConflict` is deliberately left to the
        implementation, not promised here — contrast `TitleRepository.add`/
        `update`, which use a `SAVEPOINT` specifically so it does.
        `PostgresImportRunRepository` rolls back the whole transaction
        instead of using a SAVEPOINT (see its own module docstring): unlike
        `TitleRepository`'s general-purpose callers, its one caller,
        `BootstrapService`, never has other work pending on the session at
        this point worth a SAVEPOINT's extra round trip to protect. The
        session *does* stay usable afterward, deliberately —
        `BootstrapService.import_dataset`'s except handler continues on
        this same session to record the failure as a durable `ImportRun`,
        which is exactly why the rollback is there rather than skipped.
        """

    @abstractmethod
    async def get(self, dataset: str) -> ImportRun | None:
        """The stored run for `dataset`, or None if it has never run."""

    @abstractmethod
    async def list_runs(self) -> list[ImportRun]:
        """Every stored run, most recent activity first — what the CLI's
        `bootstrap-status` prints."""
