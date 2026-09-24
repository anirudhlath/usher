"""Replacing a file, rather than truncating one and hoping."""

import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import IO

__all__ = ["scratch_beside", "write_atomically"]


def scratch_beside(final: Path) -> Path:
    """Where to assemble `final` before it exists under its own name.

    A *sibling*, because `os.replace` is atomic only within one filesystem
    and a temporary directory is usually a different mount. Dot-prefixed so a
    process killed before its cleanup leaves something obviously not the
    artifact, and randomly suffixed rather than PID-suffixed because two
    writers aimed at one destination have to be unable to share a scratch
    file even when one of them is a re-exec of the other.
    """
    return final.with_name(f".{final.name}.{uuid.uuid4().hex}.partial")


def write_atomically(final: Path, body: Callable[[IO[bytes]], None]) -> None:
    """Run `body` against a scratch sibling and rename it over `final`.

    A reader of `final` sees the previous file or the finished one and never
    a prefix of the new one -- which is what makes a failed run cost nothing,
    including a run that fails over a destination that already holds last
    night's copy.

    The rename is atomic against other processes and *not* against a power
    cut, so two `fsync`s close it and neither is optional: the file's, or the
    name survives a crash pointing at contents that were never written, and
    the directory's, or the contents survive under the old name because the
    rename itself was the thing still in cache.

    Blocking, deliberately: a caller on an event loop hands the whole call to
    a thread so the `body` -- typically compression -- goes with it.
    """
    scratch = scratch_beside(final)
    try:
        with scratch.open("wb") as handle:
            body(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, final)
    except BaseException:
        # `BaseException`, not `Exception`: a `KeyboardInterrupt` is an
        # ordinary way to stop a long write and must not be the one path that
        # leaves the scratch file behind.
        scratch.unlink(missing_ok=True)
        raise
    # Outside the cleanup, because by here there is no scratch file left to
    # clean up and a durability failure must not read as a failed write.
    _fsync_directory(final.parent)


def _fsync_directory(directory: Path) -> None:
    """Flush the directory entry the rename just created.

    A directory has to be opened read-only to be `fsync`ed, which POSIX leaves
    to the implementation and Linux allows.
    """
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
