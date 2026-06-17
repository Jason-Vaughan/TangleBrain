"""Tests for the C1 local-first selector (tanglebrain/selector.py)."""
from __future__ import annotations

import unittest

from tanglebrain.adapters import ApiAdapter, AdapterError, CliAdapter, OpenAICompatAdapter
from tanglebrain.roster import Invoke, Roster, RosterEntry, load_roster
from tanglebrain.selector import (
    SelectionError,
    build_adapter,
    select_by_id,
    select_local,
)
from tanglebrain.settings import Settings


def local_entry() -> RosterEntry:
    return RosterEntry(
        id="gpt-oss-120b",
        tier="local",
        invoke=Invoke(kind="openai-compat", base_url="http://x/v1", model="gpt-oss-120b"),
    )


def cli_entry() -> RosterEntry:
    return RosterEntry(id="claude", tier="sub", invoke=Invoke(kind="cli", cmd=["claude"]))


def api_entry(enabled: bool = True) -> RosterEntry:
    return RosterEntry(
        id="gpt-5",
        tier="api",
        invoke=Invoke(kind="api", base_url="http://x/v1", model="gpt-5", key_ref="none"),
        enabled=enabled,
    )


class SelectLocalTest(unittest.TestCase):
    def test_selects_local_from_packaged_roster(self):
        entry = select_local(load_roster())
        self.assertEqual(entry.id, "gpt-oss-120b")

    def test_picks_first_invocable_local(self):
        roster = Roster([cli_entry(), local_entry()])
        self.assertEqual(select_local(roster).id, "gpt-oss-120b")

    def test_no_local_raises(self):
        with self.assertRaises(SelectionError):
            select_local(Roster([cli_entry()]))

    def test_local_but_not_openai_compat_is_not_selected(self):
        odd_local = RosterEntry(id="weird", tier="local", invoke=Invoke(kind="cli", cmd=["x"]))
        with self.assertRaises(SelectionError):
            select_local(Roster([odd_local]))


class SelectByIdTest(unittest.TestCase):
    def test_selects_named_entry(self):
        roster = Roster([local_entry(), cli_entry()])
        self.assertEqual(select_by_id(roster, "claude").id, "claude")

    def test_unknown_id_raises_with_known_ids(self):
        roster = Roster([local_entry(), cli_entry()])
        with self.assertRaises(SelectionError) as ctx:
            select_by_id(roster, "nope")
        # The error lists the ids that *are* available, to make the typo obvious.
        self.assertIn("claude", str(ctx.exception))


class BuildAdapterTest(unittest.TestCase):
    def test_builds_openai_compat_adapter(self):
        adapter = build_adapter(local_entry())
        self.assertIsInstance(adapter, OpenAICompatAdapter)

    def test_builds_cli_adapter(self):
        adapter = build_adapter(cli_entry())
        self.assertIsInstance(adapter, CliAdapter)

    def test_api_entry_inert_when_billing_disabled(self):
        # Default gate is OFF — a paid entry parses but is never routable (issue #2).
        with self.assertRaises(AdapterError) as ctx:
            build_adapter(api_entry(), settings=Settings(api_billing_enabled=False))
        self.assertIn("billing is disabled", str(ctx.exception))

    def test_api_entry_builds_when_billing_enabled(self):
        adapter = build_adapter(api_entry(), settings=Settings(api_billing_enabled=True))
        self.assertIsInstance(adapter, ApiAdapter)

    def test_api_entry_disabled_not_routable_even_when_billing_on(self):
        # The per-key kill-switch overrides the global gate: enabled=false is never routable.
        with self.assertRaises(AdapterError) as ctx:
            build_adapter(api_entry(enabled=False), settings=Settings(api_billing_enabled=True))
        self.assertIn("disabled", str(ctx.exception))

    def test_api_gate_defaults_to_disabled_when_no_settings_injected(self):
        # With no settings passed, build_adapter loads the packaged settings.yaml, which ships the
        # gate OFF — so the default posture is inert without any injection.
        with self.assertRaises(AdapterError):
            build_adapter(api_entry())


if __name__ == "__main__":
    unittest.main()
