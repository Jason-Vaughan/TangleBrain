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

    def test_cli_entries_declare_a_parser(self):
        self.assertEqual(self.roster.by_id("claude").invoke.parse, "claude-json")
        self.assertEqual(self.roster.by_id("codex").invoke.parse, "plain")
        self.assertEqual(self.roster.by_id("gemini").invoke.parse, "gemini-json")

    def test_gemini_cmd_marks_prompt_injection_point(self):
        # gemini's -p needs the prompt as its value, so the cmd carries a {prompt} token.
        self.assertIn("{prompt}", self.roster.by_id("gemini").invoke.cmd)

    def test_orchestrators_declare_delegate_args(self):
        # C3b: each orchestrator carries the per-CLI flags to inject the gpt-oss delegate.
        for entry_id in ("claude", "codex", "gemini"):
            self.assertTrue(
                self.roster.by_id(entry_id).invoke.delegate_args,
                f"{entry_id} should declare delegate_args",
            )
        # claude uses the proven --mcp-config path with the {delegate_mcp_json} token.
        self.assertIn("{delegate_mcp_json}", self.roster.by_id("claude").invoke.delegate_args)

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

    def test_parse_must_be_string(self):
        path = write_yaml(
            "- id: a\n  tier: sub\n  invoke: {kind: cli, cmd: [x], parse: [not, a, string]}\n",
            self,
        )
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_delegate_args_must_be_list(self):
        path = write_yaml(
            "- id: a\n  tier: sub\n  invoke: {kind: cli, cmd: [x], delegate_args: nope}\n",
            self,
        )
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


class ApiEntryTest(unittest.TestCase):
    """The paid-API tier (issue #2): api entries parse (but the loader requires full config)."""

    _OK = (
        "- id: gpt5\n  tier: api\n"
        "  invoke: {kind: api, base_url: 'http://x/v1', model: gpt-5, key_ref: 'file:~/k.key'}\n"
    )

    def test_api_entry_parses_with_full_invoke(self):
        roster = load_roster(write_yaml(self._OK, self))
        entry = roster.by_id("gpt5")
        self.assertEqual(entry.tier, "api")
        self.assertEqual(entry.invoke.kind, "api")
        self.assertEqual(entry.invoke.base_url, "http://x/v1")
        self.assertEqual(entry.invoke.key_ref, "file:~/k.key")
        # Defaults: enabled true, no budget recorded.
        self.assertTrue(entry.enabled)
        self.assertIsNone(entry.budget_usd_month)

    def test_api_requires_base_url_and_model(self):
        path = write_yaml(
            "- id: a\n  tier: api\n  invoke: {kind: api, key_ref: 'file:~/k.key'}\n", self
        )
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_api_requires_key_ref(self):
        # A paid entry without a key reference must be rejected — never silently keyless.
        path = write_yaml(
            "- id: a\n  tier: api\n  invoke: {kind: api, base_url: 'http://x/v1', model: m}\n", self
        )
        with self.assertRaises(RosterError) as ctx:
            load_roster(path)
        self.assertIn("key_ref", str(ctx.exception))

    def test_enabled_and_budget_parse(self):
        path = write_yaml(self._OK + "  enabled: false\n  budget_usd_month: 25\n", self)
        entry = load_roster(path).by_id("gpt5")
        self.assertFalse(entry.enabled)
        self.assertEqual(entry.budget_usd_month, 25.0)

    def test_enabled_must_be_bool(self):
        path = write_yaml(self._OK + "  enabled: yesplease\n", self)
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_budget_must_be_positive_number(self):
        for bad in ("0", "-5", "true", "'lots'"):
            path = write_yaml(self._OK + f"  budget_usd_month: {bad}\n", self)
            with self.assertRaises(RosterError):
                load_roster(path)


if __name__ == "__main__":
    unittest.main()
