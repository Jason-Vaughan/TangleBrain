"""Atomic file replacement — stage beside the target, then rename over it.

Every file this package persists is read back by a later process, so none of them may ever be
observable half-written. ``os.replace`` is atomic within a filesystem, so each write here lands in
a staging file *in the target's own directory* and is then renamed into place: a reader sees either
the whole old file or the whole new one, and an interrupted write leaves the old one intact.

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


def _staging_path(target: Path) -> Path:
    """Return a unique staging path beside ``target``.

    Args:
        target: The file that will ultimately be replaced.

    Returns:
        A sibling path in the same directory (so the rename stays within one filesystem), carrying
        a random suffix so concurrent writers never share a staging file.
    """
    return target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")


def atomic_write(path: str | os.PathLike[str], text: str) -> None:
    """Replace ``path`` with ``text``, atomically. Creates parent directories as needed.

    Args:
        path: The file to replace.
        text: The full new contents, written as UTF-8.

    Raises:
        OSError: If the directory cannot be created, or the staging write or rename fails. The
            target is left untouched in every failing case, and no staging file survives.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_path(target)
    try:
        staging.write_text(text, encoding="utf-8")
        os.replace(staging, target)
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
    staging = _staging_path(target)
    try:
        shutil.copy2(source, staging)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
