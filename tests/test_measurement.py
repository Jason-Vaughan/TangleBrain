"""Tests for the C4 measurement / spend-avoided layer (tanglebrain/measurement.py).

Fully hermetic: the usage log is a temp path and pricing is injected, so nothing touches the real
~/.cache or the packaged config. Covers the estimation/cost math, the fault-tolerant log I/O, and
the rollup/format.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from tanglebrain.measurement import (
    PLACEHOLDER_PRICING,
    PRICING_HEADER,
    Pricing,
    cloud_equiv_usd,
    default_log_path,
    estimate_tokens,
    format_rollup,
    load_pricing,
    record_task,
    read_records,
    rollup,
    save_pricing,
    validate_pricing,
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
        # The packaged config/pricing.yaml carries the canonical monad-stats anchor ($3/$15).
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


if __name__ == "__main__":
    unittest.main()
