"""Tests for the roster config loader (tanglebrain/roster.py)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tanglebrain.roster import (
    ROSTER_ENV_VAR,
    Roster,
    RosterError,
    default_roster_path,
    load_roster,
    packaged_roster_path,
)


def write_yaml(text: str, test: unittest.TestCase) -> str:
    """Write YAML to a temp file and return its path, registering cleanup on the test."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(text)
    handle.close()
    test.addCleanup(os.unlink, handle.name)
    return handle.name


class PackagedRosterTest(unittest.TestCase):
    """The GENERIC example roster shipped with the package parses correctly.

    R2a (public-OSS rollout): the bundled default ships exactly ONE active entry — the free local
    tier — and no orchestrators. The opt-in subscription/authenticated-CLI tier (claude/codex/gemini)
    and the paid-API tier ship as COMMENTED examples, so a fresh clone routes to local out of the box
    and an operator opts in by uncommenting. Pinned to ``packaged_roster_path()`` so it tests the
    bundled example regardless of any operator roster the dev machine may have at
    ``~/.config/tanglebrain/roster.yaml``.
    """

    def setUp(self):
        self.roster = load_roster(packaged_roster_path())

    def test_packaged_path_points_at_bundled_yaml(self):
        self.assertTrue(packaged_roster_path().exists())
        self.assertEqual(packaged_roster_path().name, "roster.yaml")

    def test_ships_one_active_entry_the_local_tier(self):
        # The only ACTIVE (uncommented) entry is the free local tier.
        self.assertEqual(len(self.roster), 1)
        self.assertEqual([e.id for e in self.roster], ["local-ollama"])

    def test_local_entry_is_generic_ollama_no_key(self):
        local = self.roster.by_id("local-ollama")
        self.assertEqual(local.tier, "local")
        self.assertEqual(local.invoke.kind, "openai-compat")
        self.assertTrue(local.invoke.base_url.endswith("/v1"))
        self.assertEqual(local.invoke.model, "llama3.2")
        self.assertIsNone(local.invoke.key_ref)  # generic Ollama needs no auth

    def test_no_active_orchestrators_by_default(self):
        # The sub entries are commented out, so the packaged default has nothing to rotate over.
        # A bare `tanglebrain "…"` therefore errors out cleanly until an operator opts a sub in;
        # `--local` works out of the box.
        self.assertEqual(self.roster.orchestrators(), [])

    def test_in_tier_local(self):
        self.assertEqual([e.id for e in self.roster.in_tier("local")], ["local-ollama"])

    def test_opt_in_tiers_present_as_commented_examples(self):
        # The subscription-CLI and paid-API tiers ship as commented opt-in examples, not active
        # entries — uncommenting one (and its tier-specific gate) is how an operator enables it.
        raw = packaged_roster_path().read_text()
        for marker in ("# - id: claude", "# - id: codex", "# - id: gemini", "# - id: paid-overflow"):
            self.assertIn(marker, raw, f"{marker!r} should ship as a commented opt-in example")


class RosterDiscoveryTest(unittest.TestCase):
    """default_roster_path() resolution order: env → ~/.config/tanglebrain → packaged."""

    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {ROSTER_ENV_VAR: "~/from/env.yaml"}, clear=False):
            self.assertEqual(default_roster_path(), Path("~/from/env.yaml").expanduser())

    def test_xdg_user_roster_when_no_env(self):
        tmp = tempfile.mkdtemp()
        user = Path(tmp) / "tanglebrain" / "roster.yaml"
        user.parent.mkdir(parents=True)
        user.write_text("- id: x\n  tier: local\n  invoke: {kind: openai-compat, base_url: u, model: m}\n")
        env = {k: v for k, v in os.environ.items() if k != ROSTER_ENV_VAR}
        env["XDG_CONFIG_HOME"] = tmp
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_roster_path(), user)

    def test_falls_back_to_packaged(self):
        # No env, and an XDG home with no tanglebrain/roster.yaml → packaged example.
        empty = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if k != ROSTER_ENV_VAR}
        env["XDG_CONFIG_HOME"] = empty
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_roster_path(), packaged_roster_path())


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
