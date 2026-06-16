"""Tests for the C1 local-first selector (tanglebrain/selector.py)."""
from __future__ import annotations

import unittest

from tanglebrain.adapters import AdapterError, OpenAICompatAdapter
from tanglebrain.roster import Invoke, Roster, RosterEntry, load_roster
from tanglebrain.selector import SelectionError, build_adapter, select_local


def local_entry() -> RosterEntry:
    return RosterEntry(
        id="gpt-oss-120b",
        tier="local",
        invoke=Invoke(kind="openai-compat", base_url="http://x/v1", model="gpt-oss-120b"),
    )


def cli_entry() -> RosterEntry:
    return RosterEntry(id="claude", tier="sub", invoke=Invoke(kind="cli", cmd=["claude"]))


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


class BuildAdapterTest(unittest.TestCase):
    def test_builds_openai_compat_adapter(self):
        adapter = build_adapter(local_entry())
        self.assertIsInstance(adapter, OpenAICompatAdapter)

    def test_cli_entry_has_no_adapter_yet(self):
        with self.assertRaises(AdapterError):
            build_adapter(cli_entry())


if __name__ == "__main__":
    unittest.main()
