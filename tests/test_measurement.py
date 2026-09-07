"""Tests for the measurement / spend-avoided layer (`tanglebrain/measurement.py`, `totals.py`).

Fully hermetic: the usage log and the totals file are temp paths and pricing is injected, so
nothing touches the operator's real state root or the packaged config. Covers the estimation and
cost math, the fault-tolerant log I/O, the `totals.json` format, and the rollup/format path that
sums stored lifetime totals with the current row window.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import random
import shutil
import threading
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from tanglebrain import measurement
from tanglebrain.adapters.base import AdapterError
from tanglebrain.adapters.cli import _parse_json_field
from tanglebrain.measurement import (
    KEEP_RECENT_BYTES,
    LOG_FILENAME,
    MAX_LOG_BYTES,
    CompactionRefusedError,
    PLACEHOLDER_PRICING,
    PRICING_HEADER,
    Pricing,
    cloud_equiv_usd,
    compact_log,
    default_log_path,
    estimate_tokens,
    fold_records_into_totals,
    format_rollup,
    load_pricing,
    probe_measurement_health,
    record_task,
    read_records,
    rollup,
    save_pricing,
    validate_pricing,
)
from tanglebrain.totals import (
    NOT_PERSISTED,
    TOTALS_FILENAME,
    carry_unknown_fields,
    default_totals_path,
    empty_totals,
    normalize_totals,
    read_totals,
    write_totals,
)

# A fixed, non-placeholder pricing so cost assertions are exact and the caveat is off.
FIXED = Pricing(reference_model="test-frontier", input_per_mtok=2.0, output_per_mtok=10.0, is_placeholder=False)


@dataclass
class FakeEntry:
    """Stand-in for a RosterEntry (record_task only reads .tier / .id)."""

    id: str
    tier: str


class EstimateTokensTest(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens(None), 0)  # falsy guard

    def test_non_empty_is_at_least_one(self):
        self.assertEqual(estimate_tokens("ab"), 1)  # 2 // 4 == 0 -> clamped to 1

    def test_chars_over_four(self):
        self.assertEqual(estimate_tokens("a" * 40), 10)


class CloudEquivTest(unittest.TestCase):
    def test_known_math(self):
        # 1M in @ $2 + 1M out @ $10 = $12
        self.assertAlmostEqual(cloud_equiv_usd(1_000_000, 1_000_000, FIXED), 12.0)

    def test_zero_tokens_zero_cost(self):
        self.assertEqual(cloud_equiv_usd(0, 0, FIXED), 0.0)


class RecordTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = Path(self.tmp) / "sub" / "usage.jsonl"  # nested: parent must be created

    def test_appends_well_formed_record(self):
        record_task(
            path="local",
            entry=FakeEntry("gpt-oss-120b", "local"),
            prompt="a" * 40,
            response="b" * 80,
            log_path=self.log,
            pricing=FIXED,
        )
        records = read_records(self.log)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["path"], "local")
        self.assertEqual(rec["tier"], "local")
        self.assertEqual(rec["model"], "gpt-oss-120b")
        self.assertEqual(rec["in_tokens_est"], 10)
        self.assertEqual(rec["out_tokens_est"], 20)
        self.assertAlmostEqual(rec["spend_avoided_usd"], cloud_equiv_usd(10, 20, FIXED), places=6)
        self.assertEqual(rec["pricing_ref"], "test-frontier")

    def test_appends_accumulate(self):
        for _ in range(3):
            record_task(path="router", entry=FakeEntry("claude", "sub"),
                        prompt="hi", response="there", log_path=self.log, pricing=FIXED)
        self.assertEqual(len(read_records(self.log)), 3)

    def test_api_tier_avoids_nothing(self):
        record_task(path="model", entry=FakeEntry("gpt-paid", "api"),
                    prompt="x" * 40, response="y" * 40, log_path=self.log, pricing=FIXED)
        rec = read_records(self.log)[0]
        self.assertGreater(rec["cloud_equiv_usd"], 0.0)
        self.assertEqual(rec["spend_avoided_usd"], 0.0)

    def test_none_entry_is_unknown(self):
        record_task(path="router", entry=None, prompt="x", response="y",
                    log_path=self.log, pricing=FIXED)
        rec = read_records(self.log)[0]
        self.assertEqual(rec["tier"], "unknown")
        self.assertEqual(rec["model"], "unknown")

    def test_logging_failure_never_raises(self):
        # Point the log at a path whose parent cannot be created (a file used as a directory).
        blocker = Path(self.tmp) / "blocker"
        blocker.write_text("i am a file")
        bad = blocker / "nested" / "usage.jsonl"
        # Must not raise despite the unwritable path. The stderr note is this path's own contract
        # (`LostWriteNoticeTest`); swallow it here so the suite's output stays clean.
        with contextlib.redirect_stderr(io.StringIO()):
            record_task(path="local", entry=FakeEntry("x", "local"), prompt="p", response="r",
                        log_path=bad, pricing=FIXED)


class LostWriteNoticeTest(unittest.TestCase):
    """`record_task` still swallows a failed append, and now says once that it happened.

    The swallow is a ratified invariant — measurement never breaks the answer — and its cost is
    that the spend-avoided headline can quietly go on understating. These pin the other half: the
    operator is told, once, and the telling can never become the failure it reports.
    """

    def setUp(self):
        """Isolate each test from the process-wide once-per-run flag."""
        measurement._LOST_WRITE_NOTED = False
        self.addCleanup(setattr, measurement, "_LOST_WRITE_NOTED", False)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = Path(self.tmp) / "sub" / "usage.jsonl"
        # A file standing where a directory has to be created: every append fails, none partially.
        blocker = Path(self.tmp) / "blocker"
        blocker.write_text("i am a file")
        self.bad = blocker / "nested" / "usage.jsonl"

    def record(self, log_path, times=1):
        """Record `times` tasks against `log_path`, returning what reached stderr.

        Args:
            log_path: Where to point the usage log.
            times: How many tasks to record in this one process.

        Returns:
            The captured stderr text.
        """
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(times):
                record_task(path="local", entry=FakeEntry("x", "local"), prompt="p",
                            response="r", log_path=log_path, pricing=FIXED)
        return err.getvalue()

    def test_successful_write_says_nothing(self):
        """The healthy path is the common one; a notice on it would be pure noise."""
        self.assertEqual(self.record(self.log, times=3), "")
        self.assertEqual(len(read_records(self.log)), 3)

    def test_lost_write_is_noted(self):
        """A dropped append is invisible in the answer, so it has to be visible somewhere."""
        err = self.record(self.bad)
        self.assertIn("could not be recorded to the usage log", err)
        self.assertIn("--stats", err, "the note must name the figure the loss distorts")
        self.assertIn("understates", err, "and the direction it moves it in")

    def test_note_carries_the_diagnosis(self):
        """A write failure the operator cannot act on is a notice they cannot use."""
        err = self.record(self.bad)
        # The blocked path is a file used as a directory: the errno and the path are the fix.
        self.assertIn("Error", err, "the exception type reaches the reader")
        self.assertIn(str(self.bad.parent.parent), err, "as does the path that could not be made")

    def test_note_fires_once_across_many_failures(self):
        """Per-task would run on every routed request and train the reader to skip it."""
        err = self.record(self.bad, times=25)
        self.assertEqual(err.count("could not be recorded"), 1)

    def test_note_disclaims_being_a_count(self):
        """Once-per-run means one line can stand for any number of losses. Say so."""
        err = self.record(self.bad, times=5)
        self.assertIn("once per run", err)

    def test_note_fires_once_across_threads(self):
        """`delegate_many` loses appends across threads; the operator still gets one line.

        Pins what the operator experiences, not the mechanism: the flag is an unguarded
        test-and-set, and a lock-free version was mutation-checked to pass this — a genuinely
        raced double print is a duplicated line, which is the cost the flag is allowed to have.
        """
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            threads = [
                threading.Thread(
                    target=record_task,
                    kwargs={"path": "delegate", "entry": FakeEntry("x", "local"), "prompt": "p",
                            "response": "r", "log_path": self.bad, "pricing": FIXED},
                )
                for _ in range(16)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(err.getvalue().count("could not be recorded"), 1)

    def test_a_failing_note_does_not_escape(self):
        """The notice runs inside the swallow; anything it raises reaches the user's answer."""
        class BrokenStderr(io.StringIO):
            def write(self, _data):
                raise OSError("stderr is gone")

        with contextlib.redirect_stderr(BrokenStderr()):
            # Must return normally: a broken stream is not allowed to become a broken answer.
            record_task(path="local", entry=FakeEntry("x", "local"), prompt="p", response="r",
                        log_path=self.bad, pricing=FIXED)

    def test_a_failure_after_the_append_is_not_a_lost_write(self):
        """The note claims a row was dropped, so it must not fire over a row that landed."""
        with patch.object(measurement, "_compact_if_oversized",
                          side_effect=OSError("boom")):
            err = self.record(self.log)
        self.assertEqual(err, "")
        self.assertEqual(len(read_records(self.log)), 1, "the row is on disk, nothing was lost")

    def test_note_never_carries_prompt_or_response_text(self):
        """The one hard rule on this path: no prompt or response text leaves the process."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            record_task(path="local", entry=FakeEntry("x", "local"),
                        prompt="SECRETPROMPT", response="SECRETRESPONSE",
                        log_path=self.bad, pricing=FIXED)
        self.assertNotIn("SECRETPROMPT", err.getvalue())
        self.assertNotIn("SECRETRESPONSE", err.getvalue())


class ReadRecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = Path(self.tmp) / "usage.jsonl"

    def test_missing_file_is_empty(self):
        self.assertEqual(read_records(self.log), [])

    def test_skips_corrupt_and_blank_lines(self):
        self.log.write_text(
            json.dumps({"tier": "sub", "spend_avoided_usd": 1.0}) + "\n"
            "{ not valid json\n"
            "\n"
            + json.dumps({"tier": "local", "spend_avoided_usd": 2.0}) + "\n"
        )
        records = read_records(self.log)
        self.assertEqual(len(records), 2)  # the garbage + blank lines dropped


class RollupTest(unittest.TestCase):
    def test_totals_and_by_tier(self):
        records = [
            {"tier": "local", "in_tokens_est": 10, "out_tokens_est": 20,
             "cloud_equiv_usd": 1.0, "spend_avoided_usd": 1.0},
            {"tier": "sub", "in_tokens_est": 5, "out_tokens_est": 5,
             "cloud_equiv_usd": 0.5, "spend_avoided_usd": 0.5},
            {"tier": "local", "in_tokens_est": 1, "out_tokens_est": 1,
             "cloud_equiv_usd": 0.1, "spend_avoided_usd": 0.1},
        ]
        s = rollup(records)
        self.assertEqual(s["tasks"], 3)
        self.assertEqual(s["by_tier"], {"local": 2, "sub": 1})
        self.assertEqual(s["in_tokens_est"], 16)
        self.assertEqual(s["out_tokens_est"], 26)
        self.assertAlmostEqual(s["spend_avoided_usd"], 1.6)

    def test_empty_records(self):
        s = rollup([])
        self.assertEqual(s["tasks"], 0)
        self.assertEqual(s["by_tier"], {})
        self.assertEqual(s["spend_avoided_usd"], 0.0)

    def test_tolerates_bad_numeric_fields(self):
        s = rollup([{"tier": "local", "in_tokens_est": "oops", "spend_avoided_usd": None}])
        self.assertEqual(s["tasks"], 1)
        self.assertEqual(s["in_tokens_est"], 0)
        self.assertEqual(s["spend_avoided_usd"], 0.0)


class LoadPricingTest(unittest.TestCase):
    def test_loads_packaged_pricing(self):
        # The packaged config/pricing.yaml carries the default Claude Sonnet anchor ($3/$15).
        p = load_pricing()
        self.assertIsInstance(p, Pricing)
        self.assertFalse(p.is_placeholder)
        self.assertEqual(p.input_per_mtok, 3.0)
        self.assertEqual(p.output_per_mtok, 15.0)

    def test_missing_file_falls_back_to_placeholder(self):
        self.assertIs(load_pricing("/nonexistent/pricing.yaml"), PLACEHOLDER_PRICING)

    def test_corrupt_file_falls_back(self):
        tmp = tempfile.mkdtemp()
        bad = Path(tmp) / "pricing.yaml"
        bad.write_text("input_per_mtok: not-a-number\n")
        self.assertIs(load_pricing(bad), PLACEHOLDER_PRICING)


class ValidatePricingTest(unittest.TestCase):
    def _ok(self, **over):
        d = {"reference_model": "M", "input_per_mtok": 3.0, "output_per_mtok": 15.0, "placeholder": False}
        d.update(over)
        return d

    def test_valid(self):
        p = validate_pricing(self._ok())
        self.assertEqual((p.reference_model, p.input_per_mtok, p.output_per_mtok, p.is_placeholder),
                         ("M", 3.0, 15.0, False))

    def test_strips_reference_model(self):
        self.assertEqual(validate_pricing(self._ok(reference_model="  M  ")).reference_model, "M")

    def test_empty_model_rejected(self):
        with self.assertRaises(ValueError):
            validate_pricing(self._ok(reference_model="   "))

    def test_negative_rate_rejected(self):
        with self.assertRaises(ValueError):
            validate_pricing(self._ok(output_per_mtok=-5))

    def test_non_numeric_rate_rejected(self):
        with self.assertRaises(ValueError):
            validate_pricing(self._ok(input_per_mtok="lots"))

    def test_bool_rate_rejected(self):
        # bool is a subclass of int — must not slip through as a rate.
        with self.assertRaises(ValueError):
            validate_pricing(self._ok(input_per_mtok=True))

    def test_nan_rate_rejected(self):
        with self.assertRaises(ValueError):
            validate_pricing(self._ok(input_per_mtok=float("nan")))

    def test_non_bool_placeholder_rejected(self):
        with self.assertRaises(ValueError):
            validate_pricing(self._ok(placeholder="yes"))


class SavePricingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "config" / "pricing.yaml"
        # Route backups into a temp state dir, not the real ~/.cache.
        self._env = patch.dict("os.environ", {"TANGLEBRAIN_STATE_DIR": str(Path(self.tmp) / "state")}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_roundtrips_and_preserves_header(self):
        p = Pricing("Claude Sonnet", 3.0, 15.0, False)
        save_pricing(p, self.path)
        text = self.path.read_text()
        self.assertIn(PRICING_HEADER.splitlines()[0], text)  # header survived
        back = load_pricing(self.path)
        self.assertEqual(back.reference_model, "Claude Sonnet")
        self.assertEqual(back.input_per_mtok, 3.0)
        self.assertFalse(back.is_placeholder)

    def test_no_tmp_left_behind(self):
        save_pricing(Pricing("M", 1.0, 2.0, True), self.path)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_an_interrupted_backup_leaves_no_file_wearing_a_backup_name(self):
        # A backup is read exactly when the original is already gone, so a short one that carries a
        # valid name is worse than no backup at all. The copy stages and renames for that reason.
        def truncated_copy(_src, staging):
            Path(staging).write_text("placeholder: fal", encoding="utf-8")
            raise OSError("interrupted")

        save_pricing(Pricing("First", 1.0, 2.0, False), self.path)
        with patch("tanglebrain.atomic.shutil.copy2", side_effect=truncated_copy):
            with self.assertRaises(OSError):
                save_pricing(Pricing("Second", 9.0, 9.0, False), self.path)
        backups = Path(self.tmp) / "state" / "backups"
        self.assertEqual(list(backups.glob("pricing-*.yaml")), [])

    def test_backup_created_on_overwrite(self):
        save_pricing(Pricing("First", 1.0, 2.0, False), self.path)   # creates the file (no prior → no backup)
        save_pricing(Pricing("Second", 9.0, 9.0, False), self.path)  # overwrites → backs up "First"
        backups = list((Path(self.tmp) / "state" / "backups").glob("pricing-*.yaml"))
        self.assertTrue(backups)
        self.assertIn("First", backups[0].read_text())
        self.assertEqual(load_pricing(self.path).reference_model, "Second")

    def test_placeholder_flag_roundtrips(self):
        save_pricing(Pricing("M", 1.0, 2.0, True), self.path)
        self.assertTrue(load_pricing(self.path).is_placeholder)

    def test_preserves_existing_file_header_verbatim(self):
        # A save over an existing file must keep that file's curated header (no drift/doc loss).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        custom = "# CUSTOM HEADER\n# COST_BASIS provenance line\n"
        self.path.write_text(custom + 'placeholder: false\nreference_model: "Old"\n'
                                      'input_per_mtok: 1.0\noutput_per_mtok: 2.0\n')
        save_pricing(Pricing("New", 9.0, 9.0, False), self.path)
        text = self.path.read_text()
        self.assertIn("# CUSTOM HEADER", text)
        self.assertIn("COST_BASIS provenance line", text)  # specific provenance survives the save
        self.assertEqual(load_pricing(self.path).reference_model, "New")

    def test_adversarial_values_roundtrip(self):
        # _render_pricing must produce YAML that load_pricing reads back identically.
        for ref in ['has: a colon', 'has "double" quotes', "has 'single'", "back\\slash",
                    "unicode €é", "line\nbreak", "tab\there"]:
            with self.subTest(ref=ref):
                save_pricing(Pricing(ref, 0.0, 1e20, False), self.path)
                back = load_pricing(self.path)
                self.assertEqual(back.reference_model, ref)
                self.assertEqual(back.input_per_mtok, 0.0)
                self.assertEqual(back.output_per_mtok, 1e20)


class OriginAttributionTest(unittest.TestCase):
    """#74: the origin field on records, its rollup bucket, and the --stats line."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = Path(self.tmp) / "usage.jsonl"

    def _record(self, **kwargs):
        record_task(
            path="model", entry=FakeEntry("m", "local"), prompt="p", response="r",
            log_path=self.log, pricing=FIXED, **kwargs,
        )

    def test_record_writes_origin_when_given_and_omits_when_absent(self):
        self._record(origin="serve")
        self._record()
        tagged, untagged = read_records(self.log)
        self.assertEqual(tagged["origin"], "serve")
        self.assertNotIn("origin", untagged)

    def test_record_writes_parent_task_id_on_task_records(self):
        # #74: an external caller's identity (the serve header) rides on a kind="task" record.
        self._record(parent_task_id="tc-session-42")
        record = read_records(self.log)[0]
        self.assertEqual(record["kind"], "task")
        self.assertEqual(record["parent_task_id"], "tc-session-42")

    def test_rollup_buckets_by_origin_with_untagged_sentinel(self):
        summary = rollup([
            {"tier": "local", "origin": "serve"},
            {"tier": "local", "origin": "serve"},
            {"tier": "sub", "origin": "cli"},
            {"tier": "sub"},  # pre-#74 record — never guessed at
            {"kind": "delegate", "model": "m", "origin": "serve"},  # delegates stay out
        ])
        self.assertEqual(summary["by_origin"], {"serve": 2, "cli": 1, "untagged": 1})

    def test_format_rollup_shows_origin_split_only_when_tagged(self):
        tagged = format_rollup(
            rollup([{"tier": "local", "origin": "serve"}, {"tier": "local"}]), FIXED
        )
        self.assertIn("By origin:", tagged)
        self.assertIn("serve 1", tagged)
        self.assertIn("untagged 1", tagged)
        # All-untagged history says nothing — the line stays hidden.
        untagged_only = format_rollup(rollup([{"tier": "local"}]), FIXED)
        self.assertNotIn("By origin:", untagged_only)


class FormatRollupTest(unittest.TestCase):
    def test_renders_figures(self):
        s = rollup([{"tier": "local", "in_tokens_est": 10, "out_tokens_est": 20,
                     "cloud_equiv_usd": 1.5, "spend_avoided_usd": 1.5}])
        out = format_rollup(s, FIXED)
        self.assertIn("Tasks routed:", out)
        self.assertIn("$1.50", out)
        self.assertIn("test-frontier", out)
        self.assertNotIn("PLACEHOLDER", out)

    def test_placeholder_caveat_shown(self):
        out = format_rollup(rollup([]), PLACEHOLDER_PRICING)
        self.assertIn("PLACEHOLDER", out)


class PricingRevisionSpanTest(unittest.TestCase):
    """The reference-pricing line describes the figure, not the current config.

    Each record is priced when it runs and a `pricing.yaml` edit never restates history, so a
    figure summed across an edit spans several revisions and the block has to say so.
    """

    @staticmethod
    def _ref_line(out: str) -> str:
        return next(line for line in out.splitlines() if "Pricing ref:" in line)

    @staticmethod
    def _rows(*refs: str) -> list[dict]:
        return [{"kind": "task", "tier": "local", "spend_avoided_usd": 1.0, "pricing_ref": r}
                for r in refs]

    def test_one_revision_matching_current_pricing_renders_exactly_as_before(self):
        # Byte-identity against the pre-span behaviour, expressed as the only difference that
        # behaviour could not see: the field itself. Every record today carries `pricing_ref`, so
        # stripping it reproduces the block this line used to print.
        rows = self._rows("test-frontier", "test-frontier")
        stripped = [{k: v for k, v in r.items() if k != "pricing_ref"} for r in rows]
        self.assertEqual(format_rollup(rollup(rows), FIXED), format_rollup(rollup(stripped), FIXED))
        self.assertEqual(self._ref_line(format_rollup(rollup(rows), FIXED)),
                         "  Pricing ref:    test-frontier")

    def test_one_revision_is_named_even_when_it_is_not_the_configured_one(self):
        # An operator who edits the rates and has not routed anything since: the figure is still a
        # figure priced under the old revision, and labelling it with the new one is the defect.
        out = format_rollup(rollup(self._rows("older-frontier")), FIXED)
        self.assertEqual(self._ref_line(out), "  Pricing ref:    older-frontier")
        self.assertNotIn("test-frontier", out)

    def test_a_span_reports_its_count_and_says_why_that_is_expected(self):
        out = format_rollup(rollup(self._rows("frontier-a", "frontier-b", "frontier-c")), FIXED)
        self.assertEqual(self._ref_line(out), "  Pricing ref:    3 revisions")
        self.assertIn("spans an edit to the reference pricing", out)
        # The reassurance the caveat exists to carry: an edit does not restate what came before.
        self.assertIn("keeps the figure it was priced at", out)

    def test_the_span_note_reports_an_edit_rather_than_a_rate_change(self):
        # `pricing_ref` carries the reference-model label and nothing else, so a span is evidence
        # that the pricing config was edited — a relabelling raises it over unchanged rates. The
        # line must not claim to have seen the rates move.
        out = format_rollup(rollup(self._rows("frontier-a", "frontier-b")), FIXED)
        self.assertIn("spans an edit to the reference pricing", out)
        self.assertNotIn("rates changed", out)

    def test_the_span_note_does_not_read_as_a_fault(self):
        # A benign, expected state must not borrow the warning glyph the placeholder caveat owns,
        # or the caveat that does mean something stops being read.
        out = format_rollup(rollup(self._rows("frontier-a", "frontier-b")), FIXED)
        self.assertIn("ℹ pricing:", out)
        self.assertNotIn("⚠", out)

    def test_the_revisions_are_counted_never_listed(self):
        # Partitioning the headline by revision was considered and rejected: correct, unreadable,
        # and unbounded in width once an operator has tuned the rates a few times.
        out = format_rollup(rollup(self._rows("frontier-a", "frontier-b")), FIXED)
        self.assertNotIn("frontier-a", out)
        self.assertNotIn("frontier-b", out)

    def test_no_span_note_when_the_history_holds_one_revision(self):
        self.assertNotIn("ℹ pricing:", format_rollup(rollup(self._rows("frontier-a")), FIXED))

    def test_a_history_with_no_revision_evidence_falls_back_to_current_pricing(self):
        # An empty log, or rows written before the per-record field existed: there is nothing
        # truer to print, so the line stays exactly what it has always been.
        for summary in (rollup([]), rollup([{"kind": "task", "tier": "local"}])):
            out = format_rollup(summary, FIXED)
            self.assertEqual(self._ref_line(out), "  Pricing ref:    test-frontier")
            self.assertNotIn("ℹ pricing:", out)

    def test_a_span_across_folded_totals_and_live_rows_is_detected(self):
        # The case the stored `pricing_refs` set exists for: the rows carrying the older revision
        # are gone, and the span has to survive them.
        out = format_rollup(rollup(self._rows("frontier-b"), {"pricing_refs": ["frontier-a"]}), FIXED)
        self.assertEqual(self._ref_line(out), "  Pricing ref:    2 revisions")

    def test_a_delegate_widens_the_span_because_its_cloud_equiv_is_rendered(self):
        # The block prints the delegates' cloud-equiv, priced like everything else, so a sub-call
        # from after an edit puts a second revision into a figure the reader can see.
        out = format_rollup(rollup(self._rows("frontier-a") + [
            {"kind": "delegate", "model": "m", "cloud_equiv_usd": 0.5, "pricing_ref": "frontier-b"},
        ]), FIXED)
        self.assertEqual(self._ref_line(out), "  Pricing ref:    2 revisions")
        # Pinned here too, or the reason in this test's name can be deleted without failing it.
        self.assertIn("Cloud-equiv:  $0.50", out)

    def test_an_api_tier_task_widens_the_span_though_it_avoided_nothing(self):
        # Real spend avoids nothing, so `spend_avoided_usd` is 0.0. The task is still a priced,
        # counted task: it lands in the task count and the tier split, both rendered, so the
        # revision it was priced under is behind figures the reader sees.
        out = format_rollup(rollup(self._rows("frontier-a") + [
            {"kind": "task", "tier": "api", "spend_avoided_usd": 0.0,
             "cloud_equiv_usd": 0.5, "pricing_ref": "frontier-b"},
        ]), FIXED)
        self.assertEqual(self._ref_line(out), "  Pricing ref:    2 revisions")
        # Pinned like the delegate's, so the reason this test gives cannot rot unnoticed.
        self.assertIn("Tasks routed:   2", out)
        self.assertIn("api 1", out)

    def test_a_failure_only_history_names_no_revision(self):
        # A failed task priced nothing, so it never widens the span — `rollup` collects the ref
        # only from records that put money into the figure.
        out = format_rollup(rollup([{"kind": "failure", "pricing_ref": "never-charged"}]), FIXED)
        self.assertEqual(self._ref_line(out), "  Pricing ref:    test-frontier")

    def test_rendering_stores_nothing_and_mutates_nothing(self):
        # A display fix: no stored value changes, and the summary it was handed comes back intact.
        summary = rollup(self._rows("frontier-a", "frontier-b"))
        before = copy.deepcopy(summary)
        format_rollup(summary, FIXED)
        self.assertEqual(summary, before)


class DefaultLogPathTest(unittest.TestCase):
    def test_honors_state_dir_env(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"TANGLEBRAIN_STATE_DIR": "/tmp/tb-test"}, clear=False):
            self.assertEqual(default_log_path(), Path("/tmp/tb-test/usage.jsonl"))

    def test_log_and_backups_resolve_under_one_data_tier_root(self):
        """The log and the backups share the state root, and the root is not the cache tier.

        Resolution order is asserted in `test_router.StateRootTest`; what matters here is that
        measurement reads the *same* root the router does, so the migration moves one directory
        rather than chasing files across two.
        """
        import os
        from unittest.mock import patch

        from tanglebrain.measurement import _backup_dir
        from tanglebrain.router import state_root

        home = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items()
               if k not in ("TANGLEBRAIN_STATE_DIR", "XDG_DATA_HOME")}
        env["HOME"] = home
        with patch.dict(os.environ, env, clear=True):
            root = state_root()
            self.assertEqual(default_log_path().parent, root)
            self.assertEqual(_backup_dir().parent, root)
            self.assertNotIn(".cache", root.parts)


class UsageLogSurvivesTheMoveTest(unittest.TestCase):
    """The end-to-end fact chunk 01 exists for: an existing operator's figure does not change.

    The unit tests cover the copy; this covers the thing the copy is *for* — a rollup taken
    before and after the migration reads the same number. A migration that moved bytes but broke
    the read path would pass every test above and still zero someone's history.
    """

    def test_rollup_is_identical_across_the_migration(self):
        import os
        from unittest.mock import patch

        from tanglebrain.router import migrate_state_root

        home = Path(tempfile.mkdtemp())
        legacy = home / ".cache" / "tanglebrain"
        legacy.mkdir(parents=True)
        rows = [
            {"kind": "task", "path": "local", "tier": "local", "model": "m",
             "in_tokens_est": 100, "out_tokens_est": 200,
             "cloud_equiv_usd": 0.5, "spend_avoided_usd": 0.5, "pricing_ref": "ref"},
            {"kind": "task", "path": "router", "tier": "sub", "model": "m2",
             "in_tokens_est": 10, "out_tokens_est": 20,
             "cloud_equiv_usd": 0.25, "spend_avoided_usd": 0.25, "pricing_ref": "ref"},
        ]
        (legacy / "usage.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

        env = {k: v for k, v in os.environ.items()
               if k not in ("TANGLEBRAIN_STATE_DIR", "XDG_DATA_HOME")}
        env["HOME"] = str(home)
        with patch.dict(os.environ, env, clear=True):
            before = rollup(read_records(legacy / "usage.jsonl"))
            migrate_state_root(stream=io.StringIO())
            after = rollup(read_records())
        self.assertEqual(before, after)
        self.assertAlmostEqual(after["spend_avoided_usd"], 0.75)
        self.assertEqual(after["tasks"], 2)


class DelegateObservabilityTest(unittest.TestCase):
    """kind='delegate' records: written, kept out of the headline, rolled up separately, thread-safe."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = str(Path(self.tmp) / "usage.jsonl")

    def _read(self):
        return read_records(self.log)

    def test_record_defaults_to_task_kind(self):
        record_task(path="router", entry=FakeEntry("claude", "sub"),
                    prompt="hi", response="yo", log_path=self.log, pricing=FIXED)
        self.assertEqual(self._read()[0]["kind"], "task")

    def test_record_delegate_kind(self):
        record_task(path="delegate", entry=FakeEntry("local-x", "local"),
                    prompt="hi", response="yo", kind="delegate", log_path=self.log, pricing=FIXED)
        self.assertEqual(self._read()[0]["kind"], "delegate")

    def test_rollup_excludes_delegates_from_headline(self):
        records = [
            {"kind": "task", "tier": "sub", "in_tokens_est": 10, "out_tokens_est": 10,
             "cloud_equiv_usd": 1.0, "spend_avoided_usd": 1.0},
            {"kind": "delegate", "model": "local-x", "in_tokens_est": 100, "out_tokens_est": 200,
             "cloud_equiv_usd": 5.0, "spend_avoided_usd": 5.0},
        ]
        s = rollup(records)
        # Headline counts the one task only — delegate tokens/spend must NOT inflate it.
        self.assertEqual(s["tasks"], 1)
        self.assertEqual(s["by_tier"], {"sub": 1})
        self.assertEqual(s["in_tokens_est"], 10)
        self.assertEqual(s["out_tokens_est"], 10)
        self.assertAlmostEqual(s["spend_avoided_usd"], 1.0)
        # Delegate sub-rollup is separate + informational.
        d = s["delegates"]
        self.assertEqual(d["count"], 1)
        self.assertEqual(
            d["by_backend"],
            {"local-x": {"count": 1, "in_tokens_est": 100, "out_tokens_est": 200}},
        )
        self.assertEqual(d["in_tokens_est"], 100)
        self.assertEqual(d["out_tokens_est"], 200)
        self.assertAlmostEqual(d["cloud_equiv_usd"], 5.0)

    def test_kindless_record_counts_as_task(self):
        s = rollup([{"tier": "local", "in_tokens_est": 4, "spend_avoided_usd": 0.2}])
        self.assertEqual(s["tasks"], 1)
        self.assertEqual(s["delegates"]["count"], 0)

    def test_rollup_excludes_failures_from_headline(self):
        # #100: failure records are counted, but held out of the headline like delegates, and
        # every lost attempt (behind a failover success or a total failure) is tallied.
        records = [
            {"kind": "task", "tier": "sub", "spend_avoided_usd": 1.0,
             "failures": [{"entry": "claude", "error": "boom"}]},
            {"kind": "failure", "path": "router", "in_tokens_est": 10, "spend_avoided_usd": 0.0,
             "failures": [{"entry": "claude", "error": "e1"}, {"entry": "gemini", "error": "e2"}]},
        ]
        s = rollup(records)
        self.assertEqual(s["tasks"], 1)  # the failure record is not a routed task
        self.assertEqual(s["failures"], 1)
        self.assertEqual(s["lost_attempts"], 3)  # 1 lost failover + 2 exhausted attempts
        self.assertEqual(s["in_tokens_est"], 0)  # failure tokens stay out of the headline
        self.assertEqual(s["by_tier"], {"sub": 1})
        self.assertAlmostEqual(s["spend_avoided_usd"], 1.0)

    def test_by_backend_aggregates_multiple(self):
        records = [
            {"kind": "delegate", "model": "local-x", "in_tokens_est": 10, "out_tokens_est": 5},
            {"kind": "delegate", "model": "local-x", "in_tokens_est": 20, "out_tokens_est": 5},
            {"kind": "delegate", "model": "cheap-sub", "in_tokens_est": 1, "out_tokens_est": 1},
        ]
        d = rollup(records)["delegates"]
        self.assertEqual(d["count"], 3)
        self.assertEqual(d["by_backend"]["local-x"]["count"], 2)
        self.assertEqual(d["by_backend"]["local-x"]["in_tokens_est"], 30)
        self.assertEqual(d["by_backend"]["cheap-sub"]["count"], 1)

    def test_record_writes_task_id_when_given(self):
        record_task(path="router", entry=FakeEntry("claude", "sub"), prompt="hi", response="yo",
                    task_id="task-abc", log_path=self.log, pricing=FIXED)
        rec = self._read()[0]
        self.assertEqual(rec["task_id"], "task-abc")
        self.assertNotIn("parent_task_id", rec)

    def test_record_writes_parent_task_id_for_delegate(self):
        record_task(path="delegate", entry=FakeEntry("local-x", "local"), prompt="hi", response="yo",
                    kind="delegate", parent_task_id="task-abc", log_path=self.log, pricing=FIXED)
        rec = self._read()[0]
        self.assertEqual(rec["parent_task_id"], "task-abc")
        self.assertNotIn("linkage_lost", rec)
        self.assertNotIn("task_id", rec)

    def test_record_marks_parentless_delegate_as_lost_linkage(self):
        record_task(path="delegate", entry=FakeEntry("local-x", "local"), prompt="hi", response="yo",
                    kind="delegate", log_path=self.log, pricing=FIXED)
        rec = self._read()[0]
        self.assertTrue(rec["linkage_lost"])
        self.assertNotIn("parent_task_id", rec)

    def test_record_omits_linkage_fields_when_absent(self):
        record_task(path="local", entry=FakeEntry("local-x", "local"), prompt="hi", response="yo",
                    log_path=self.log, pricing=FIXED)
        rec = self._read()[0]
        self.assertNotIn("task_id", rec)
        self.assertNotIn("parent_task_id", rec)
        self.assertNotIn("linkage_lost", rec)

    def test_rollup_groups_delegates_by_parent(self):
        records = [
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p1"},
            {"kind": "delegate", "model": "cheap-sub", "parent_task_id": "p1"},
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p2"},
        ]
        by_parent = rollup(records)["delegates"]["by_parent"]
        self.assertEqual(by_parent["p1"]["count"], 2)
        self.assertEqual(by_parent["p1"]["by_backend"], {"local-x": 1, "cheap-sub": 1})
        self.assertEqual(by_parent["p2"]["count"], 1)
        self.assertNotIn("unlinked", by_parent)

    def test_rollup_unlinked_delegate_grouped_under_sentinel(self):
        # Legacy parentless delegates still get the new positive signal during rollup.
        by_parent = rollup([{"kind": "delegate", "model": "local-x"}])["delegates"]["by_parent"]
        self.assertEqual(by_parent["unlinked"]["count"], 1)

    def test_rollup_counts_lost_delegate_linkage_but_not_parentless_tasks(self):
        records = [
            {"kind": "delegate", "model": "local-x", "linkage_lost": True},
            {"kind": "delegate", "model": "local-x"},  # legacy row, before the signal existed
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p1"},
            {"kind": "task", "model": "local-x"},
        ]
        self.assertEqual(rollup(records)["delegates"]["linkage_lost"], 2)

    def test_format_shows_linked_parents(self):
        s = rollup([
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p1"},
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p2"},
            {"kind": "delegate", "model": "local-x"},
        ])
        out = format_rollup(s, FIXED)
        self.assertIn("Linked to:", out)
        self.assertIn("2 parent task(s)", out)
        self.assertIn("Linkage lost: 1", out)
        self.assertNotIn("unlinked", out)

    def test_format_all_unlinked_reads_cleanly(self):
        # Lost linkage is a separate lifetime signal; the parent tree remains window-scoped.
        s = rollup([{"kind": "delegate", "model": "local-x"},
                    {"kind": "delegate", "model": "local-x"}])
        out = format_rollup(s, FIXED)
        self.assertIn("Linkage lost: 2", out)
        self.assertNotIn("Linked to:", out)
        self.assertNotIn("parent task(s)", out)

    def test_format_shows_delegate_section_when_present(self):
        s = rollup([
            {"kind": "task", "tier": "sub", "in_tokens_est": 1, "out_tokens_est": 1,
             "cloud_equiv_usd": 0.1, "spend_avoided_usd": 0.1},
            {"kind": "delegate", "model": "local-x", "in_tokens_est": 50, "out_tokens_est": 50,
             "cloud_equiv_usd": 2.0},
        ])
        out = format_rollup(s, FIXED)
        self.assertIn("Delegated sub-tasks", out)
        self.assertIn("local-x", out)

    def test_format_omits_delegate_section_when_absent(self):
        s = rollup([{"kind": "task", "tier": "sub", "spend_avoided_usd": 0.1}])
        self.assertNotIn("Delegated sub-tasks", format_rollup(s, FIXED))

    def test_concurrent_appends_are_serialized(self):
        import threading

        def worker(n):
            record_task(path="delegate", entry=FakeEntry(f"m{n}", "local"),
                        prompt="p", response="r", kind="delegate", log_path=self.log, pricing=FIXED)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        recs = self._read()
        self.assertEqual(len(recs), 20)  # 20 well-formed lines — no interleaved/corrupted writes
        self.assertTrue(all(r.get("kind") == "delegate" for r in recs))


# A fully-populated totals file: every field the format carries, with a distinct value per field so
# a reader that crosses two of them shows up as a wrong number rather than a coincidence.
FULL_TOTALS = {
    "tasks": 11,
    "failures": 2,
    "lost_attempts": 3,
    "by_tier": {"local": 7, "cli": 4},
    "by_origin": {"cli": 6, "gui": 5},
    "in_tokens_est": 1000,
    "out_tokens_est": 2000,
    "cloud_equiv_usd": 4.5,
    "spend_avoided_usd": 4.25,
    "pricing_refs": ["old-frontier", "test-frontier"],
    "delegates": {
        "count": 5,
        "linkage_lost": 2,
        "by_backend": {"m1": {"count": 5, "in_tokens_est": 50, "out_tokens_est": 60}},
        "in_tokens_est": 50,
        "out_tokens_est": 60,
        "cloud_equiv_usd": 0.75,
    },
}


class TotalsFormatTest(unittest.TestCase):
    """The `totals.json` format itself: where it lives, what it holds, how it degrades."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.totals = Path(self.tmp) / TOTALS_FILENAME

    def test_lives_beside_the_usage_log_in_the_data_tier(self):
        with patch.dict("os.environ", {"TANGLEBRAIN_STATE_DIR": self.tmp}, clear=False):
            self.assertEqual(default_totals_path().parent, default_log_path().parent)
            self.assertEqual(default_totals_path().name, TOTALS_FILENAME)

    def test_absent_file_reads_as_zeros(self):
        self.assertEqual(read_totals(self.totals), empty_totals())

    def test_corrupt_file_reads_as_zeros_and_does_not_raise(self):
        self.totals.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(read_totals(self.totals), empty_totals())

    def test_truncated_mid_write_file_reads_as_zeros(self):
        # The realistic corruption: a fold interrupted partway through writing the object.
        self.totals.write_text('{"tasks": 11, "spend_avoided_usd": 4.2', encoding="utf-8")
        self.assertEqual(read_totals(self.totals), empty_totals())

    def test_non_object_json_reads_as_zeros(self):
        self.totals.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(read_totals(self.totals), empty_totals())

    def test_every_field_round_trips(self):
        self.totals.write_text(json.dumps(FULL_TOTALS), encoding="utf-8")
        self.assertEqual(read_totals(self.totals), FULL_TOTALS)

    def test_unknown_key_is_ignored(self):
        self.totals.write_text(
            json.dumps({**FULL_TOTALS, "invented_by_a_later_version": 99}), encoding="utf-8"
        )
        got = read_totals(self.totals)
        self.assertNotIn("invented_by_a_later_version", got)
        self.assertEqual(got, FULL_TOTALS)  # and every shared field still survives

    def test_unknown_nested_key_is_ignored(self):
        raw = json.loads(json.dumps(FULL_TOTALS))
        raw["delegates"]["invented_later"] = 99
        raw["delegates"]["by_backend"]["m1"]["invented_later"] = 99
        self.totals.write_text(json.dumps(raw), encoding="utf-8")
        got = read_totals(self.totals)
        self.assertNotIn("invented_later", got["delegates"])
        self.assertNotIn("invented_later", got["delegates"]["by_backend"]["m1"])

    def test_missing_key_reads_as_zero(self):
        self.totals.write_text(json.dumps({"tasks": 11}), encoding="utf-8")
        got = read_totals(self.totals)
        self.assertEqual(got["tasks"], 11)
        self.assertEqual(got, {**empty_totals(), "tasks": 11})

    def test_bad_field_types_coerce_rather_than_raise(self):
        self.totals.write_text(
            json.dumps({
                "tasks": "eleven", "spend_avoided_usd": None, "by_tier": "not-a-map",
                "pricing_refs": "not-a-list", "delegates": {"by_backend": {"m1": "not-a-dict"}},
            }),
            encoding="utf-8",
        )
        got = read_totals(self.totals)
        self.assertEqual(got["tasks"], 0)
        self.assertEqual(got["spend_avoided_usd"], 0.0)
        self.assertEqual(got["by_tier"], {})
        self.assertEqual(got["pricing_refs"], [])
        # The model was recorded, so it stays — dropping it would understate the backend split.
        self.assertEqual(
            got["delegates"]["by_backend"]["m1"],
            {"count": 0, "in_tokens_est": 0, "out_tokens_est": 0},
        )

    def test_pricing_refs_are_sorted_and_deduplicated(self):
        self.totals.write_text(json.dumps({"pricing_refs": ["b", "a", "b"]}), encoding="utf-8")
        self.assertEqual(read_totals(self.totals)["pricing_refs"], ["a", "b"])

    def test_empty_totals_is_not_shared_between_callers(self):
        first = empty_totals()
        first["tasks"] = 99
        first["by_tier"]["local"] = 99
        self.assertEqual(empty_totals()["tasks"], 0)
        self.assertEqual(empty_totals()["by_tier"], {})

    def test_every_lifetime_rollup_field_has_a_home_in_totals(self):
        """The anti-drift mechanism for two field lists that must agree.

        `rollup` and `empty_totals` describe the same aggregate in two modules. A field added to
        one and not the other is a figure that silently becomes window-scoped while sitting under
        a lifetime headline — the contradiction the split exists to prevent. Asserting the
        correspondence is what keeps it an invariant rather than a habit; `by_parent` is the one
        recorded exception, and naming it here is what makes a *second* exception fail.
        """
        summary = rollup([])
        self.assertEqual(set(summary) - set(empty_totals()), set())
        self.assertEqual(
            set(summary["delegates"]) - set(empty_totals()["delegates"]), {"by_parent"}
        )


class RollupReadsTotalsPlusRowsTest(unittest.TestCase):
    """The read half: the headline is stored totals plus the rows still on disk."""

    def _rows(self):
        return [
            {"kind": "task", "tier": "local", "origin": "cli", "in_tokens_est": 10,
             "out_tokens_est": 20, "cloud_equiv_usd": 0.5, "spend_avoided_usd": 0.5,
             "pricing_ref": "test-frontier"},
            {"kind": "delegate", "model": "m1", "parent_task_id": "t1", "in_tokens_est": 5,
             "out_tokens_est": 6, "cloud_equiv_usd": 0.25, "pricing_ref": "test-frontier"},
        ]

    def test_no_totals_rolls_up_to_the_window_figure(self):
        # Asserted against an explicit expected dict rather than against `rollup(rows,
        # empty_totals())` — that comparison is two spellings of the same zero argument through the
        # same normalizer, so it would hold even if every summation below were wrong.
        self.assertEqual(rollup(self._rows()), {
            "tasks": 1,
            "failures": 0,
            "lost_attempts": 0,
            "by_tier": {"local": 1},
            "by_origin": {"cli": 1},
            "in_tokens_est": 10,
            "out_tokens_est": 20,
            "cloud_equiv_usd": 0.5,
            "spend_avoided_usd": 0.5,
            "pricing_refs": ["test-frontier"],
            "delegates": {
                "count": 1,
                "linkage_lost": 0,
                "by_backend": {"m1": {"count": 1, "in_tokens_est": 5, "out_tokens_est": 6}},
                "by_parent": {"t1": {"count": 1, "by_backend": {"m1": 1}}},
                "in_tokens_est": 5,
                "out_tokens_est": 6,
                "cloud_equiv_usd": 0.25,
            },
        })

    def test_scalars_and_maps_sum_across_totals_and_rows(self):
        got = rollup(self._rows(), FULL_TOTALS)
        self.assertEqual(got["tasks"], 12)                      # 11 stored + 1 row
        self.assertEqual(got["in_tokens_est"], 1010)
        self.assertEqual(got["out_tokens_est"], 2020)
        self.assertEqual(got["spend_avoided_usd"], 4.75)
        self.assertEqual(got["cloud_equiv_usd"], 5.0)
        self.assertEqual(got["by_tier"], {"local": 8, "cli": 4})
        self.assertEqual(got["by_origin"], {"cli": 7, "gui": 5})
        self.assertEqual(got["delegates"]["count"], 6)
        self.assertEqual(got["delegates"]["linkage_lost"], 2)
        self.assertEqual(
            got["delegates"]["by_backend"]["m1"],
            {"count": 6, "in_tokens_est": 55, "out_tokens_est": 66},
        )

    def test_by_parent_covers_the_window_only(self):
        # Never seeded from totals, because one key per parent task id cannot be folded.
        got = rollup(self._rows(), FULL_TOTALS)
        self.assertEqual(set(got["delegates"]["by_parent"]), {"t1"})

    def test_pricing_refs_merge_totals_and_rows(self):
        got = rollup(self._rows(), FULL_TOTALS)
        self.assertEqual(got["pricing_refs"], ["old-frontier", "test-frontier"])

    def test_pricing_refs_from_rows_alone(self):
        rows = self._rows() + [{"kind": "task", "pricing_ref": "newer-frontier"}]
        self.assertEqual(rollup(rows)["pricing_refs"], ["newer-frontier", "test-frontier"])

    def test_failure_record_does_not_widen_the_pricing_span(self):
        # A failure priced nothing, so it must not caveat a figure it never contributed to.
        rows = [{"kind": "failure", "pricing_ref": "never-charged"}]
        self.assertEqual(rollup(rows)["pricing_refs"], [])

    def test_rollup_does_not_mutate_the_totals_it_was_given(self):
        totals = json.loads(json.dumps(FULL_TOTALS))
        rollup(self._rows(), totals)
        self.assertEqual(totals, FULL_TOTALS)

    def test_corrupt_totals_degrades_to_the_window_figure(self):
        tmp = Path(tempfile.mkdtemp()) / TOTALS_FILENAME
        tmp.write_text("{broken", encoding="utf-8")
        rows = self._rows()
        self.assertEqual(rollup(rows, read_totals(tmp)), rollup(rows))

    def test_a_malformed_totals_argument_degrades_to_the_window_figure(self):
        # `rollup` normalizes what it is handed rather than trusting it: it is the function whose
        # failure blanks the headline, so no caller can crash it with a bad shape.
        rows = self._rows()
        self.assertEqual(
            rollup(rows, {"tasks": "x", "by_tier": "nope", "delegates": 7}), rollup(rows)
        )

    def test_totals_from_a_newer_version_still_sum(self):
        raw = {**FULL_TOTALS, "some_future_field": 5}
        self.assertEqual(rollup(self._rows(), normalize_totals(raw))["tasks"], 12)


class WindowVersusLifetimeLabellingTest(unittest.TestCase):
    """Every figure in the block is lifetime except one, and the block says which."""

    def _summary(self):
        return rollup([{"kind": "delegate", "model": "m1", "parent_task_id": "t1"}], FULL_TOTALS)

    def test_heading_claims_lifetime(self):
        self.assertIn("lifetime", format_rollup(self._summary(), FIXED).splitlines()[0])

    def test_parent_tree_is_labelled_window_scoped(self):
        out = format_rollup(self._summary(), FIXED)
        self.assertIn("Linked to:", out)
        self.assertIn("(within the current row window)", out)

    def test_panel_labels_the_parent_tree_window_scoped_too(self):
        # The GUI renders the same dict, so it inherits the same contradiction if left unlabelled.
        panel = (Path(__file__).resolve().parents[1]
                 / "tanglebrain" / "gui" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("(current row window)", panel)
        self.assertIn("(local rollup, lifetime)", panel)


def _generated_records(rnd: random.Random, count: int) -> list[dict]:
    """Build a batch of plausible usage records with every field the rollup reads.

    Random rather than hand-written because the invariant under test — the figure does not move
    when rows do — has to hold across mixtures of kinds, tiers, origins and pricing revisions, not
    just the one arrangement an author happens to picture. Values are drawn from a seeded
    generator so a failure is reproducible from the seed printed with it.
    """
    kinds = ["task", "task", "task", "task", "delegate", "failure"]
    records = []
    for i in range(count):
        kind = rnd.choice(kinds)
        in_tok, out_tok = rnd.randrange(0, 5000), rnd.randrange(0, 5000)
        equiv = round(rnd.uniform(0, 0.5), 6)
        record = {
            "ts": f"2026-01-01T00:{i:02d}:00+00:00",
            "kind": kind,
            "path": rnd.choice(["router", "local", "model", "delegate"]),
            "tier": rnd.choice(["local", "cli", "api"]),
            "model": rnd.choice(["m1", "m2", "m3"]),
            "in_tokens_est": in_tok,
            "out_tokens_est": out_tok,
            "cloud_equiv_usd": equiv,
            "spend_avoided_usd": 0.0 if kind == "failure" else equiv,
            "pricing_ref": rnd.choice(["frontier-a", "frontier-b"]),
        }
        if kind == "delegate" and rnd.random() < 0.7:
            record["parent_task_id"] = rnd.choice(["t1", "t2", "t3"])
        if kind == "task":
            record["origin"] = rnd.choice(["cli", "gui", "serve"])
        if rnd.random() < 0.2:
            record["failures"] = [{"entry": "e1", "error": "boom"}]
        records.append(record)
    return records


def _without_window_scope(summary: dict) -> dict:
    """Drop the one figure compaction is *allowed* to change, so the rest can be compared exactly.

    ``delegates.by_parent`` describes the rows currently on disk and nothing else — it is never
    folded, because one key per parent task id is unbounded. Comparing it across a compaction would
    assert the opposite of the design.
    """
    summary["delegates"].pop("by_parent", None)
    return summary


class CompactionTest(unittest.TestCase):
    """`compact_log`: rows move into the totals, and the headline does not move with them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / LOG_FILENAME
        self.totals = self.tmp / TOTALS_FILENAME

    def _write_log(self, records):
        self.log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def _append_log(self, records):
        with self.log.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def _figure(self):
        """The rendered figure as a reader sees it: stored totals plus whatever rows remain."""
        return rollup(read_records(self.log), read_totals(self.totals))

    def _compact(self, keep_recent):
        return compact_log(
            keep_recent=keep_recent, log_path=self.log, totals_path=self.totals
        )

    def test_the_figure_is_invariant_under_compaction(self):
        """The property: over generated logs, folding rows away changes no rendered figure.

        Exact equality, not approximate. The fold runs the same summation the read path runs, over
        the same records in the same order, so the floats are bit-identical rather than merely
        close — an assertion that would go soft the moment either side grew its own arithmetic.
        """
        for seed in range(30):
            with self.subTest(seed=seed):
                rnd = random.Random(seed)
                records = _generated_records(rnd, rnd.randrange(0, 30))
                self._write_log(records)
                self.totals.unlink(missing_ok=True)
                # Half the runs fold into a store that already holds history, half into a fresh
                # one: an implementation that ignored the stored side would pass only the latter.
                if seed % 2:
                    write_totals(FULL_TOTALS, self.totals)
                before = self._figure()
                keep = rnd.randrange(0, len(records) + 1)
                folded = self._compact(keep)
                self.assertEqual(folded, len(records) - keep)
                self.assertEqual(_without_window_scope(self._figure()),
                                 _without_window_scope(before))

    def test_the_figure_survives_many_folds_with_appends_between_them(self):
        # One fold is a different test from many: rounding or an off-by-one in the split shows up
        # only once a stored total is folded *into* rather than created.
        rnd = random.Random(4242)
        everything = []
        for _ in range(6):
            batch = _generated_records(rnd, 7)
            everything.extend(batch)
            self._append_log(batch)
            self._compact(keep_recent=3)
        self.assertEqual(len(read_records(self.log)), 3)  # the window really was bounded
        self.assertEqual(_without_window_scope(self._figure()),
                         _without_window_scope(rollup(everything)))

    def test_totals_land_before_any_row_is_dropped(self):
        """A torn compaction over-counts. This pins the direction, not merely that it is nonzero.

        Over-counting is visible in the figure and reconcilable against rows still on disk.
        Under-counting is a silent, permanent loss of the only claim the product makes about
        itself, so the two writes are ordered — and a test that could not tell them apart would
        leave the ordering free to be reversed by anyone tidying the function.

        The ordering is read **at the seam** — the figure is captured from inside the log rewrite,
        the instant before it fails — rather than from the wreckage afterwards. That is what a
        failed fold now leaves nothing to inspect: it puts the totals back. Reading it here is
        stricter, not looser, because it observes the order directly instead of inferring it from
        what survived, and a rewrite-first implementation still turns it red — the totals would not
        yet hold the folded rows when the rewrite runs.
        """
        records = _generated_records(random.Random(7), 10)
        self._write_log(records)
        truth = self._figure()
        folded_tasks = sum(1 for r in records[:9] if r["kind"] == "task")
        at_the_seam: dict = {}

        def look_then_crash(log_path, lines):
            at_the_seam.update(self._figure())
            raise OSError("crash")

        with patch("tanglebrain.measurement._rewrite_log", side_effect=look_then_crash):
            with self.assertRaises(OSError):
                self._compact(keep_recent=1)
        self.assertTrue(at_the_seam, "the rewrite was never reached")
        self.assertEqual(len(read_records(self.log)), 10)      # nothing was dropped
        self.assertEqual(at_the_seam["tasks"], truth["tasks"] + folded_tasks)
        self.assertGreater(at_the_seam["spend_avoided_usd"], truth["spend_avoided_usd"])
        # And the half-applied state does not outlive the failure — see
        # `test_a_failed_rewrite_puts_the_totals_back` for the rollback itself.
        self.assertEqual(_without_window_scope(self._figure()), _without_window_scope(truth))

    def test_a_failed_totals_write_drops_no_rows(self):
        # The other half of the ordering. If the log were rewritten first, this run would lose
        # nine rows with nothing anywhere recording them.
        records = _generated_records(random.Random(8), 10)
        self._write_log(records)
        before_bytes = self.log.read_bytes()
        truth = self._figure()
        with patch("tanglebrain.measurement.write_totals", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._compact(keep_recent=1)
        self.assertEqual(self.log.read_bytes(), before_bytes)
        self.assertFalse(self.totals.exists())
        self.assertEqual(self._figure(), truth)

    def test_an_empty_window_is_a_no_op(self):
        self._write_log([])
        self.assertEqual(self._compact(keep_recent=0), 0)
        self.assertFalse(self.totals.exists())  # no zeroed file materialized beside the log

    def test_an_absent_log_is_a_no_op(self):
        self.assertEqual(self._compact(keep_recent=0), 0)
        self.assertFalse(self.totals.exists())

    def test_a_window_already_short_enough_is_a_no_op(self):
        records = _generated_records(random.Random(9), 3)
        self._write_log(records)
        before_bytes = self.log.read_bytes()
        self.assertEqual(self._compact(keep_recent=5), 0)
        self.assertEqual(self.log.read_bytes(), before_bytes)
        self.assertFalse(self.totals.exists())

    def test_only_the_oldest_rows_are_folded(self):
        records = _generated_records(random.Random(10), 4)
        self._write_log(records)
        self.assertEqual(self._compact(keep_recent=1), 3)
        self.assertEqual(read_records(self.log), records[-1:])

    def test_kept_rows_are_written_back_verbatim(self):
        # Verbatim, not re-serialized: a field a newer TangleBrain added survives, and so does a
        # line torn by an interrupted append — the rewrite must not be the thing that deletes it.
        self.log.write_text(
            '{"kind": "task", "tier": "local", "spend_avoided_usd": 1.0}\n'
            '{"kind": "task", "tier": "local", "invented_later": 7,  "spacing": "odd"}\n'
            '{"kind": "task", "tier": "loc\n',
            encoding="utf-8",
        )
        self.assertEqual(self._compact(keep_recent=2), 1)
        self.assertEqual(
            self.log.read_text(encoding="utf-8"),
            '{"kind": "task", "tier": "local", "invented_later": 7,  "spacing": "odd"}\n'
            '{"kind": "task", "tier": "loc\n',
        )

    def test_a_kept_row_keeps_its_own_whitespace(self):
        # "Verbatim" has to mean the line as found, not the line re-spaced: the rewrite is not
        # the place to normalize a row it was only asked to keep.
        self.log.write_text(
            '{"kind": "task", "spend_avoided_usd": 1.0}\n'
            '   {"kind": "task", "spend_avoided_usd": 2.0}   \n',
            encoding="utf-8",
        )
        self.assertEqual(self._compact(keep_recent=1), 1)
        self.assertEqual(self.log.read_text(encoding="utf-8"),
                         '   {"kind": "task", "spend_avoided_usd": 2.0}   \n')
        self.assertEqual(self._figure()["tasks"], 2)  # and both rows still count

    def test_pricing_refs_survive_the_fold(self):
        # Folding destroys the per-row `pricing_ref` evidence, so the span has to be captured in
        # the totals as the rows go, or `--stats` goes blind to it after the first compaction.
        self._write_log([
            {"kind": "task", "pricing_ref": "frontier-b", "spend_avoided_usd": 1.0},
            {"kind": "task", "pricing_ref": "frontier-a", "spend_avoided_usd": 1.0},
        ])
        self._compact(keep_recent=0)
        self.assertEqual(read_totals(self.totals)["pricing_refs"], ["frontier-a", "frontier-b"])
        self.assertEqual(self._figure()["pricing_refs"], ["frontier-a", "frontier-b"])

    def test_the_parent_tree_is_never_folded(self):
        # Unbounded cardinality: one key per parent task id would grow the totals file forever.
        self._write_log([
            {"kind": "delegate", "model": "m1", "parent_task_id": f"t{i}"} for i in range(5)
        ])
        self._compact(keep_recent=0)
        self.assertNotIn("by_parent", json.loads(self.totals.read_text(encoding="utf-8"))["delegates"])
        self.assertEqual(self._figure()["delegates"]["by_parent"], {})
        self.assertEqual(self._figure()["delegates"]["count"], 5)  # the countable part did fold

    def test_a_field_written_by_a_newer_version_survives_the_fold(self):
        # The round-trip half of the forward-compatibility contract. `normalize_totals` drops what
        # it does not recognise, so without the carry-through an older TangleBrain would delete a
        # newer one's fields the first time it compacted.
        self.totals.write_text(
            json.dumps({**FULL_TOTALS, "invented_by_a_later_version": 99,
                        "delegates": {**FULL_TOTALS["delegates"], "invented_nested": 7}}),
            encoding="utf-8",
        )
        self._write_log([{"kind": "task", "tier": "local", "spend_avoided_usd": 1.0}])
        self._compact(keep_recent=0)
        stored = json.loads(self.totals.read_text(encoding="utf-8"))
        self.assertEqual(stored["invented_by_a_later_version"], 99)
        self.assertEqual(stored["delegates"]["invented_nested"], 7)
        self.assertEqual(stored["tasks"], 12)  # and the known fields still folded

    def test_a_corrupt_totals_file_refuses_the_fold_rather_than_overwriting_it(self):
        """The under-count the write ordering exists to prevent, reached without a crash.

        A present-but-unparseable totals file reads as zeros. Folding onto zeros would replace the
        damaged bytes and then delete the rows that could have reconciled them — bad-but-recoverable
        becomes permanent. Refusing keeps both halves on disk, which is the whole point.
        """
        self.totals.write_text('{"tasks": 11, "spend_avoided_usd": 4.2', encoding="utf-8")
        corrupt_bytes = self.totals.read_bytes()
        records = _generated_records(random.Random(11), 8)
        self._write_log(records)
        log_bytes = self.log.read_bytes()
        with self.assertRaises(CompactionRefusedError):
            self._compact(keep_recent=2)
        self.assertEqual(self.totals.read_bytes(), corrupt_bytes)  # damaged, not destroyed
        self.assertEqual(self.log.read_bytes(), log_bytes)         # and every row still there

    def test_an_absent_totals_file_still_folds(self):
        # Absence is the normal first-compaction case and must not be confused with corruption.
        self._write_log(_generated_records(random.Random(12), 8))
        self.assertEqual(self._compact(keep_recent=2), 6)

    def test_an_append_during_a_compaction_survives_it(self):
        """`_LOG_LOCK` held across read-fold-truncate — the chunk's stated concurrency deliverable.

        Without the lock the appending thread writes into the file `_rewrite_log` is about to
        replace, and the row is gone with no error anywhere. Deleting `with _LOG_LOCK:` turns this
        red, which is what makes the guarantee a contract rather than a comment.
        """
        self._write_log(_generated_records(random.Random(13), 6))
        inside, release = threading.Event(), threading.Event()
        real_write = write_totals

        def block_mid_compaction(totals, path=None):
            real_write(totals, path)
            inside.set()
            release.wait(5)

        with patch("tanglebrain.measurement.write_totals", side_effect=block_mid_compaction):
            compactor = threading.Thread(target=self._compact, args=(), kwargs={"keep_recent": 2})
            compactor.start()
            self.assertTrue(inside.wait(5), "compaction never reached its totals write")
            appender = threading.Thread(
                target=record_task,
                kwargs={"path": "router", "entry": None, "prompt": "x", "response": "y",
                        "origin": "arrived-mid-compaction",
                        "log_path": self.log, "pricing": FIXED},
            )
            appender.start()
            appender.join(0.5)          # blocked on the lock the compaction holds
            release.set()
            appender.join(5)
            compactor.join(5)
        rows = read_records(self.log)
        self.assertEqual(len(rows), 3, "the appended row was written into the replaced file")
        self.assertEqual(rows[-1].get("origin"), "arrived-mid-compaction")

    def test_a_failed_rewrite_puts_the_totals_back(self):
        """A half-applied fold is undone, so the figure is exactly what it was before the attempt.

        The rows were never destroyed, so restoring the totals loses nothing — and it is what makes
        "the over-count is bounded by one batch" true rather than hopeful. Asserting the *figure*
        rather than the row count is the point: the rows survive either way, and the number is what
        goes wrong.
        """
        write_totals(FULL_TOTALS, self.totals)
        self._write_log(_generated_records(random.Random(21), 10))
        before = self._figure()
        stored_before = self.totals.read_text(encoding="utf-8")
        with patch("tanglebrain.measurement._rewrite_log", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._compact(keep_recent=2)
        self.assertEqual(len(read_records(self.log)), 10, "no row was dropped")
        self.assertEqual(self.totals.read_text(encoding="utf-8"), stored_before,
                         "the totals file is byte-identical, foreign fields included")
        self.assertEqual(_without_window_scope(self._figure()), _without_window_scope(before))

    def test_a_failed_rewrite_removes_a_totals_file_the_fold_created(self):
        """Nothing existed before the attempt, so nothing should exist after it."""
        self._write_log(_generated_records(random.Random(22), 10))
        with patch("tanglebrain.measurement._rewrite_log", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._compact(keep_recent=2)
        self.assertFalse(self.totals.exists(), "a rolled-back fold left its own file behind")

    def test_a_failed_rewrite_keeps_a_totals_file_it_could_not_read(self):
        """"Absent" and "unreadable" are different, because the rollback does opposite work for them.

        Deleting a file this run merely failed to *read* would be the under-count direction — the
        rows that could reconcile it are still on disk, and the totals holding the rest are not.
        Leaving it over-counts, which is recoverable. The refusal guard makes the state unreachable
        through `compact_log` today, so the rollback is exercised directly — which also means the
        file preserved here stands for the *post-fold* one the real sequence would have written,
        not the pre-fold one. What is pinned is the helper's branch, not an end-to-end path.
        """
        self.totals.write_text('{"tasks": 5}', encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=OSError("unreadable")):
            snapshot = measurement._totals_snapshot(self.totals)
        self.assertEqual(snapshot, (True, None), "an unreadable file must not look absent")
        measurement._restore_totals(self.totals, snapshot)
        self.assertEqual(self.totals.read_text(encoding="utf-8"), '{"tasks": 5}',
                         "the rollback deleted a totals file it could not read")

    def test_a_snapshot_tells_an_absent_file_from_an_unreadable_one(self):
        self.assertEqual(measurement._totals_snapshot(self.totals), (False, None))
        self.totals.write_bytes(b"\xff\xfe not utf-8")
        self.assertEqual(measurement._totals_snapshot(self.totals), (True, None),
                         "undecodable bytes are unreadable, not absent")

    def test_a_negative_keep_is_rejected(self):
        with self.assertRaises(ValueError):
            self._compact(keep_recent=-1)


class AutomaticCompactionTest(unittest.TestCase):
    """The size cap and its trigger: recording a task is what keeps the log bounded.

    The cap is patched down to a few kilobytes throughout. That is the point of reading it from a
    named constant — a test that changes the constant changes the behaviour, so these exercise the
    real trigger rather than a test-only path beside it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / LOG_FILENAME
        self.totals = self.tmp / TOTALS_FILENAME

    def _flood(self, count, log=None):
        """Record ``count`` tasks through the real entry point, mixing the fields the rollup reads.

        Deterministic in everything the figure depends on (the ``ts`` differs run to run and is
        never summed), so the same flood run under two different caps must produce the same
        number.
        """
        for i in range(count):
            kind = ["task", "task", "task", "delegate", "failure"][i % 5]
            record_task(
                path="router",
                entry=FakeEntry(f"m{i % 3}", ["local", "cli", "api"][i % 3]),
                prompt="p" * (20 + i % 7),
                response="r" * (40 + i % 11),
                kind=kind,
                origin=["cli", "gui", "serve"][i % 3] if kind == "task" else None,
                parent_task_id=f"t{i % 4}" if kind == "delegate" else None,
                log_path=self.log if log is None else log,
                pricing=FIXED,
            )

    def _figure(self, tmp=None):
        tmp = self.tmp if tmp is None else tmp
        return rollup(read_records(tmp / LOG_FILENAME), read_totals(tmp / TOTALS_FILENAME))

    def _fold_sizes(self, count, cap, budget):
        """Run a flood, returning the log's size immediately after each fold that dropped rows.

        What the retention budget bounds is the file a fold *leaves*, and the flood keeps appending
        afterwards — so the end state says nothing about it. These are the only moments the budget
        makes a claim about.
        """
        sizes = []
        real_compact = measurement.compact_log

        def spy(**kwargs):
            dropped = real_compact(**kwargs)
            if dropped:
                sizes.append(self.log.stat().st_size)
            return dropped

        with patch.object(measurement, "MAX_LOG_BYTES", cap), \
                patch.object(measurement, "KEEP_RECENT_BYTES", budget), \
                patch.object(measurement, "compact_log", side_effect=spy):
            self._flood(count)
        return sizes

    def test_the_window_stays_under_the_cap_across_a_write_flood(self):
        """The cap holds *throughout*, not merely at the end — checked after every single append.

        A trigger that fired only occasionally, or that folded to a window still over the cap,
        would pass an end-state assertion and fail this one.
        """
        cap = 4000
        with patch.object(measurement, "MAX_LOG_BYTES", cap), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 1000):
            for _ in range(200):
                self._flood(1)
                size = self.log.stat().st_size
                # One record may land on top of a full log before the check that folds it, so the
                # bound is the cap plus a single row — never a second, and never unbounded.
                self.assertLessEqual(size, cap + 512, "the row window outgrew its cap")

    def test_the_figure_is_unchanged_by_the_folds_the_cap_triggers(self):
        """The same flood under a cap it never reaches, and one it crosses repeatedly, agree exactly.

        Exact equality: the fold runs the read path's own summation over the same rows in the same
        order, so the floats are bit-identical rather than close. A tolerance here would absorb the
        drift the test exists to catch.
        """
        uncapped = self.tmp / "uncapped"
        uncapped.mkdir()
        with patch.object(measurement, "MAX_LOG_BYTES", 100 * 1024 * 1024):
            self._flood(120, log=uncapped / LOG_FILENAME)
        self.assertFalse((uncapped / TOTALS_FILENAME).exists(), "nothing should have folded")

        folds = []
        real_compact = measurement.compact_log

        def spy(**kwargs):
            dropped = real_compact(**kwargs)
            folds.append(dropped)
            return dropped

        with patch.object(measurement, "MAX_LOG_BYTES", 4000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 1000), \
                patch.object(measurement, "compact_log", side_effect=spy):
            self._flood(120)

        # N > 1: one fold is a different test from many — a stored total folded *into* is where an
        # off-by-one or a re-rounding shows up, and a single fold never reaches that state.
        self.assertGreater(sum(1 for dropped in folds if dropped), 1, "expected repeated folds")
        self.assertLess(len(read_records(self.log)), 120, "rows should have left the window")
        self.assertEqual(_without_window_scope(self._figure()),
                         _without_window_scope(self._figure(uncapped)))

    def test_the_cap_is_read_from_the_constant(self):
        """Same input, two caps, two behaviours — the number is not baked into the trigger."""
        with patch.object(measurement, "MAX_LOG_BYTES", 100 * 1024 * 1024):
            self._flood(60)
        self.assertEqual(len(read_records(self.log)), 60)
        self.assertFalse(self.totals.exists())

        with patch.object(measurement, "MAX_LOG_BYTES", 2000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 500):
            self._flood(1)
        self.assertLess(len(read_records(self.log)), 61)
        self.assertTrue(self.totals.exists())

    def test_the_retention_budget_bounds_what_a_fold_leaves(self):
        sizes = self._fold_sizes(120, cap=4000, budget=900)
        self.assertGreater(len(sizes), 1, "expected repeated folds to measure")
        # Whole rows only, so a fold lands at or under the budget and never over it.
        self.assertLessEqual(max(sizes), 900)
        # And it fills the budget rather than merely respecting it: keeping one row would satisfy
        # the bound above while throwing away the window the budget exists to preserve.
        self.assertGreater(min(sizes), 450, "the fold kept far less than the budget allows")

    def test_a_row_wider_than_the_budget_folds_the_log_empty(self):
        """A budget no single row fits in keeps nothing rather than keeping one row over it."""
        sizes = self._fold_sizes(30, cap=2000, budget=10)
        self.assertTrue(sizes and set(sizes) == {0}, f"expected empty logs, got {sizes}")
        self.assertGreater(read_totals(self.totals)["tasks"], 0, "the rows went into the totals")

    def test_the_budget_counts_the_newline_each_kept_row_is_written_back_with(self):
        """Off by one per row and a fold leaves a file over the budget it was asked to hit."""
        self.log.write_text("aaaa\nbbbb\ncccc\n", encoding="utf-8")   # 5 bytes per row
        self.assertEqual(measurement._keep_recent_for_budget(self.log, 15), 3)
        self.assertEqual(measurement._keep_recent_for_budget(self.log, 14), 2)
        self.assertEqual(measurement._keep_recent_for_budget(self.log, 4), 0)

    def test_the_totals_land_beside_the_log_the_rows_came_from(self):
        """One path override moves the pair — the trigger never reaches the real state root."""
        with patch.object(measurement, "MAX_LOG_BYTES", 2000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 500):
            self._flood(40)
        self.assertTrue(self.totals.exists())
        self.assertEqual(sorted(q.name for q in self.tmp.iterdir()),
                         sorted([LOG_FILENAME, TOTALS_FILENAME]))

    def test_a_refused_fold_leaves_every_row_and_never_raises(self):
        """A damaged `totals.json` stops the pruning, not the recording — and loses nothing.

        Compaction refuses rather than folding onto bytes it cannot read. Automatic, that is a
        silent no-op: the log keeps growing until the file is repaired, which is why the condition
        has to be visible where the operator reads the figure.
        """
        self.totals.write_text("{ not json", encoding="utf-8")
        with patch.object(measurement, "MAX_LOG_BYTES", 2000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 500):
            self._flood(60)
        self.assertEqual(len(read_records(self.log)), 60, "a refused fold drops nothing")
        self.assertEqual(self.totals.read_text(encoding="utf-8"), "{ not json")

    def test_the_trigger_swallows_a_refusal_and_a_write_failure_itself(self):
        """Caught here, not by the caller's blanket `except`, which keeps meaning "the append failed".

        Reached directly rather than through `record_task`, because that blanket catch would hide a
        regression: an escaping `CompactionRefusedError` looks exactly like a handled one from the
        outside.
        """
        with patch.object(measurement, "MAX_LOG_BYTES", 100 * 1024 * 1024):
            self._flood(30)
        with patch.object(measurement, "MAX_LOG_BYTES", 1000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 500):
            self.totals.write_text("{ not json", encoding="utf-8")
            measurement._compact_if_oversized(self.log)          # refusal: must not raise
            self.assertEqual(len(read_records(self.log)), 30)
            self.totals.unlink()
            before = self._figure()
            with patch.object(measurement, "_rewrite_log", side_effect=OSError("disk full")):
                measurement._compact_if_oversized(self.log)      # OSError: must not raise either
            self.assertEqual(len(read_records(self.log)), 30)
            self.assertEqual(_without_window_scope(self._figure()),
                             _without_window_scope(before), "a swallowed failure moved the figure")

    def test_a_failing_fold_never_breaks_the_append(self):
        """The write that matters already happened; compaction's `OSError` is swallowed at source."""
        with patch.object(measurement, "MAX_LOG_BYTES", 2000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 500), \
                patch.object(measurement, "_rewrite_log", side_effect=OSError("disk full")):
            self._flood(40)
        self.assertEqual(len(read_records(self.log)), 40, "every appended row is on disk")

    def test_a_fold_that_keeps_failing_does_not_inflate_the_figure(self):
        """The failure mode an automatic trigger creates and a manual call never could.

        A rewrite failure leaves the log over its cap, so the *next* recorded task folds the same
        rows again. Without the rollback in `compact_log` those folds land on an already-inflated
        total and the figure grows by a whole batch per task — silently, with no crash, from a
        condition that repeats (a full disk fails a megabyte-scale rewrite while a 300-byte append
        still succeeds). The comparison is against the same flood under a cap it never reaches, so
        what is asserted is the figure a user would read.
        """
        uncapped = self.tmp / "uncapped"
        uncapped.mkdir()
        with patch.object(measurement, "MAX_LOG_BYTES", 100 * 1024 * 1024):
            self._flood(60, log=uncapped / LOG_FILENAME)

        folds = []

        def count_then_fail(log_path, lines):
            folds.append(1)
            raise OSError("disk full")

        with patch.object(measurement, "MAX_LOG_BYTES", 2000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 500), \
                patch.object(measurement, "_rewrite_log", side_effect=count_then_fail):
            self._flood(60)
        self.assertGreater(len(folds), 1, "the trigger must have retried, or this proves nothing")
        self.assertEqual(len(read_records(self.log)), 60, "no row was dropped")
        self.assertEqual(_without_window_scope(self._figure()),
                         _without_window_scope(self._figure(uncapped)))

    def test_the_trigger_runs_outside_the_append_lock(self):
        """`_LOG_LOCK` is not reentrant, so a trigger inside it would hang rather than raise.

        The log is primed under a cap it cannot reach, and only the *crossing* append runs — in a
        thread joined with a timeout, so a deadlock fails the test instead of stalling the suite
        forever. Priming inside the same window would deadlock on the main thread first, where
        nothing can report it.
        """
        with patch.object(measurement, "MAX_LOG_BYTES", 100 * 1024 * 1024):
            self._flood(40)
        self.assertFalse(self.totals.exists(), "nothing should have folded while priming")
        with patch.object(measurement, "MAX_LOG_BYTES", 2000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 500):
            worker = threading.Thread(target=self._flood, args=(1,), daemon=True)
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive(), "recording deadlocked against the compaction lock")
        self.assertTrue(self.totals.exists(), "the crossing append really did fold")

    def test_a_fold_already_running_is_not_joined_by_a_second_thread(self):
        """The fan-out guard: `delegate_many` crossing the cap on N threads folds once, not N times."""
        with patch.object(measurement, "MAX_LOG_BYTES", 100 * 1024 * 1024):
            self._flood(60)
        size_before = self.log.stat().st_size
        with patch.object(measurement, "MAX_LOG_BYTES", 1000), \
                patch.object(measurement, "KEEP_RECENT_BYTES", 500):
            self.assertGreater(size_before, 1000, "the log must already be over the cap")
            with measurement._COMPACT_LOCK:
                # In a thread with a bounded join: a guard that *waited* would otherwise stall the
                # suite forever instead of failing, which is the one outcome a test cannot report.
                second = threading.Thread(
                    target=measurement._compact_if_oversized, args=(self.log,), daemon=True)
                second.start()
                second.join(10)
                self.assertFalse(second.is_alive(), "it queued behind the fold instead of skipping")
            self.assertEqual(self.log.stat().st_size, size_before, "it should have stood aside")
            measurement._compact_if_oversized(self.log)   # and folds once the lock is free
        self.assertLess(self.log.stat().st_size, size_before)

    def test_a_log_that_vanished_between_the_append_and_the_check_is_not_an_error(self):
        measurement._compact_if_oversized(self.tmp / "gone.jsonl")   # no raise, no file created
        self.assertFalse((self.tmp / TOTALS_FILENAME).exists())

    def test_the_checked_in_cap_leaves_room_for_a_useful_window(self):
        """The shipped defaults, not a patched pair: retention is strictly under the cap.

        Without that gap a fold would leave the log still over the cap and every following append
        would rewrite the whole file.
        """
        self.assertLess(KEEP_RECENT_BYTES, MAX_LOG_BYTES)


class FoldRecordsIntoTotalsTest(unittest.TestCase):
    """The pure fold, without the file I/O around it."""

    def test_reads_back_as_the_same_figure_the_rows_produced(self):
        rows = [{"kind": "task", "tier": "local", "origin": "cli", "in_tokens_est": 10,
                 "out_tokens_est": 20, "cloud_equiv_usd": 0.5, "spend_avoided_usd": 0.5,
                 "pricing_ref": "test-frontier"}]
        self.assertEqual(_without_window_scope(rollup([], fold_records_into_totals(rows))),
                         _without_window_scope(rollup(rows)))

    def test_drops_the_unbounded_parent_tree(self):
        folded = fold_records_into_totals([{"kind": "delegate", "parent_task_id": "t1"}])
        self.assertNotIn("by_parent", folded["delegates"])

    def test_produces_exactly_the_stored_shape(self):
        # A field the fold invents but `empty_totals` has no home for would be silently dropped on
        # the next read — the figure would shrink with no error anywhere.
        self.assertEqual(set(fold_records_into_totals([])), set(empty_totals()))

    def test_does_not_mutate_the_totals_it_was_given(self):
        totals = json.loads(json.dumps(FULL_TOTALS))
        fold_records_into_totals([{"kind": "task", "tier": "local"}], totals)
        self.assertEqual(totals, FULL_TOTALS)


class TotalsWriterTest(unittest.TestCase):
    """`write_totals`: atomic, and non-destructive of fields this version has never heard of."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.totals = self.tmp / TOTALS_FILENAME

    def test_round_trips_through_the_reader(self):
        write_totals(FULL_TOTALS, self.totals)
        self.assertEqual(read_totals(self.totals), FULL_TOTALS)

    def test_leaves_no_staging_file_behind(self):
        write_totals(FULL_TOTALS, self.totals)
        self.assertEqual([p.name for p in self.tmp.iterdir()], [TOTALS_FILENAME])

    def test_a_failed_write_leaves_the_previous_totals_whole(self):
        write_totals(FULL_TOTALS, self.totals)
        with patch("tanglebrain.atomic.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_totals({**FULL_TOTALS, "tasks": 999}, self.totals)
        self.assertEqual(read_totals(self.totals), FULL_TOTALS)

    def test_carries_unknown_fields_through_from_the_file_it_replaces(self):
        self.totals.write_text(json.dumps({"tasks": 1, "from_the_future": {"a": 1}}),
                               encoding="utf-8")
        write_totals(empty_totals(), self.totals)
        self.assertEqual(json.loads(self.totals.read_text(encoding="utf-8"))["from_the_future"],
                         {"a": 1})

    def test_a_corrupt_previous_file_is_simply_replaced(self):
        self.totals.write_text("{not json", encoding="utf-8")
        write_totals(FULL_TOTALS, self.totals)
        self.assertEqual(read_totals(self.totals), FULL_TOTALS)


class CarryUnknownFieldsTest(unittest.TestCase):
    """The merge rule behind the round-trip, in isolation."""

    def test_known_fields_take_the_computed_value(self):
        self.assertEqual(carry_unknown_fields({"tasks": 1}, {"tasks": 9}), {"tasks": 9})

    def test_unknown_fields_ride_through_at_any_depth(self):
        got = carry_unknown_fields(
            {"top": 1, "delegates": {"count": 1, "deep": {"deeper": 2}}},
            {"delegates": {"count": 9}},
        )
        self.assertEqual(got, {"top": 1, "delegates": {"count": 9, "deep": {"deeper": 2}}})

    def test_a_stored_garbage_value_is_not_carried_back_over_its_coerced_form(self):
        # `normalize_totals` already turned this into a usable zero; restoring the garbage would
        # undo the coercion the whole format depends on.
        self.assertEqual(carry_unknown_fields({"by_tier": "not-a-map"}, {"by_tier": {}}),
                         {"by_tier": {}})

    def test_a_deliberately_unstored_key_is_dropped_rather_than_carried(self):
        # `by_parent` holds one entry per parent task id. If a stored file ever carried it, a
        # predicate that only asks "did this run compute it" would preserve it forever — unbounded
        # growth in the one file whose size the totals/window split rests on.
        self.assertEqual(NOT_PERSISTED[("delegates",)], frozenset({"by_parent"}))
        got = carry_unknown_fields(
            {"delegates": {"count": 1, "by_parent": {"t1": {"count": 1}}}},
            {"delegates": {"count": 9}},
        )
        self.assertNotIn("by_parent", got["delegates"])

    def test_a_fold_never_persists_the_parent_tree_even_if_the_file_had_one(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        totals = tmp / TOTALS_FILENAME
        totals.write_text(json.dumps({"delegates": {"by_parent": {"t1": {"count": 1}}}}),
                          encoding="utf-8")
        write_totals(fold_records_into_totals([{"kind": "delegate", "parent_task_id": "t2"}]),
                     totals)
        self.assertNotIn("by_parent", json.loads(totals.read_text(encoding="utf-8"))["delegates"])

    def test_a_non_object_prior_file_carries_nothing(self):
        self.assertEqual(carry_unknown_fields([1, 2, 3], {"tasks": 1}), {"tasks": 1})
        self.assertEqual(carry_unknown_fields(None, {"tasks": 1}), {"tasks": 1})

    def test_neither_argument_is_mutated(self):
        raw, totals = {"future": 1}, {"tasks": 9}
        carry_unknown_fields(raw, totals)
        self.assertEqual((raw, totals), ({"future": 1}, {"tasks": 9}))


class PersistedRecordCarriesNoResponseTextTest(unittest.TestCase):
    """The usage log must never contain prompt or response text — enforced, not asserted.

    `docs/design/data-model.md` § Invariants states this as a guarantee and grounds it in being
    structural: "a redaction filter can be bypassed by the next code path that forgets it; there
    is nothing to redact cannot". A review-only mechanism is invisible between reviews, and a real
    leak survived that way through `failures`. This test is what replaced it.

    This drives the whole path an operator actually hits: a backend returns output the adapter
    cannot parse, the router keeps `str(exc)` as a failure, and `record_task` persists it. The
    test fails if any future adapter reintroduces a body into an error message, which is the
    mechanism the guarantee was missing.
    """

    BODY = "Zaphod Beeblebrox ate the last Vogon poetry anthology"
    PROMPT = "Marvin's diagnostic subroutine returned melancholy"

    def _log_after_failed_parse(self, stdout: str) -> str:
        """Route one unparseable backend response through to a persisted record.

        Args:
            stdout: What the backend returned and the adapter could not parse.

        Returns:
            The raw text of the usage log written for that task.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = Path(tmp) / "usage.jsonl"
        try:
            _parse_json_field(stdout, "text", label="local-ollama")
        except AdapterError as exc:
            # Exactly what router.py does when a candidate fails.
            failures = [("local-ollama", str(exc))]
        else:  # pragma: no cover - the fixture must actually reach a failure
            self.fail("fixture did not produce an AdapterError")
        record_task(
            path="router", entry=None, prompt=self.PROMPT, response="",
            kind="failure", failures=failures, log_path=log,
        )
        return log.read_text(encoding="utf-8")

    def test_response_text_is_absent_from_the_persisted_record(self):
        written = self._log_after_failed_parse(self.BODY)
        self.assertNotIn(self.BODY, written)
        self.assertNotIn("Zaphod", written)

    def test_the_failure_is_still_recorded_and_still_diagnostic(self):
        # The guarantee must not be bought by dropping the signal #100 added.
        written = self._log_after_failed_parse(self.BODY)
        record = json.loads(written.strip())
        self.assertEqual(record["kind"], "failure")
        self.assertEqual(record["failures"][0]["entry"], "local-ollama")
        self.assertIn("not valid JSON", record["failures"][0]["error"])

    def test_prompt_text_is_absent_from_the_persisted_record(self):
        # Prompts reach only `estimate_tokens`, but assert it rather than trust it: this is the
        # half of the guarantee with the most code paths feeding it.
        written = self._log_after_failed_parse(self.BODY)
        self.assertNotIn(self.PROMPT, written)
        self.assertNotIn("Marvin", written)




@unittest.skipIf(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    "root ignores the mode bits this probe reads, so every unwritable case would read as healthy",
)
class MeasurementHealthTest(unittest.TestCase):
    """The `--stats` health probe: what it reports, what it stays quiet about, and its wording.

    Every unwritable case is driven by a real mode change on a real temp path rather than a patched
    `os.access`, because the thing under test is whether the probe asks the operating system the
    right question about the right path. A mocked answer would pass against a probe that checked
    the wrong file.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.log = self.dir / LOG_FILENAME
        self.totals = self.dir / "totals.json"

    def _cleanup(self):
        # Restore write permission first: a 0o500 directory cannot have its children unlinked.
        with contextlib.suppress(OSError):
            self.dir.chmod(0o700)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _probe(self):
        return probe_measurement_health(log_path=self.log, totals_path=self.totals)

    # --- quiet cases: the states that are ordinary rather than damaged -------------------------

    def test_healthy_store_reports_nothing(self):
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        self.totals.write_text(json.dumps({"tasks": 1}), encoding="utf-8")
        self.assertEqual(self._probe(), [])

    def test_missing_log_directory_is_not_degraded(self):
        # A fresh install has never routed a task, so it has no log and no directory. Reporting
        # that as damage would fire the health line on every clean machine, which is how an
        # honesty signal gets trained out of the reader.
        absent = self.dir / "never-created"
        self.assertEqual(
            probe_measurement_health(
                log_path=absent / LOG_FILENAME, totals_path=absent / "totals.json"
            ),
            [],
        )

    def test_a_readonly_state_root_with_no_log_directory_yet_is_reported(self):
        # `record_task` appends through `mkdir(parents=True, exist_ok=True)`, so the append
        # succeeds exactly when the nearest EXISTING ancestor is writable. A probe that looked only
        # at `log.parent` reported nothing here — every append raises and the rollup stays clean
        # forever, which is the silence this whole feature exists to break.
        self.dir.chmod(0o500)
        absent = self.dir / "not-created-yet"
        findings = probe_measurement_health(
            log_path=absent / LOG_FILENAME, totals_path=absent / "totals.json"
        )
        self.assertEqual(len(findings), 1)
        self.assertIn(str(self.dir), findings[0])
        self.assertIn("nearest existing parent", findings[0])

    def test_missing_totals_is_not_degraded(self):
        # Absence is the normal state of a log that has never crossed the compaction cap.
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        self.assertFalse(self.totals.exists())
        self.assertEqual(self._probe(), [])

    # --- damaged cases: each one exercised in the damaged state, not the fallback --------------

    def test_unwritable_log_directory_is_reported(self):
        self.dir.chmod(0o500)
        findings = self._probe()
        self.assertEqual(len(findings), 1)
        self.assertIn("directory", findings[0])
        self.assertIn(str(self.dir), findings[0])

    def test_unwritable_log_file_is_reported(self):
        # The append target once the log exists is the file, not the directory — a writable
        # directory holding a read-only log still loses every task.
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        self.log.chmod(0o400)
        findings = self._probe()
        self.assertEqual(len(findings), 1)
        self.assertIn(str(self.log), findings[0])

    def test_present_but_unreadable_totals_is_reported(self):
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        self.totals.write_text("{not json at all", encoding="utf-8")
        findings = self._probe()
        self.assertEqual(len(findings), 1)
        self.assertIn(str(self.totals), findings[0])

    def test_totals_that_parses_but_is_not_an_object_is_reported(self):
        # Compaction refuses on anything that is not a dict, so the probe's condition has to be
        # the same predicate — a JSON array is present, parses, and is still unusable.
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        self.totals.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(len(self._probe()), 1)

    def test_the_probe_condition_is_compactions_own_refusal_condition(self):
        # If these ever disagree, one of them is lying to the operator: the health line would
        # promise pruning is fine while compaction refuses, or the reverse.
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        self.totals.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(CompactionRefusedError):
            compact_log(keep_recent=0, log_path=self.log, totals_path=self.totals)
        self.assertEqual(len(self._probe()), 1)

    def test_log_and_totals_fail_independently_and_are_named_separately(self):
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        self.totals.write_text("{not json at all", encoding="utf-8")
        self.log.chmod(0o400)
        findings = self._probe()
        self.assertEqual(len(findings), 2)
        self.assertTrue(any(str(self.log) in f for f in findings))
        self.assertTrue(any(str(self.totals) in f for f in findings))

    # --- the contract the probe owes the caller -----------------------------------------------

    def test_probe_never_raises(self):
        # `record_task` swallows everything, so a probe that raised would be invisible there and
        # fatal in `--stats`, which has no such handler. Driven through a real failure rather
        # than a patched one: the path is a directory, so every file operation on it errors.
        self.assertIsInstance(
            probe_measurement_health(log_path=self.dir, totals_path=self.dir), list
        )
        # And when path resolution itself blows up — the one step that used to sit outside the
        # guard, where a raise would have escaped into `--stats`, which has no handler.
        with patch(
            "tanglebrain.measurement.default_log_path", side_effect=RuntimeError("no state root")
        ):
            findings = probe_measurement_health()
        self.assertEqual(len(findings), 1)
        self.assertIn("could not be located", findings[0])

    def test_an_unprobeable_store_reports_rather_than_going_quiet(self):
        # Silence renders identically to a healthy store, so a swallowed probe failure would make
        # "I could not tell" look like "all well" — the one substitution this signal must not make.
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        with patch("tanglebrain.measurement.os.access", side_effect=PermissionError("boom")):
            findings = self._probe()
        self.assertEqual(len(findings), 1)
        self.assertIn("could not be checked", findings[0])
        self.assertIn("PermissionError", findings[0])

    def test_findings_state_the_check_not_a_guarantee(self):
        # "no writes lost" is a completeness claim with no mechanism behind it; the probe knows
        # only what it asked the filesystem, at the moment it asked.
        self.log.write_text('{"kind": "task"}\n', encoding="utf-8")
        self.totals.write_text("{not json at all", encoding="utf-8")
        self.log.chmod(0o400)
        for finding in self._probe():
            self.assertIn("checked", finding)
            for overclaim in ("no writes lost", "all writes", "every write", "guarantee"):
                self.assertNotIn(overclaim, finding)


class HealthLineRenderingTest(unittest.TestCase):
    """How the health findings render into the `--stats` block."""

    def test_healthy_rollup_keeps_todays_shape(self):
        # An all-green store renders byte-identically to a rollup that knows nothing about health,
        # matching the existing failure-line and origin-line idiom.
        summary = rollup([{"kind": "task", "model": "m1"}])
        self.assertEqual(
            format_rollup(summary, FIXED), format_rollup(summary, FIXED, health=[])
        )

    def test_findings_render_under_the_measurement_topic(self):
        out = format_rollup(
            rollup([{"kind": "task", "model": "m1"}]), FIXED, health=["the log is unwritable"]
        )
        self.assertIn("measurement:", out)
        self.assertIn("the log is unwritable", out)

    def test_each_finding_gets_its_own_line(self):
        out = format_rollup(
            rollup([{"kind": "task", "model": "m1"}]), FIXED, health=["first thing", "second thing"]
        )
        rendered = [ln for ln in out.splitlines() if "measurement:" in ln]
        self.assertEqual(len(rendered), 2)

    def test_long_findings_wrap_with_a_hanging_indent(self):
        # Found by running `--stats` against a damaged store rather than by reading the code: a
        # real finding names an absolute path and a consequence and runs past 200 characters, and
        # unwrapped it folds at the terminal edge with no indent — a block meant to read as
        # informative arriving looking like a stack trace.
        finding = (
            "the lifetime totals file cannot be read — checked that "
            "/Users/someone/Library/Application Support/tanglebrain/totals.json parses as an "
            "object; the figures above cover only the rows still on disk, and compaction is "
            "refusing to fold, so the log is no longer being pruned"
        )
        out = format_rollup(rollup([{"kind": "task", "model": "m1"}]), FIXED, health=[finding])
        body = [ln for ln in out.splitlines() if "measurement:" in ln or ln.startswith("      ")]
        self.assertGreater(len(body), 1, "a finding this long must wrap")
        for line in body:
            self.assertLessEqual(len(line), 100, f"line runs long: {line!r}")
        self.assertTrue(
            all(ln.startswith("      ") for ln in body[1:]),
            "continuations must sit indented under their own bullet",
        )

    def test_a_long_path_is_never_broken_across_lines(self):
        # The contract is wrap-at-word-boundaries, NOT hard-fold. A path is one unbreakable token:
        # splitting it would keep every line under the width and make the single most useful thing
        # in the finding impossible to copy. So an over-long path overflows the width on purpose —
        # this pins that choice rather than the width.
        path = "/" + "deeply-nested-directory/" * 6 + "totals.json"
        out = format_rollup(
            rollup([{"kind": "task", "model": "m1"}]),
            FIXED,
            health=[f"the lifetime totals file cannot be read — checked that {path} parses"],
        )
        self.assertIn(path, out)
        self.assertTrue(
            any(len(ln) > 96 for ln in out.splitlines()),
            "the unbreakable path is expected to overflow rather than be mangled",
        )

    def test_the_health_line_uses_the_warning_glyph(self):
        # The opposite of the pricing-span note, and deliberately so. That note marks a benign,
        # expected state, so it takes the informational glyph. This line renders only when a
        # check actually failed — tasks are not being recorded, or the headline is understated —
        # so it takes the warning glyph. A real fault wearing the benign glyph is the same defect
        # as a benign state wearing the warning one, pointed the other way.
        out = format_rollup(
            rollup([{"kind": "task", "model": "m1"}]), FIXED, health=["the log is unwritable"]
        )
        self.assertIn("⚠ measurement:", out)
        self.assertNotIn("ℹ measurement:", out)


if __name__ == "__main__":
    unittest.main()
