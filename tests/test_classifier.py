"""Tests for the local classifier gate (tanglebrain/classifier.py).

The local adapter is faked — no network. The load-bearing guarantee is **fail-safe to FRONTIER**:
any ambiguity or failure must resolve to frontier so the gate never traps a hard task on local.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tanglebrain.adapters.base import AdapterError
from tanglebrain.classifier import FRONTIER, TRIVIAL, _build_prompt, _parse_verdict, classify
from tanglebrain.roster import Invoke, Roster, RosterEntry


def local_roster() -> Roster:
    return Roster([RosterEntry(
        id="gpt-oss", tier="local", invoke=Invoke(kind="openai-compat", base_url="u", model="m"))])


def factory(text=None, exc=None):
    """An adapter_factory whose adapter returns ``text`` or raises ``exc``; records the run call."""
    calls = {}

    def make(entry):
        adapter = MagicMock()
        if exc is not None:
            adapter.run.side_effect = exc
        else:
            adapter.run.side_effect = lambda p, o: calls.update(prompt=p, opts=o) or text
        return adapter

    make.calls = calls
    return make


class ParseVerdictTest(unittest.TestCase):
    def test_clean_one_word_trivial(self):
        for ok in ("TRIVIAL", "trivial", "trivial.", "  Trivial\n"):
            self.assertEqual(_parse_verdict(ok), TRIVIAL, ok)

    def test_frontier_word(self):
        self.assertEqual(_parse_verdict("FRONTIER"), FRONTIER)

    def test_both_words_is_frontier(self):
        # Ambiguous → safe default.
        self.assertEqual(_parse_verdict("not trivial, this is frontier"), FRONTIER)

    def test_negation_prose_fails_safe_to_frontier(self):
        # The key strict-parse cases: a negation or prose that mentions 'trivial' must NOT leak to
        # TRIVIAL — only a clean leading one-word verdict counts.
        for prose in ("this is not trivial work", "the task is trivial", "definitely trivial imo"):
            self.assertEqual(_parse_verdict(prose), FRONTIER, prose)

    def test_neither_word_is_frontier(self):
        for junk in ("", "  ", "maybe?", "yes", "42"):
            self.assertEqual(_parse_verdict(junk), FRONTIER)


class ClassifyTest(unittest.TestCase):
    def test_trivial_response(self):
        self.assertEqual(classify("2+2?", roster=local_roster(), adapter_factory=factory("TRIVIAL")), TRIVIAL)

    def test_frontier_response(self):
        self.assertEqual(
            classify("design a system", roster=local_roster(), adapter_factory=factory("FRONTIER")),
            FRONTIER,
        )

    def test_adapter_error_fails_safe_to_frontier(self):
        out = classify("x", roster=local_roster(), adapter_factory=factory(exc=AdapterError("boom")))
        self.assertEqual(out, FRONTIER)

    def test_no_local_entry_fails_safe_to_frontier(self):
        # Empty roster → select_local raises → caught → frontier (never blocks routing).
        self.assertEqual(classify("x", roster=Roster([]), adapter_factory=factory("TRIVIAL")), FRONTIER)

    def test_builds_prompt_with_request_and_passes_budget(self):
        fac = factory("TRIVIAL")
        classify("REFACTOR THIS", roster=local_roster(), adapter_factory=fac, max_tokens=777)
        self.assertIn("REFACTOR THIS", fac.calls["prompt"])
        self.assertIn("TRIVIAL or FRONTIER", fac.calls["prompt"])
        self.assertEqual(fac.calls["opts"], {"max_tokens": 777})


class BuildPromptTest(unittest.TestCase):
    def test_includes_instructions_and_request(self):
        p = _build_prompt("hello world")
        self.assertIn("hello world", p)
        self.assertIn("TRIVIAL", p)
        self.assertIn("FRONTIER", p)


if __name__ == "__main__":
    unittest.main()
