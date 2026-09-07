"""Tests for the migrated-log integrity check (tanglebrain/integrity.py).

Every case builds a real state root under a temp ``HOME`` and, where compaction is part of the
story, folds it with the real :func:`~tanglebrain.measurement.compact_log` rather than a
hand-written imitation of what compaction leaves behind. The detector's whole design rests on two
claims about that function — that it removes a contiguous prefix, and that it is the only thing
that moves ``totals.json`` — so a fake would test the model instead of the mechanism.
"""
from __future__ import annotations

import contextlib
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
        self.assertIn("holds usage records your current log does not", finding)

    def test_a_copy_cut_mid_record_is_reported(self):
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        # The copy stopped part-way through row 3, so the next append glues onto that fragment.
        self.write_current(legacy[:3], partial_tail=legacy[3][:20])
        self.append_current([row(n) for n in range(100, 105)])
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("holds usage records your current log does not", finding)

    def test_a_copy_that_landed_nothing_is_reported(self):
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current([])
        self.append_current([row(n) for n in range(100, 110)])
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("holds usage records your current log does not", finding)

    def test_a_missing_current_log_is_reported(self):
        self.write_legacy([row(n) for n in range(3)])
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("holds usage records your current log does not", finding)

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
        self.assertIn("holds usage records your current log does not", finding)


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
        self.assertNotIn("holds usage records your current log does not", finding)

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
        self.assertIn("holds usage records your current log does not", finding)

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
        self.assertIn("holds usage records your current log does not", probe_migration_integrity())


class TornLegacyTailTest(IntegrityTestBase):
    """A pre-move log that ends mid-record must not make a complete migration look short."""

    def _seed_torn(self) -> list[str]:
        """A legacy log whose final append died part-way, copied forward completely."""
        rows = [row(n) for n in range(6)]
        self.legacy.mkdir(parents=True, exist_ok=True)
        fragment = row(6)[:18]
        (self.legacy / "usage.jsonl").write_text(
            "".join(f"{r}\n" for r in rows) + fragment, encoding="utf-8"
        )
        # A complete migration copies the fragment verbatim, and the first append after the move
        # writes straight onto it — so the current log holds `<fragment><next record>` as one line
        # and can never contain the legacy file's last line again.
        self.new.mkdir(parents=True, exist_ok=True)
        (self.new / "usage.jsonl").write_text(
            "".join(f"{r}\n" for r in rows) + fragment, encoding="utf-8"
        )
        self.append_current([row(n) for n in range(100, 104)])
        return rows

    def test_a_torn_tail_copied_completely_is_silent_before_a_fold(self):
        self._seed_torn()
        self.assertIsNone(probe_migration_integrity())

    def test_a_torn_tail_copied_completely_is_silent_after_a_fold(self):
        """The case the byte boundary used to hide: once a fold moves it, the record path decides.

        Before the fragment was excluded, this store — intact, migrated in full — was told its
        records were missing, because the legacy file's last *line* was a fragment that the current
        log had already been appended onto.
        """
        rows = self._seed_torn()
        self.assertEqual(self.fold_current(keep_recent=5), 5)
        current = (self.new / "usage.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(rows[0], current, "the fold did not reach the migrated records")
        self.assertIsNone(probe_migration_integrity())

    def test_a_torn_tail_that_was_truncated_is_still_reported(self):
        """Excluding the fragment must not cost the detection it exists to make."""
        rows = [row(n) for n in range(6)]
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "usage.jsonl").write_text(
            "".join(f"{r}\n" for r in rows) + row(6)[:18], encoding="utf-8"
        )
        self.write_current(rows[:2])
        self.append_current([row(n) for n in range(100, 104)])
        self.assertIn(
            "holds usage records your current log does not", probe_migration_integrity()
        )

    def test_a_fragment_longer_than_the_tail_block_is_still_not_a_row(self):
        """The full-read path needs the fragment rule too, and only a huge row reaches it.

        `_newest_row` cannot answer when the tail block holds no complete row — a record longer
        than `BOUNDARY_BYTES` does that — so the probe falls back to reading the log. That fallback
        is the one place the rule could have been forgotten, and forgetting it makes the anchor the
        fragment, which the current log can never contain because it was appended onto.
        """
        rows = [row(n) for n in range(6)]
        fragment = "{" + "x" * (integrity.BOUNDARY_BYTES + 4000)
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "usage.jsonl").write_text(
            "".join(f"{r}\n" for r in rows) + fragment, encoding="utf-8"
        )
        legacy_size = (self.legacy / "usage.jsonl").stat().st_size
        self.assertGreater(legacy_size, integrity.BOUNDARY_BYTES,
                           "the tail block would hold a complete row — this reaches the wrong path")
        # Migrated in full, then used: the fragment is copied verbatim and appended onto.
        self.new.mkdir(parents=True, exist_ok=True)
        (self.new / "usage.jsonl").write_text(
            "".join(f"{r}\n" for r in rows) + fragment, encoding="utf-8"
        )
        self.append_current([row(n) for n in range(100, 110)])
        self.assertEqual(self.fold_current(keep_recent=11), 5)  # folds past the oldest migrated rows
        self.assertIsNone(probe_migration_integrity())

    def test_a_legacy_log_of_nothing_but_a_fragment_has_no_rows_to_miss(self):
        """A log that never completed a record holds nothing a comparison can find missing."""
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "usage.jsonl").write_text('{"kind": "task", "ts": "2026', encoding="utf-8")
        self.write_current([row(n) for n in range(20)])
        self.fold_current(keep_recent=10)
        self.assertTrue((self.new / "totals.json").exists(), "no fold — this tests the wrong branch")
        self.assertIsNone(probe_migration_integrity())


class UnreadableStoreTest(IntegrityTestBase):
    """A check that could not run says so; it never renders as a healthy store."""

    def test_an_unreadable_legacy_log_is_reported_not_swallowed(self):
        rows = [row(n) for n in range(6)]
        self.write_legacy(rows)
        self.write_current(rows[:2])
        target = self.legacy / "usage.jsonl"
        target.chmod(0o000)
        self.addCleanup(target.chmod, 0o644)
        if os.access(target, os.R_OK):  # running as root — the mode says nothing
            self.skipTest("cannot make a file unreadable as this user")
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding, "an unreadable legacy log rendered as a healthy store")
        self.assertIn("could not read", finding)

    def test_a_legacy_log_of_invalid_utf8_is_reported_not_swallowed(self):
        # The boundary read is bytes and survives this; the row comparison is what cannot proceed.
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "usage.jsonl").write_bytes(b"\xff\xfe not utf-8 at all\n" * 40)
        self.write_current([row(n) for n in range(4)])
        self.assertIsNotNone(probe_migration_integrity())

    def test_an_undecodable_legacy_log_is_reported_even_after_a_fold(self):
        """The branch that stayed silent when the tail read alone was made to speak.

        The tail comparison reads bytes and cannot fail on encoding, so an undecodable legacy log
        gets past it. Only the row comparison hits the decode — and while that returned `[]` for
        "unreadable" as well as "no rows", the caller read it as "nothing to report" and the store
        rendered as healthy. Reaching it needs a fold, because without one the totals settle the
        question before any row is read.
        """
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "usage.jsonl").write_bytes(b'{"kind": "\xff\xfe bad bytes"}\n' * 30)
        self.write_current([row(n) for n in range(60)])
        self.fold_current(keep_recent=20)  # moves totals, so the fold branch is the one taken
        self.assertTrue((self.new / "totals.json").exists(), "no fold — this tests the wrong branch")
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding, "an undecodable legacy log rendered as a healthy store")
        self.assertIn("could not read", finding)

    def test_an_unreadable_current_log_names_the_current_log(self):
        """The diagnostic is a filename, so naming the wrong one wastes the only help it gives.

        Both files are in play here and only one failed. Asserting the phrase alone passed while
        the notice sent the operator to check the permissions of the file that was fine.
        """
        rows = [row(n) for n in range(6)]
        self.write_legacy(rows)
        self.write_current(rows)
        target = self.new / "usage.jsonl"
        target.chmod(0o000)
        self.addCleanup(target.chmod, 0o644)
        if os.access(target, os.R_OK):
            self.skipTest("cannot make a file unreadable as this user")
        finding = probe_migration_integrity()
        self.assertIsNotNone(finding)
        self.assertIn("could not read", finding)
        self.assertIn(f"could not read {target}", finding)
        # ...and the instruction still points at the legacy root, which is what must be kept.
        self.assertIn(f"Keep {self.legacy}", finding)

    def test_an_unreadable_legacy_log_names_the_legacy_log(self):
        rows = [row(n) for n in range(6)]
        self.write_legacy(rows)
        self.write_current(rows[:2])
        target = self.legacy / "usage.jsonl"
        target.chmod(0o000)
        self.addCleanup(target.chmod, 0o644)
        if os.access(target, os.R_OK):
            self.skipTest("cannot make a file unreadable as this user")
        self.assertIn(f"could not read {target}", probe_migration_integrity())


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
        # instruction the detector exists to deliver while the records still exist. It names the
        # directory rather than saying "that directory": the unreadable wording can name the
        # *current* log in its first sentence, and a bare demonstrative would then point at it.
        for inconclusive in (False, True):
            with self.subTest(inconclusive=inconclusive):
                self.assertIn(f"Keep {self.legacy}", self._finding(inconclusive=inconclusive))

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
        # stdout carries the routed answer and gets piped, and on the MCP delegate it carries the
        # protocol. Redirect both and assert where the line landed, rather than reading the source
        # for the name of a stream — which passes just as well if the print never runs.
        legacy = [row(n) for n in range(6)]
        self.write_legacy(legacy)
        self.write_current(legacy[:2])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            returned = warn_if_migration_incomplete()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), f"{returned}\n")


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
        with patch.object(integrity, "_rows", side_effect=AssertionError("read the whole log")):
            self.assertIsNone(probe_migration_integrity())

    def test_a_healthy_folded_store_does_not_read_both_logs_in_full(self):
        """After a fold the boundary is gone for good, so what replaces it must still be bounded.

        The newest migrated record sits at the head of the surviving window on a healthy store, so
        the scan that looks for it stops there. Collecting the current log into a set instead would
        pay for the whole file on every startup of every console script, permanently — a fold is
        not a transient state, and the operator is told to keep the legacy directory forever.
        """
        legacy = [row(n) for n in range(40)]
        self.write_legacy(legacy)
        self.write_current(legacy)
        self.append_current([row(n) for n in range(1000, 1400)])
        self.fold_current(keep_recent=420)  # folds into the migrated records, keeping the newest
        with patch.object(integrity, "_rows", side_effect=AssertionError("read a log in full")):
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
