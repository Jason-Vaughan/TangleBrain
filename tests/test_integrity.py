"""Tests for the migrated-log integrity check (tanglebrain/integrity.py).

Every case builds a real state root under a temp ``HOME`` and, where compaction is part of the
story, folds it with the real :func:`~tanglebrain.measurement.compact_log` rather than a
hand-written imitation of what compaction leaves behind. The detector's whole design rests on two
claims about that function — that it removes a contiguous prefix, and that it is the only thing
that moves ``totals.json`` — so a fake would test the model instead of the mechanism.
"""
from __future__ import annotations

import inspect
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — covered by the 3.11/3.12 CI jobs.
    tomllib = None  # type: ignore[assignment]

from tanglebrain import integrity
from tanglebrain.integrity import probe_migration_integrity, warn_if_migration_incomplete
from tanglebrain.measurement import compact_log
from tanglebrain.router import LEGACY_STATE_SUBDIR, STATE_DIR_ENV, XDG_DATA_HOME_ENV


def row(n: int) -> str:
    """One usage-log line, unique in ``n`` so presence is a real question.

    Args:
        n: Distinguishes the row from every other.

    Returns:
        A JSON object line, without its newline.
    """
    return json.dumps({"kind": "task", "ts": f"2026-01-01T00:00:{n:02d}Z",
                       "in_tokens_est": 10, "out_tokens_est": 20})


class IntegrityTestBase(unittest.TestCase):
    """A temp ``HOME`` with both state roots resolvable under it."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.legacy = self.home / LEGACY_STATE_SUBDIR
        self.new = self.home / ".local" / "share" / "tanglebrain"
        env = {k: v for k, v in os.environ.items() if k not in (STATE_DIR_ENV, XDG_DATA_HOME_ENV)}
        env["HOME"] = str(self.home)
        self._env = patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)

    def write_legacy(self, rows: list[str]) -> None:
        """Seed the pre-move root's usage log."""
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "usage.jsonl").write_text("".join(f"{r}\n" for r in rows), encoding="utf-8")

    def write_current(self, rows: list[str], *, partial_tail: str = "") -> None:
        """Seed the current root's usage log, optionally ending mid-line.

        Args:
            rows: Complete rows, each written with its newline.
            partial_tail: Bytes appended with no newline — how a copy cut mid-record looks.
        """
        self.new.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{r}\n" for r in rows) + partial_tail
        (self.new / "usage.jsonl").write_text(body, encoding="utf-8")

    def append_current(self, rows: list[str]) -> None:
        """Append post-migration rows to the current log, as recording does."""
        with (self.new / "usage.jsonl").open("a", encoding="utf-8") as handle:
            for r in rows:
                handle.write(f"{r}\n")

    def fold_current(self, keep_recent: int) -> int:
        """Compact the current log for real, so the totals move exactly as they would in the field.

        Args:
            keep_recent: How many of the newest rows to leave in the log.

        Returns:
            The number of rows folded away.
        """
        return compact_log(
            keep_recent=keep_recent,
            log_path=self.new / "usage.jsonl",
            totals_path=self.new / "totals.json",
        )


class NothingToReportTest(IntegrityTestBase):
    """States that are healthy, or that the check has no standing to judge."""

    def test_a_complete_migration_is_silent(self):
        rows = [row(n) for n in range(5)]
        self.write_legacy(rows)
        self.write_current(rows)
        self.assertIsNone(probe_migration_integrity())

    def test_a_complete_migration_with_later_records_is_silent(self):
        legacy = [row(n) for n in range(5)]
        self.write_legacy(legacy)
        self.write_current(legacy)
        self.append_current([row(n) for n in range(100, 140)])
        self.assertIsNone(probe_migration_integrity())

    def test_no_legacy_root_is_silent(self):
        self.write_current([row(n) for n in range(3)])
        self.assertIsNone(probe_migration_integrity())

    def test_an_empty_legacy_log_is_silent(self):
        self.write_legacy([])
        self.write_current([row(1)])
        self.assertIsNone(probe_migration_integrity())

    def test_an_override_collapsing_both_roots_is_silent(self):
        # The operator's explicit state dir was always the state root, so there is no move to
        # audit and every record is trivially "missing" from itself.
        with patch.dict(os.environ, {STATE_DIR_ENV: str(self.new)}, clear=False):
            self.write_current([row(1)])
            self.assertIsNone(probe_migration_integrity())

    def test_a_store_it_cannot_resolve_is_silent_rather_than_fatal(self):
        # This runs before a command has parsed its arguments. A home directory that will not
        # resolve is a reason to skip the check, never a reason to fail the tool.
        with patch("tanglebrain.integrity.legacy_state_root", side_effect=RuntimeError("no home")):
            self.assertIsNone(probe_migration_integrity())


class TruncatedMigrationTest(IntegrityTestBase):
    """Stores the v0.21.0 race left short, in each shape it can leave them."""

    def test_a_copy_cut_on_a_record_boundary_is_reported(self):
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current(legacy[:3])
        self.append_current([row(n) for n in range(100, 105)])
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("copied it only in part", finding)

    def test_a_copy_cut_mid_record_is_reported(self):
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        # The copy stopped part-way through row 3, so the next append glues onto that fragment.
        self.write_current(legacy[:3], partial_tail=legacy[3][:20])
        self.append_current([row(n) for n in range(100, 105)])
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("copied it only in part", finding)

    def test_a_copy_that_landed_nothing_is_reported(self):
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current([])
        self.append_current([row(n) for n in range(100, 110)])
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("copied it only in part", finding)

    def test_a_missing_current_log_is_reported(self):
        self.write_legacy([row(n) for n in range(3)])
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("copied it only in part", finding)

    def test_a_truncated_log_bigger_than_the_legacy_one_is_still_reported(self):
        """The case that rules out comparing sizes, kept as an executable argument.

        After the move the new log grows on every run while the legacy one is frozen, so a store
        truncated at migration and used for a week holds *more* bytes than the file it is missing
        records from. A size comparison calls this healthy — and it is the long-running stores,
        the ones with the most history to lose, that reach it first.
        """
        legacy = [row(n) for n in range(20)]
        self.write_legacy(legacy)
        self.write_current(legacy[:2])
        self.append_current([row(n) for n in range(100, 200)])
        legacy_size = (self.legacy / "usage.jsonl").stat().st_size
        current_size = (self.new / "usage.jsonl").stat().st_size
        self.assertGreater(current_size, legacy_size, "this test proves nothing unless it is bigger")
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("copied it only in part", finding)


class CompactionTest(IntegrityTestBase):
    """A fold removes migrated records from a healthy log; that must not read as data loss."""

    def test_a_fold_into_the_migrated_records_is_silent(self):
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current(legacy)
        self.append_current([row(n) for n in range(100, 104)])
        folded = self.fold_current(keep_recent=6)  # drops the four oldest migrated rows
        self.assertEqual(folded, 4)
        self.assertNotIn(legacy[0], (self.new / "usage.jsonl").read_text(encoding="utf-8"))
        # The newest migrated record is still here, and a fold takes a contiguous prefix — so
        # everything after it is here too, and nothing is missing.
        self.assertIsNone(probe_migration_integrity())

    def test_a_fold_past_every_migrated_record_is_reported_as_inconclusive(self):
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current(legacy)
        self.append_current([row(n) for n in range(100, 104)])
        self.assertEqual(self.fold_current(keep_recent=3), 7)
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("could not confirm", finding)
        self.assertNotIn("copied it only in part", finding)

    def test_no_overlap_and_no_fold_is_reported_as_certain(self):
        """Absent records with the totals untouched can only be a short copy.

        Compaction is the sole writer of `totals.json`, and the legacy root is frozen after the
        move — so totals that never moved are a fold that never ran, and a fold that never ran
        cannot be why the records are gone.
        """
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current([])
        self.append_current([row(n) for n in range(100, 104)])
        self.assertFalse((self.new / "totals.json").exists())
        finding = probe_migration_integrity()
        self.assertIn("copied it only in part", finding)

    def test_a_fold_that_predates_the_move_is_not_mistaken_for_a_later_one(self):
        """The legacy root's own totals migrate too, so equal totals still mean no fold since."""
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        totals = json.dumps({"tasks": 900, "in_tokens_est": 5, "out_tokens_est": 6})
        (self.legacy / "totals.json").write_text(totals, encoding="utf-8")
        self.new.mkdir(parents=True, exist_ok=True)
        (self.new / "totals.json").write_text(totals, encoding="utf-8")
        self.write_current([])
        self.append_current([row(n) for n in range(100, 104)])
        self.assertIn("copied it only in part", probe_migration_integrity())


class FindingTextTest(IntegrityTestBase):
    """What the operator is actually told, in both wordings."""

    def _finding(self, *, inconclusive: bool) -> str:
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current(legacy if inconclusive else [])
        self.append_current([row(n) for n in range(100, 104)])
        if inconclusive:
            self.fold_current(keep_recent=3)
        finding = probe_migration_integrity()
        assert finding is not None
        return finding

    def test_both_wordings_name_the_legacy_file(self):
        for inconclusive in (False, True):
            with self.subTest(inconclusive=inconclusive):
                self.assertIn(str(self.legacy / "usage.jsonl"), self._finding(inconclusive=inconclusive))

    def test_both_wordings_tell_the_operator_to_keep_the_legacy_directory(self):
        # Phase 2's repair has nothing to merge from once it is deleted, so this is the one
        # instruction the detector exists to deliver while the records still exist.
        for inconclusive in (False, True):
            with self.subTest(inconclusive=inconclusive):
                self.assertIn("Keep that directory", self._finding(inconclusive=inconclusive))

    def test_the_inconclusive_wording_does_not_assert_data_loss(self):
        finding = self._finding(inconclusive=True)
        self.assertIn("could not confirm", finding)
        self.assertIn("compaction", finding)

    def test_it_is_one_line(self):
        # It rides the startup of every console script, including piped ones. A paragraph on
        # stderr before every answer is how an operator learns to stop reading stderr.
        self.assertNotIn("\n", self._finding(inconclusive=False))


class WarnTest(IntegrityTestBase):
    """The stderr wrapper the console scripts call."""

    def test_a_healthy_store_prints_nothing(self):
        rows = [row(n) for n in range(4)]
        self.write_legacy(rows)
        self.write_current(rows)
        out = io.StringIO()
        self.assertIsNone(warn_if_migration_incomplete(stream=out))
        self.assertEqual(out.getvalue(), "")

    def test_a_short_store_prints_the_finding_and_returns_it(self):
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current(legacy[:2])
        out = io.StringIO()
        returned = warn_if_migration_incomplete(stream=out)
        self.assertIsNotNone(returned)
        self.assertEqual(out.getvalue(), f"{returned}\n")

    def test_it_defaults_to_stderr_never_stdout(self):
        # stdout carries the routed answer and gets piped; a notice there corrupts it.
        source = inspect.getsource(warn_if_migration_incomplete)
        self.assertIn("sys.stderr", source)
        self.assertNotIn("sys.stdout", source)


class StartupCostTest(IntegrityTestBase):
    """The healthy path must not read the logs it is comparing."""

    def test_a_healthy_store_never_reads_a_record(self):
        """The check runs on every start of every console script, so its cost is a feature.

        The legacy file's size is the offset its last byte sits at inside a complete copy, so the
        healthy answer comes from a seek and a small read in each file — no part of the cost grows
        with the log. Asserting it structurally is what stops a later change from quietly making
        every `tanglebrain` invocation read two megabytes before parsing its arguments.
        """
        rows = [row(n) for n in range(500)]
        self.write_legacy(rows)
        self.write_current(rows)
        self.append_current([row(n) for n in range(1000, 1500)])
        with patch.object(integrity, "_records", side_effect=AssertionError("read the whole log")):
            self.assertIsNone(probe_migration_integrity())

    def test_the_boundary_read_is_bounded(self):
        rows = [row(n) for n in range(2000)]
        self.write_legacy(rows)
        self.write_current(rows)
        self.assertGreater((self.legacy / "usage.jsonl").stat().st_size, integrity.BOUNDARY_BYTES)
        reads: list[int] = []
        real_open = Path.open

        def counting_open(self_path, *args, **kwargs):
            handle = real_open(self_path, *args, **kwargs)
            if "b" in (args[0] if args else kwargs.get("mode", "r")):
                real_read = handle.read

                def read(size=-1):
                    data = real_read(size)
                    reads.append(len(data))
                    return data
                handle.read = read  # type: ignore[method-assign]
            return handle

        with patch.object(Path, "open", counting_open):
            self.assertIsNone(probe_migration_integrity())
        self.assertTrue(reads, "the boundary comparison never happened")
        self.assertLessEqual(max(reads), integrity.BOUNDARY_BYTES)


@unittest.skipIf(tomllib is None, "tomllib is 3.11+; the 3.11/3.12 CI jobs cover this")
class ConsoleScriptWiringTest(unittest.TestCase):
    """Every console script must check the store it is about to read from.

    The list is derived from `[project.scripts]` rather than written here, so a fifth entry point
    added without the check fails this test. The sibling in `test_router.py` makes the same demand
    of the migration itself; they are separate contracts and fail with separate messages.
    """

    def test_every_console_script_checks_migration_integrity(self):
        repo_root = Path(__file__).resolve().parents[1]
        scripts = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["scripts"]
        self.assertGreaterEqual(len(scripts), 4, "entry points vanished — check pyproject.toml")
        for name, target in sorted(scripts.items()):
            with self.subTest(script=name):
                module_name, _, func_name = target.partition(":")
                module = __import__(module_name, fromlist=[func_name])
                source = inspect.getsource(getattr(module, func_name))
                self.assertIn(
                    "warn_if_migration_incomplete()", source,
                    f"{name} ({target}) reads state without checking the migration that filled it",
                )

    def test_the_check_runs_after_the_migration_that_it_audits(self):
        # Ordering is the whole contract: checking first would report every first-run machine as
        # short, because the migration that fills the new root has not run yet.
        repo_root = Path(__file__).resolve().parents[1]
        scripts = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["scripts"]
        for name, target in sorted(scripts.items()):
            with self.subTest(script=name):
                module_name, _, func_name = target.partition(":")
                module = __import__(module_name, fromlist=[func_name])
                source = inspect.getsource(getattr(module, func_name))
                self.assertLess(
                    source.index("migrate_state_root()"),
                    source.index("warn_if_migration_incomplete()"),
                    f"{name} audits the migration before running it",
                )


if __name__ == "__main__":
    unittest.main()
