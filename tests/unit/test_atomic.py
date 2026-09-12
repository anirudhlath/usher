import os
from pathlib import Path
from typing import IO

import pytest

from usher.atomic import scratch_beside, write_atomically


def test_the_scratch_file_is_a_hidden_sibling_and_never_the_destination(tmp_path: Path) -> None:
    """A sibling because `os.replace` is atomic only within one filesystem,
    and hidden so a process killed before its cleanup leaves something an
    operator will not mistake for the file they asked for."""
    final = tmp_path / "artifact.jsonl.gz"
    scratch = scratch_beside(final)

    assert scratch.parent == final.parent
    assert scratch != final
    assert scratch.name.startswith(".artifact.jsonl.gz.")


def test_two_writers_aimed_at_one_destination_get_different_scratch_files(
    tmp_path: Path,
) -> None:
    """The suffix is random rather than derived from the process, because two
    writers that shared a scratch name would interleave into a file that is
    neither of theirs -- and a PID is shared by a process and its own re-exec.
    """
    final = tmp_path / "artifact.jsonl.gz"

    assert scratch_beside(final) != scratch_beside(final)


def test_the_finished_bytes_are_flushed_to_disk_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename is atomic against other processes and not against a power
    cut: without the `fsync` it can leave a correctly-named file whose
    contents were never written. Asserted as an *order* rather than as "fsync
    was called", because a flush after the rename buys nothing.
    """
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def replace(src: Path, dst: Path) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)

    def body(handle: IO[bytes]) -> None:
        handle.write(b"body")

    write_atomically(tmp_path / "x", body)

    assert calls == ["fsync", "replace"]
    assert (tmp_path / "x").read_bytes() == b"body"


def test_a_body_that_raises_leaves_neither_a_destination_nor_a_scratch_file(
    tmp_path: Path,
) -> None:
    """The whole point of the scratch sibling: a failed run must cost the
    previous file nothing, and must not litter the directory it failed in.
    """
    final = tmp_path / "x"
    final.write_bytes(b"last night's copy")

    def dies(handle: IO[bytes]) -> None:
        handle.write(b"half")
        raise TypeError("row 4 of table 2")

    with pytest.raises(TypeError):
        write_atomically(final, dies)

    assert final.read_bytes() == b"last night's copy"
    assert sorted(one.name for one in tmp_path.iterdir()) == ["x"]


def test_a_cancelled_write_cleans_up_too(tmp_path: Path) -> None:
    """`BaseException` rather than `Exception`: stopping a long write with a
    keyboard interrupt is ordinary, and it must not be the one path that
    leaves a fragment behind."""
    final = tmp_path / "x"

    def interrupted(handle: IO[bytes]) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        write_atomically(final, interrupted)

    assert list(tmp_path.iterdir()) == []
