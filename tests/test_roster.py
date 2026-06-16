"""Tests for the roster config loader (tanglebrain/roster.py)."""
from __future__ import annotations

import os
import tempfile
import unittest

from tanglebrain.roster import (
    Roster,
    RosterError,
    default_roster_path,
    load_roster,
)


def write_yaml(text: str, test: unittest.TestCase) -> str:
    """Write YAML to a temp file and return its path, registering cleanup on the test."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(text)
    handle.close()
    test.addCleanup(os.unlink, handle.name)
    return handle.name


class PackagedRosterTest(unittest.TestCase):
    """The roster shipped with the package parses and matches plan §5."""

    def setUp(self):
        self.roster = load_roster()

    def test_default_path_points_at_packaged_yaml(self):
        self.assertTrue(default_roster_path().exists())
        self.assertEqual(default_roster_path().name, "roster.yaml")

    def test_has_four_entries(self):
        self.assertEqual(len(self.roster), 4)

    def test_entry_ids_in_declared_order(self):
        ids = [e.id for e in self.roster]
        self.assertEqual(ids, ["gpt-oss-120b", "claude", "codex", "gemini"])

    def test_local_entry_is_openai_compat_with_key_ref(self):
        local = self.roster.by_id("gpt-oss-120b")
        self.assertEqual(local.tier, "local")
        self.assertEqual(local.invoke.kind, "openai-compat")
        self.assertTrue(local.invoke.base_url.endswith("/v1"))
        self.assertEqual(local.invoke.model, "gpt-oss-120b")
        self.assertEqual(local.invoke.key_ref, "file:~/.config/tanglebrain/tanglebrain-spike.key")

    def test_claude_entry_scrubs_anthropic_key_and_can_orchestrate(self):
        claude = self.roster.by_id("claude")
        self.assertEqual(claude.invoke.kind, "cli")
        self.assertIn("ANTHROPIC_API_KEY", claude.invoke.scrub_env)
        self.assertTrue(claude.can_orchestrate)

    def test_orchestrators_are_the_three_subs(self):
        self.assertEqual([e.id for e in self.roster.orchestrators()], ["claude", "codex", "gemini"])

    def test_in_tier_local(self):
        self.assertEqual([e.id for e in self.roster.in_tier("local")], ["gpt-oss-120b"])


class LoaderValidationTest(unittest.TestCase):
    """The loader rejects malformed rosters with clear RosterErrors."""

    def test_missing_file(self):
        with self.assertRaises(RosterError):
            load_roster("/no/such/roster.yaml")

    def test_not_a_list(self):
        path = write_yaml("id: solo\ntier: local\n", self)
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_entry_missing_id(self):
        path = write_yaml("- tier: local\n  invoke: {kind: openai-compat, base_url: x, model: y}\n", self)
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_entry_missing_tier(self):
        path = write_yaml("- id: a\n  invoke: {kind: openai-compat, base_url: x, model: y}\n", self)
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_invalid_tier_rejected(self):
        path = write_yaml("- id: a\n  tier: locl\n  invoke: {kind: openai-compat, base_url: x, model: y}\n", self)
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_unknown_invoke_kind(self):
        path = write_yaml("- id: a\n  tier: local\n  invoke: {kind: telepathy}\n", self)
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_openai_compat_requires_base_url_and_model(self):
        path = write_yaml("- id: a\n  tier: local\n  invoke: {kind: openai-compat, model: y}\n", self)
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_cli_requires_cmd(self):
        path = write_yaml("- id: a\n  tier: sub\n  invoke: {kind: cli}\n", self)
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_duplicate_ids_rejected(self):
        path = write_yaml(
            "- id: dup\n  tier: local\n  invoke: {kind: openai-compat, base_url: x, model: y}\n"
            "- id: dup\n  tier: sub\n  invoke: {kind: cli, cmd: [echo]}\n",
            self,
        )
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_by_id_unknown_raises_keyerror(self):
        roster = Roster([])
        with self.assertRaises(KeyError):
            roster.by_id("nope")


if __name__ == "__main__":
    unittest.main()
