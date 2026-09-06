"""Tests for atomic file replacement (`tanglebrain/atomic.py`).

The property under test is what a *reader* can observe: never a half-written file, and never a
backup that is short but validly named. Both failure modes are silent by nature — the file looks
fine until something reads it — so each test forces the interruption rather than waiting for one.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tanglebrain.atomic import atomic_copy, atomic_write, staging_path


class AtomicWriteTest(unittest.TestCase):
    """`atomic_write`: the target is replaced whole, or not at all."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = self.tmp / "file.txt"

    def _leftovers(self):
        return [p.name for p in self.tmp.iterdir() if p.name != self.target.name]

    def test_writes_a_new_file_and_creates_parents(self):
        nested = self.tmp / "a" / "b" / "file.txt"
        atomic_write(nested, "hello")
        self.assertEqual(nested.read_text(encoding="utf-8"), "hello")

    def test_replaces_existing_content(self):
        self.target.write_text("old", encoding="utf-8")
        atomic_write(self.target, "new")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "new")

    def test_leaves_no_staging_file_behind(self):
        atomic_write(self.target, "hello")
        self.assertEqual(self._leftovers(), [])

    def test_a_failed_rename_leaves_the_original_whole_and_no_staging_file(self):
        self.target.write_text("old", encoding="utf-8")
        with patch("tanglebrain.atomic.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_write(self.target, "new")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "old")
        self.assertEqual(self._leftovers(), [])

    def test_contents_are_synced_before_the_rename_that_publishes_them(self):
        # Atomicity is about readers; durability is about power loss. `compact_log` orders two
        # writes and needs the first to reach stable storage before the second is even attempted,
        # so a rename that outruns its own bytes would invert the ordering the store depends on.
        calls = []
        real_replace = os.replace
        with patch("tanglebrain.atomic.os.fsync", side_effect=lambda fd: calls.append("fsync")):
            with patch("tanglebrain.atomic.os.replace",
                       side_effect=lambda a, b: (calls.append("replace"), real_replace(a, b))[1]):
                atomic_write(self.target, "hello")
        self.assertEqual(calls[:2], ["fsync", "replace"])

    def test_a_directory_that_cannot_be_synced_is_not_an_error(self):
        # The parent-directory sync is POSIX-only; on a platform that refuses it the write must
        # still succeed, because the file sync is the half that protects the contents.
        real_open = os.open

        def refuse_dirs(path, flags, *a, **kw):
            if Path(path).is_dir():
                raise OSError("directories are not openable here")
            return real_open(path, flags, *a, **kw)

        with patch("tanglebrain.atomic.os.open", side_effect=refuse_dirs):
            atomic_write(self.target, "hello")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "hello")

    def test_staging_names_are_unique_and_sit_beside_the_target(self):
        # A fixed `.tmp` name would let two writers interleave bytes into one staging file and
        # then rename the mixture into place. Unique names make the loser a whole discarded file.
        first, second = staging_path(self.target), staging_path(self.target)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.target.parent)  # same dir keeps the rename atomic
        self.assertEqual(second.parent, self.target.parent)


class AtomicCopyTest(unittest.TestCase):
    """`atomic_copy`: a backup name never appears until the copy behind it is complete."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "source.yaml"
        self.src.write_text("a: 1\nb: 2\n", encoding="utf-8")
        self.dst = self.tmp / "backups" / "source-20260101.yaml"

    def test_copies_content_and_leaves_no_staging_file(self):
        atomic_copy(self.src, self.dst)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), self.src.read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in self.dst.parent.iterdir()], [self.dst.name])

    def test_an_interrupted_copy_leaves_no_file_wearing_the_backup_name(self):
        # The failure this exists to prevent: a short copy under a valid backup name, which looks
        # restorable and is read exactly when the original is already gone.
        def truncated_copy(_src, staging):
            Path(staging).write_text("a: 1\nb:", encoding="utf-8")
            raise OSError("interrupted")

        with patch("tanglebrain.atomic.shutil.copy2", side_effect=truncated_copy):
            with self.assertRaises(OSError):
                atomic_copy(self.src, self.dst)
        self.assertFalse(self.dst.exists())
        self.assertEqual(list(self.dst.parent.iterdir()), [])

    def test_preserves_the_source_modification_time(self):
        # `copy2` semantics, kept through the rename: a backup's timestamp is how an operator
        # tells which one to restore.
        os.utime(self.src, (1_600_000_000, 1_600_000_000))
        atomic_copy(self.src, self.dst)
        self.assertEqual(int(self.dst.stat().st_mtime), 1_600_000_000)


if __name__ == "__main__":
    unittest.main()
