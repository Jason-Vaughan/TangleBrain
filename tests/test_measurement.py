"""Tests for the measurement / spend-avoided layer (`tanglebrain/measurement.py`, `totals.py`).

Fully hermetic: the usage log and the totals file are temp paths and pricing is injected, so
nothing touches the operator's real state root or the packaged config. Covers the estimation and
cost math, the fault-tolerant log I/O, the `totals.json` format, and the rollup/format path that
sums stored lifetime totals with the current row window.
"""
from __future__ import annotations

import io
import json
import random
import shutil
import threading
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from tanglebrain.measurement import (
    LOG_FILENAME,
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
        # Must not raise despite the unwritable path.
        record_task(path="local", entry=FakeEntry("x", "local"), prompt="p", response="r",
                    log_path=bad, pricing=FIXED)


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
        self.assertNotIn("task_id", rec)

    def test_record_omits_linkage_fields_when_absent(self):
        record_task(path="local", entry=FakeEntry("local-x", "local"), prompt="hi", response="yo",
                    log_path=self.log, pricing=FIXED)
        rec = self._read()[0]
        self.assertNotIn("task_id", rec)
        self.assertNotIn("parent_task_id", rec)

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
        # A delegate with no parent_task_id (run outside a propagated task) groups under "unlinked".
        by_parent = rollup([{"kind": "delegate", "model": "local-x"}])["delegates"]["by_parent"]
        self.assertEqual(by_parent["unlinked"]["count"], 1)

    def test_format_shows_linked_parents(self):
        s = rollup([
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p1"},
            {"kind": "delegate", "model": "local-x", "parent_task_id": "p2"},
            {"kind": "delegate", "model": "local-x"},
        ])
        out = format_rollup(s, FIXED)
        self.assertIn("Linked to:", out)
        self.assertIn("2 parent task(s)", out)
        self.assertIn("1 unlinked", out)

    def test_format_all_unlinked_reads_cleanly(self):
        # When no delegate is linked, the line should read "N unlinked", not "0 parent task(s), ...".
        s = rollup([{"kind": "delegate", "model": "local-x"},
                    {"kind": "delegate", "model": "local-x"}])
        out = format_rollup(s, FIXED)
        self.assertIn("Linked to:    2 unlinked", out)
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
        """
        records = _generated_records(random.Random(7), 10)
        self._write_log(records)
        truth = self._figure()
        folded_tasks = sum(1 for r in records[:9] if r["kind"] == "task")
        with patch("tanglebrain.measurement._rewrite_log", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self._compact(keep_recent=1)
        torn = self._figure()
        self.assertEqual(len(read_records(self.log)), 10)      # nothing was dropped
        self.assertTrue(self.totals.exists())                  # the fold had already landed
        self.assertEqual(torn["tasks"], truth["tasks"] + folded_tasks)
        self.assertGreater(torn["spend_avoided_usd"], truth["spend_avoided_usd"])

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
        # the totals as the rows go — chunk 05 renders a caveat that would otherwise have no basis.
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

        appended = {"kind": "task", "tier": "local", "spend_avoided_usd": 9.0,
                    "pricing_ref": "arrived-mid-compaction"}
        with patch("tanglebrain.measurement.write_totals", side_effect=block_mid_compaction):
            compactor = threading.Thread(target=self._compact, args=(), kwargs={"keep_recent": 2})
            compactor.start()
            self.assertTrue(inside.wait(5), "compaction never reached its totals write")
            appender = threading.Thread(
                target=record_task,
                kwargs={"path": "router", "entry": None, "prompt": "x", "response": "y",
                        "log_path": self.log, "pricing": FIXED},
            )
            appender.start()
            appender.join(0.5)          # blocked on the lock the compaction holds
            release.set()
            appender.join(5)
            compactor.join(5)
        rows = read_records(self.log)
        self.assertEqual(len(rows), 3, "the appended row was written into the replaced file")
        self.assertEqual(str(appended["kind"]), rows[-1]["kind"])

    def test_a_negative_keep_is_rejected(self):
        with self.assertRaises(ValueError):
            self._compact(keep_recent=-1)


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


if __name__ == "__main__":
    unittest.main()
