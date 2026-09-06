"""Atomic file replacement — stage beside the target, then rename over it.

The files whose truncation would cost data go through here: the measurement store, and the config
saves that back up an operator's hand-edited roster or pricing. (Not every write in the package
does — ``router.py`` writes the rotation cursor bare, because a lost cursor restarts rotation and
costs nothing.) ``os.replace`` is atomic within a filesystem, so each write here lands in a staging
file *in the target's own directory* and is then renamed into place: a reader sees either the whole
old file or the whole new one, and an interrupted write leaves the old one intact.

**Atomic is not durable, and the difference matters when two files must land in order.** A rename
is atomic with respect to *readers* immediately, but the bytes behind it may still be in the page
cache — so a power loss can persist a later write while losing an earlier one, inverting an order
the caller depended on. Each write here therefore fsyncs the staging file before the rename and the
containing directory after it, which is what makes "totals first, rows second" survive more than a
killed process. The directory fsync is POSIX-only and skipped where it is not supported; the file
fsync, which is the half that protects the contents, works everywhere.

The same reasoning applies to a backup. Copying straight to the final backup name leaves a
truncated file wearing a valid name when the copy is interrupted — and a backup is consulted
exactly when the original is already gone, so a short one is worse than none. :func:`atomic_copy`
stages and renames for that reason.

**Staging names are unique, not a fixed ``.tmp``.** Two writers racing on one target would
otherwise interleave their bytes into a single staging file and rename the mixture into place. With
a unique name per call each writer stages a coherent file and the last rename wins — a whole file,
whichever one it is.

The guarantee is a rename's, so it holds only within one filesystem; staging beside the target is
what keeps it there.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path


def staging_path(target: str | os.PathLike[str]) -> Path:
    """Return a unique staging path beside ``target``.

    Public because the unique-name reasoning in this module's docstring applies to every sibling
    temp file in the package, not only the ones this module renames — ``roster_edit`` stages a
    validation candidate it never renames, and a fixed name there is the same collision.

    Args:
        target: The file the staging path sits beside.

    Returns:
        A sibling path in the same directory (so a rename stays within one filesystem), carrying a
        random suffix so concurrent writers never share a staging file.
    """
    path = Path(target)
    return path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")


def _sync(path: Path) -> None:
    """Flush ``path``'s contents to stable storage, so a later rename cannot outlive them."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_dir(directory: Path) -> None:
    """Flush ``directory``'s entries, so the rename itself survives a power loss.

    POSIX-only: opening a directory for reading fails on Windows, where a rename's metadata
    durability is the filesystem's own affair. Skipped rather than raised, because the file sync
    above is the half that protects the contents and it is not platform-specific.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


def atomic_write(path: str | os.PathLike[str], text: str) -> None:
    """Replace ``path`` with ``text``, atomically. Creates parent directories as needed.

    Args:
        path: The file to replace.
        text: The full new contents, written as UTF-8.

    Raises:
        OSError: If the directory cannot be created, or the staging write, sync or rename fails.
            The target is left untouched in every failing case, and no staging file survives.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_path(target)
    try:
        staging.write_text(text, encoding="utf-8")
        _sync(staging)
        os.replace(staging, target)
        _sync_dir(target.parent)
    finally:
        # A successful replace consumed the staging file; a failure at either step may not have.
        staging.unlink(missing_ok=True)


def atomic_copy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Copy ``src`` to ``dst`` atomically, preserving metadata. Creates parent directories.

    Args:
        src: The file to copy.
        dst: The destination path, replaced atomically once the copy is complete.

    Raises:
        OSError: If the copy or the rename fails. ``dst`` keeps whatever it held before, rather
            than becoming a partial copy, and no staging file survives.
    """
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = staging_path(target)
    try:
        shutil.copy2(source, staging)
        _sync(staging)
        os.replace(staging, target)
        _sync_dir(target.parent)
    finally:
        staging.unlink(missing_ok=True)
