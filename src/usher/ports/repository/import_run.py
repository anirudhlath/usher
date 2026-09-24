"""Import runs -- the resumable checkpoint a bulk phase records against."""

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
        """Take hold of `dataset`'s import, then begin or resume its run.

        **One holder at a time.** While another repository holds `dataset` -- another
        process, or another session of this one -- this raises `RepositoryConflict` and
        touches nothing. The hold lasts until `release()` or until the holder's
        connection ends, so a dead holder's checkpoint can be taken over; a second
        `start()` by the holder itself is not refused.

        The returned run keeps the stored cursor when `revision` matches it and starts
        from zero when it does not -- an upstream snapshot change restarts the import
        rather than splicing two snapshots. It is `RUNNING`, with `error` and
        `finished_at` cleared and a fresh `heartbeat_at`, and it has been persisted --
        except over a `COMPLETED` checkpoint, which keeps its status, revision, cursor
        and `finished_at` until the returned run is first saved, with only its `error`
        cleared and its heartbeat moved. An attempt that saves no batch therefore leaves
        the completed import standing.
        """

    @abstractmethod
    async def release(self, dataset: str) -> None:
        """Give up the hold `start()` took on `dataset`; a no-op when there is none."""

    @abstractmethod
    async def touch(self, dataset: str) -> None:
        """Move `dataset`'s `heartbeat_at` to now if its checkpoint is `RUNNING`.

        Nothing else is written. Flushes, never commits.
        """

    @abstractmethod
    async def note_failure(self, dataset: str, error: str) -> ImportRun:
        """Record `error` against `dataset`'s checkpoint without taking it over.

        For a failure before `start()`. A stored checkpoint keeps its status and cursor
        and gains the error, and its heartbeat moves unless it is `RUNNING`, whose
        heartbeat belongs to whoever is importing it. With none stored, a `FAILED`
        checkpoint at position 0 is created. Returns the checkpoint as stored. Flushes,
        never commits.
        """

    @abstractmethod
    async def save(self, run: ImportRun) -> None:
        """Persist a run's progress.

        Flushes, never commits.
        """

    @abstractmethod
    async def get(self, dataset: str) -> ImportRun | None:
        """The stored run for `dataset`, or None if it has never run."""

    @abstractmethod
    async def list_runs(self) -> list[ImportRun]:
        """Every stored run, most recent activity first."""
