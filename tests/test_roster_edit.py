"""Tests for the surgical, comment-preserving roster editor (tanglebrain/roster_edit.py).

The editor must touch only the targeted value on the targeted line — every comment, blank line, and
the nested invoke block must survive an edit byte-for-byte — and must never write a roster that
fails to re-parse.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tanglebrain.roster import load_roster
from tanglebrain.roster_edit import RosterEditError, render_field, save_roster_edits

# A realistic roster: dense inline comments, a nested invoke block, a trailing inline comment on
# can_orchestrate, an entry WITH enabled (api) and entries WITHOUT it (local/sub).
ROSTER = """\
# Header comment — must survive.
# Second header line.

- id: gpt-oss-120b
  tier: local
  invoke:
    kind: openai-compat
    base_url: "http://x/v1"
    model: "gpt-oss-120b"
    # a dense comment inside invoke that must not move or change
    key_ref: "file:~/k.key"
  cost: free
  good_at: [grunt, code, tools]

- id: claude
  tier: sub
  invoke:
    kind: cli
    cmd: ["claude", "-p"]
    parse: claude-json
  cost: subscription                    # informational
  good_at: [reasoning, review]
  can_orchestrate: true                 # joins the rotation

- id: gpt5
  tier: api
  invoke:
    kind: api
    base_url: "http://x/v1"
    model: "gpt-5"
    key_ref: "file:~/k5.key"
  cost: paid
  good_at: [hard]
  enabled: true
  budget_usd_month: 25
"""


class RosterEditTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Keep backups out of the real ~/.cache.
        self._env = mock.patch.dict(
            os.environ, {"TANGLEBRAIN_STATE_DIR": self.tmp}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.path = Path(self.tmp) / "roster.yaml"
        self.path.write_text(ROSTER, encoding="utf-8")

    def _text(self):
        return self.path.read_text(encoding="utf-8")

    def _entry(self, eid):
        return load_roster(self.path).by_id(eid)


class RenderFieldTest(unittest.TestCase):
    def test_booleans(self):
        self.assertEqual(render_field("enabled", True), "true")
        self.assertEqual(render_field("can_orchestrate", False), "false")

    def test_bool_field_rejects_non_bool(self):
        for bad in (1, "true", None):
            with self.assertRaises(RosterEditError):
                render_field("enabled", bad)

    def test_budget_integer_and_float(self):
        self.assertEqual(render_field("budget_usd_month", 25), "25")
        self.assertEqual(render_field("budget_usd_month", 25.0), "25")
        self.assertEqual(render_field("budget_usd_month", 12.5), "12.5")

    def test_budget_cleared_removes(self):
        self.assertIsNone(render_field("budget_usd_month", None))
        self.assertIsNone(render_field("budget_usd_month", ""))

    def test_budget_rejects_bad(self):
        for bad in (0, -5, True, "lots"):
            with self.assertRaises(RosterEditError):
                render_field("budget_usd_month", bad)

    def test_budget_rejects_non_finite(self):
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.assertRaises(RosterEditError):
                render_field("budget_usd_month", bad)

    def test_good_at_list(self):
        self.assertEqual(render_field("good_at", ["a", "b-c", "d.e"]), "[a, b-c, d.e]")

    def test_good_at_rejects_unsafe_tag(self):
        for bad in (["has space"], ["comma,bad"], [""], "notalist", [1]):
            with self.assertRaises(RosterEditError):
                render_field("good_at", bad)

    def test_unknown_field_rejected(self):
        with self.assertRaises(RosterEditError):
            render_field("tier", "local")


class ApplyEditTest(RosterEditTestBase):
    def test_update_preserves_trailing_comment(self):
        save_roster_edits("claude", {"can_orchestrate": False}, path=self.path)
        text = self._text()
        self.assertIn("can_orchestrate: false                 # joins the rotation", text)
        self.assertFalse(self._entry("claude").can_orchestrate)

    def test_insert_when_field_absent(self):
        # claude has no `enabled` line — it must be inserted at 2-space indent and parse as False.
        save_roster_edits("claude", {"enabled": False}, path=self.path)
        self.assertFalse(self._entry("claude").enabled)
        self.assertIn("  enabled: false", self._text())

    def test_budget_set_then_cleared_removes_line(self):
        save_roster_edits("claude", {"budget_usd_month": 30}, path=self.path)
        self.assertEqual(self._entry("claude").budget_usd_month, 30.0)
        save_roster_edits("claude", {"budget_usd_month": None}, path=self.path)
        self.assertIsNone(self._entry("claude").budget_usd_month)
        self.assertNotIn("budget_usd_month", _entry_block(self._text(), "claude"))

    def test_edit_good_at(self):
        save_roster_edits("gpt-oss-120b", {"good_at": ["grunt", "fast"]}, path=self.path)
        self.assertEqual(self._entry("gpt-oss-120b").good_at, ["grunt", "fast"])
        self.assertIn("  good_at: [grunt, fast]", self._text())

    def test_multiple_fields_one_call(self):
        save_roster_edits("gpt5", {"enabled": False, "budget_usd_month": 10}, path=self.path)
        e = self._entry("gpt5")
        self.assertFalse(e.enabled)
        self.assertEqual(e.budget_usd_month, 10.0)

    def test_insert_after_block_style_last_field(self):
        # A hand-written entry whose LAST field is block-style (good_at as a `-` list). Inserting an
        # absent field must append at the block END, not splice between the key and its list items.
        block_roster = (
            "- id: bs\n  tier: sub\n  invoke:\n    kind: cli\n    cmd: [\"x\"]\n"
            "  good_at:\n    - reasoning\n    - review\n"
        )
        p = Path(self.tmp) / "block.yaml"
        p.write_text(block_roster, encoding="utf-8")
        save_roster_edits("bs", {"enabled": False}, path=p)
        e = load_roster(p).by_id("bs")
        self.assertFalse(e.enabled)
        self.assertEqual(e.good_at, ["reasoning", "review"])  # list intact, not corrupted

    def test_comments_and_other_entries_preserved(self):
        before = self._text()
        save_roster_edits("claude", {"can_orchestrate": False}, path=self.path)
        after = self._text()
        # Every comment line is still present, verbatim.
        for line in before.splitlines():
            if line.lstrip().startswith("#"):
                self.assertIn(line, after, f"comment lost: {line!r}")
        # The dense invoke comment and the header are untouched.
        self.assertIn("# a dense comment inside invoke that must not move or change", after)
        self.assertIn("# Header comment — must survive.", after)
        # Only one line changed (true -> false on claude's can_orchestrate).
        diff = [(b, a) for b, a in zip(before.splitlines(), after.splitlines()) if b != a]
        self.assertEqual(len(diff), 1)


class SafetyTest(RosterEditTestBase):
    def test_unknown_entry_rejected_and_file_unchanged(self):
        before = self._text()
        with self.assertRaises(RosterEditError):
            save_roster_edits("nope", {"enabled": False}, path=self.path)
        self.assertEqual(self._text(), before)

    def test_invalid_value_rejected_and_file_unchanged(self):
        before = self._text()
        with self.assertRaises(RosterEditError):
            save_roster_edits("claude", {"budget_usd_month": -5}, path=self.path)
        self.assertEqual(self._text(), before)

    def test_empty_fields_rejected(self):
        with self.assertRaises(RosterEditError):
            save_roster_edits("claude", {}, path=self.path)

    def test_missing_file_rejected(self):
        with self.assertRaises(RosterEditError):
            save_roster_edits("claude", {"enabled": False}, path=Path(self.tmp) / "nope.yaml")

    def test_backup_written(self):
        save_roster_edits("claude", {"can_orchestrate": False}, path=self.path)
        backups = list((Path(self.tmp) / "backups").glob("roster-*.yaml"))
        self.assertEqual(len(backups), 1)
        # The backup holds the PRE-edit content.
        self.assertIn("can_orchestrate: true", backups[0].read_text())

    def test_result_always_reparses(self):
        # After any accepted edit, the file is a valid roster (the editor re-parses before writing).
        save_roster_edits("gpt5", {"good_at": ["a", "b"], "enabled": False}, path=self.path)
        roster = load_roster(self.path)  # would raise if malformed
        self.assertEqual(len(roster), 3)


def _entry_block(text: str, eid: str) -> str:
    """Return just the text of entry ``eid``'s block (for asserting a field is absent)."""
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"- id: {eid}"))
    end = start + 1
    while end < len(lines) and lines[end][:1] in (" ", "\t"):
        end += 1
    return "\n".join(lines[start:end])


if __name__ == "__main__":
    unittest.main()
