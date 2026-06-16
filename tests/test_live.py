"""Opt-in live end-to-end test against the real local LiteLLM endpoint.

Skipped by default — it needs network access to local over the tailnet and the scoped key at
``~/.config/tanglebrain/tanglebrain-spike.key``. Run it explicitly::

    make test-live
    # or
    TANGLEBRAIN_LIVE=1 python -m unittest tests.test_live -v

C1 "definition of done": one request routes roster → local → gpt-oss → text.
C2 adds the subscription-CLI checks (each sub returns text) and the env-scrub safety proof
(claude runs without ANTHROPIC_API_KEY). The CLI checks each additionally require their tool to
be installed and logged in, so they skip individually when the binary is absent.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tanglebrain.adapters.cli import CliAdapter, scrubbed_env
from tanglebrain.cli import run_once
from tanglebrain.delegate import run_local_delegate
from tanglebrain.roster import load_roster
from tanglebrain.router import Router

LIVE = os.environ.get("TANGLEBRAIN_LIVE") == "1"


@unittest.skipUnless(LIVE, "set TANGLEBRAIN_LIVE=1 to run the live endpoint test")
class LiveEndToEndTest(unittest.TestCase):
    def test_one_request_routes_to_local_and_returns_text(self):
        text = run_once("Reply with exactly the word: pong", max_tokens=2048)
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip(), "expected non-empty text from gpt-oss-120b")


@unittest.skipUnless(LIVE, "set TANGLEBRAIN_LIVE=1 to run the live delegate test")
class LiveDelegateTest(unittest.TestCase):
    """C2b: the MCP delegate's routing logic offloads to real gpt-oss and returns text."""

    def test_delegate_offloads_to_local_and_returns_text(self):
        text = run_local_delegate("Reply with exactly the word: pong")
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip(), "expected non-empty text from the local delegate")


@unittest.skipUnless(LIVE, "set TANGLEBRAIN_LIVE=1 to run the live orchestrated-delegation test")
class LiveDelegateInjectionTest(unittest.TestCase):
    """C3b: an orchestrator invoked with the delegate injected actually calls it (end-to-end).

    Routes through claude (the proven primary orchestrator) with delegation enabled, asks it to use
    the delegate, and checks the local model's answer comes back — the full frontier-first
    decompose→delegate→review loop. ANTHROPIC_API_KEY stays scrubbed (claude's roster scrub_env).
    """

    def test_claude_orchestrator_calls_the_delegate(self):
        if shutil.which("claude") is None:
            self.skipTest("claude CLI not installed/logged in")
        from tanglebrain.adapters.cli import CliAdapter

        adapter = CliAdapter.from_entry(load_roster().by_id("claude"), inject_delegate=True)
        answer = adapter.run(
            "Use the delegate_local tool to have the local model reply with exactly PONG, then "
            "report what it returned. Do not say PONG unless the tool returned it."
        )
        self.assertIn("PONG", answer.upper(), f"expected the delegated reply in: {answer!r}")


@unittest.skipUnless(LIVE, "set TANGLEBRAIN_LIVE=1 to run the live CLI tests")
class LiveCliTest(unittest.TestCase):
    """Each subscription CLI returns text through the roster → cli adapter path."""

    def _route(self, model: str):
        if shutil.which(model) is None:
            self.skipTest(f"{model} CLI not installed/logged in")
        text = run_once("Reply with exactly: PONG", model=model)
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip(), f"expected non-empty text from {model}")
        return text

    def test_claude_returns_text(self):
        self._route("claude")

    def test_codex_returns_text(self):
        self._route("codex")

    def test_gemini_returns_text(self):
        self._route("gemini")


@unittest.skipUnless(LIVE, "set TANGLEBRAIN_LIVE=1 to run the live router test")
class LiveRouterTest(unittest.TestCase):
    """C3: the frontier-first router selects an orchestrator and returns its text."""

    def test_router_routes_to_an_orchestrator(self):
        # Uses the real subs; rotation state goes to a temp dir so the test is self-contained.
        with patch.dict(os.environ, {"TANGLEBRAIN_STATE_DIR": tempfile.mkdtemp()}, clear=False):
            text = Router(load_roster()).route("Reply with exactly: PONG")
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip(), "expected non-empty text from an orchestrator")


@unittest.skipUnless(LIVE, "set TANGLEBRAIN_LIVE=1 to run the env-scrub safety proof")
class LiveEnvScrubTest(unittest.TestCase):
    """Safety-critical (§7): claude must run with ANTHROPIC_API_KEY scrubbed from its env."""

    def test_claude_subprocess_sees_no_anthropic_key(self):
        if shutil.which("claude") is None:
            self.skipTest("claude CLI not installed/logged in")
        # Ask claude to report what it sees; with the key scrubbed it should see it as absent.
        adapter = CliAdapter(
            cmd=["claude", "-p", "--output-format", "json"],
            parse="claude-json",
            scrub_env=["ANTHROPIC_API_KEY"],
        )
        answer = adapter.run(
            "Run the shell command `printenv ANTHROPIC_API_KEY` and reply with exactly UNSET "
            "if it prints nothing or errors, otherwise reply SET."
        )
        self.assertIn("UNSET", answer.upper(), f"claude saw the paid API key: {answer!r}")

    def test_scrubbed_env_proof_without_invoking_claude(self):
        # A deterministic companion to the live check above: the env handed to the subprocess
        # omits the key even when the parent process has it set. patch.dict restores the real
        # environment afterwards, so this never pollutes the process for later tests.
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-fake-for-test"}, clear=False):
            env = scrubbed_env(["ANTHROPIC_API_KEY"])
        self.assertNotIn("ANTHROPIC_API_KEY", env)


if __name__ == "__main__":
    unittest.main()
