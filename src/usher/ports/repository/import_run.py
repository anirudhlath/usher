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

    **A dataset has at most one holder, and only the holder writes its row.** The hold
    is taken by `start()` or `hold()` and given up by `release()` or by the holder's
    connection ending, so a dead holder's checkpoint can be taken over. A holder is
    another process, or another repository of this one.

    **A phase reading a dataset holds it too, shared** (`hold_for_reading()`): readers
    share it with each other and exclude every holder, so no import of it starts while
    a phase joins against it, and no phase starts reading it mid-import. A repository's
    reads exclude its own holds as well -- the two are held on different connections.
    """

    @abstractmethod
    async def start(self, dataset: str, revision: str) -> ImportRun:
        """Hold `dataset` as `hold()` does, then begin or resume its run.

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
    async def hold(self, dataset: str) -> None:
        """Hold `dataset`, or raise `RepositoryConflict` and touch nothing.

        Refused while another holder has it, or any reader, this repository's own reads
        included. A hold this repository already has is confirmed, and one found lost is
        taken again.
        """

    @abstractmethod
    async def release(self, dataset: str) -> None:
        """Give up this repository's hold on `dataset`; a no-op when there is none."""

    @abstractmethod
    async def hold_for_reading(self, dataset: str) -> bool:
        """Hold `dataset` shared, for a phase that reads it; `False` while it is held.

        Granted beside other readers and refused while any holder has it, this
        repository's own included; a refused read takes nothing. Reading a dataset this
        repository already reads is the one read. Kept until `release_reads()`.
        """

    @abstractmethod
    async def release_reads(self) -> None:
        """Give up every read this repository holds; a no-op when there are none."""

    @abstractmethod
    async def touch(self, dataset: str) -> None:
        """Confirm this repository still holds `dataset` and its reads, then move a heartbeat.

        A hold or a read that is gone -- its connection ended, by `idle_session_timeout`,
        a proxy's idle cut or a server restart -- raises `RepositoryConflict` and is
        dropped, so the next `hold()` takes a fresh one; a read lost drops every read.
        Only a `RUNNING` row's `heartbeat_at` is written. Flushes, never commits.
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
