"""Tests for the CLI adapter (tanglebrain/adapters/cli.py).

All subprocess execution is mocked — these tests never spawn a real CLI. The env-scrub safety
boundary is exercised hardest: it is the rule that keeps ``claude -p`` on its own authenticated
session rather than an injected API key.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from tanglebrain.adapters.base import AdapterError
from tanglebrain.adapters.cli import (
    CliAdapter,
    build_argv,
    scrubbed_env,
)
from tanglebrain.roster import Invoke, RosterEntry

# Real captured stdout shapes (probed from the installed CLIs on 2026-06-16), trimmed.
CLAUDE_JSON_OK = '{"type":"result","subtype":"success","is_error":false,"result":"PONG"}'
CLAUDE_JSON_ERR = '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"boom"}'
GEMINI_JSON_OK = '{"session_id":"abc","response":"PONG","stats":{"models":{}}}'


def completed(returncode=0, stdout="", stderr=""):
    """Build a CompletedProcess stand-in for subprocess.run's return value."""
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


def patch_run(**kwargs):
    """Patch subprocess.run in the cli adapter module; returns the patcher's MagicMock."""
    return patch("tanglebrain.adapters.cli.subprocess.run", **kwargs)


class ScrubbedEnvTest(unittest.TestCase):
    """The env-scrub safety boundary — the most important behaviour in this module."""

    def test_removes_named_var(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-paid-108", "KEEP": "1"}, clear=True):
            env = scrubbed_env(["ANTHROPIC_API_KEY"])
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_passes_other_vars_through(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk", "PATH": "/usr/bin"}, clear=True):
            env = scrubbed_env(["ANTHROPIC_API_KEY"])
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_does_not_mutate_real_environ(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-paid-108"}, clear=True):
            scrubbed_env(["ANTHROPIC_API_KEY"])
            # The parent process must still have the key — we scrub a *copy*, never os.environ.
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-paid-108")

    def test_scrubs_multiple_vars(self):
        with patch.dict(os.environ, {"A": "1", "B": "2", "C": "3"}, clear=True):
            env = scrubbed_env(["A", "C"])
        self.assertEqual(set(env), {"B"})

    def test_missing_name_is_ignored(self):
        with patch.dict(os.environ, {"KEEP": "1"}, clear=True):
            env = scrubbed_env(["NOT_SET"])
        self.assertEqual(env, {"KEEP": "1"})

    def test_empty_scrub_list_is_identity(self):
        with patch.dict(os.environ, {"A": "1"}, clear=True):
            self.assertEqual(scrubbed_env([]), {"A": "1"})

    def test_run_hands_scrubbed_env_to_subprocess(self):
        # End-to-end: the env actually passed to subprocess.run must omit the scrubbed key.
        adapter = CliAdapter(cmd=["claude", "-p"], parse="claude-json", scrub_env=["ANTHROPIC_API_KEY"])
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-paid-108", "PATH": "/b"}, clear=True):
            with patch_run(return_value=completed(0, CLAUDE_JSON_OK)) as run:
                adapter.run("hi")
        passed_env = run.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", passed_env)
        self.assertIn("PATH", passed_env)


class BuildArgvTest(unittest.TestCase):
    """Prompt injection: {prompt} substitution, else append; never via the shell."""

    def test_appends_when_no_token(self):
        self.assertEqual(build_argv(["claude", "-p"], "hello"), ["claude", "-p", "hello"])

    def test_substitutes_token(self):
        self.assertEqual(
            build_argv(["gemini", "-p", "{prompt}", "--json"], "hello"),
            ["gemini", "-p", "hello", "--json"],
        )

    def test_token_inside_larger_arg(self):
        self.assertEqual(build_argv(["t", "--q={prompt}"], "hi"), ["t", "--q=hi"])

    def test_prompt_with_special_chars_is_literal(self):
        # No shell => metacharacters are passed verbatim as one argv element, not interpreted.
        argv = build_argv(["codex", "exec"], "rm -rf / ; echo $HOME `whoami`")
        self.assertEqual(argv[-1], "rm -rf / ; echo $HOME `whoami`")


class ParserTest(unittest.TestCase):
    """Each output parser extracts the right final text and rejects bad shapes."""

    def _run(self, cmd, parse, stdout):
        adapter = CliAdapter(cmd=cmd, parse=parse)
        with patch_run(return_value=completed(0, stdout)):
            return adapter.run("q")

    def test_claude_json_returns_result(self):
        self.assertEqual(self._run(["claude"], "claude-json", CLAUDE_JSON_OK), "PONG")

    def test_claude_json_error_flag_raises(self):
        with self.assertRaises(AdapterError) as ctx:
            self._run(["claude"], "claude-json", CLAUDE_JSON_ERR)
        self.assertIn("error_max_turns", str(ctx.exception))

    def test_gemini_json_returns_response(self):
        self.assertEqual(self._run(["gemini"], "gemini-json", GEMINI_JSON_OK), "PONG")

    def test_plain_strips_stdout(self):
        self.assertEqual(self._run(["codex", "exec"], "plain", "  PONG\n"), "PONG")

    def test_plain_empty_raises(self):
        with self.assertRaises(AdapterError):
            self._run(["codex"], "plain", "   \n")

    def test_json_malformed_raises(self):
        with self.assertRaises(AdapterError):
            self._run(["claude"], "claude-json", "not json")

    def test_json_not_object_raises(self):
        with self.assertRaises(AdapterError):
            self._run(["gemini"], "gemini-json", "[1, 2, 3]")

    def test_json_missing_field_raises(self):
        with self.assertRaises(AdapterError):
            self._run(["gemini"], "gemini-json", '{"session_id": "x"}')

    def test_json_field_not_string_raises(self):
        with self.assertRaises(AdapterError):
            self._run(["gemini"], "gemini-json", '{"response": 42}')

    def test_json_field_empty_string_raises(self):
        with self.assertRaises(AdapterError):
            self._run(["gemini"], "gemini-json", '{"response": "   "}')


class RunTest(unittest.TestCase):
    """run() builds argv/stdin correctly and maps process failures to AdapterError."""

    def _adapter(self, **kw):
        kw.setdefault("cmd", ["codex", "exec"])
        kw.setdefault("parse", "plain")
        return CliAdapter(**kw)

    def test_prompt_goes_to_argv_not_stdin(self):
        with patch_run(return_value=completed(0, "ok")) as run:
            self._adapter().run("the prompt")
        self.assertEqual(run.call_args.args[0], ["codex", "exec", "the prompt"])
        # stdin is closed with EOF (input=""), so the prompt is never sent over stdin.
        self.assertEqual(run.call_args.kwargs["input"], "")

    def test_no_shell_used(self):
        with patch_run(return_value=completed(0, "ok")) as run:
            self._adapter().run("q")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_nonzero_exit_raises_with_stderr(self):
        with patch_run(return_value=completed(2, "", "kaboom")):
            with self.assertRaises(AdapterError) as ctx:
                self._adapter().run("q")
        self.assertIn("kaboom", str(ctx.exception))
        self.assertIn("2", str(ctx.exception))

    def test_timeout_raises(self):
        with patch_run(side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1.0)):
            with self.assertRaises(AdapterError) as ctx:
                self._adapter(timeout=1.0).run("q")
        self.assertIn("timed out", str(ctx.exception))

    def test_missing_binary_raises(self):
        with patch_run(side_effect=FileNotFoundError("no codex")):
            with self.assertRaises(AdapterError) as ctx:
                self._adapter().run("q")
        self.assertIn("not found", str(ctx.exception))

    def test_timeout_opt_overrides(self):
        with patch_run(return_value=completed(0, "ok")) as run:
            self._adapter(timeout=300.0).run("q", {"timeout": 5})
        self.assertEqual(run.call_args.kwargs["timeout"], 5.0)

    def test_unknown_opts_ignored(self):
        with patch_run(return_value=completed(0, "ok")):
            self.assertEqual(self._adapter().run("q", {"max_tokens": 999}), "ok")


class DelegateInjectionTest(unittest.TestCase):
    """When inject_delegate is set, delegate_args (substituted) are appended to the command."""

    CLAUDE_DELEGATE = [
        "--mcp-config",
        "{delegate_mcp_json}",
        "--allowedTools",
        "mcp__tanglebrain-delegate__delegate_local",
        "--strict-mcp-config",
    ]

    def test_not_injected_when_flag_false(self):
        adapter = CliAdapter(
            cmd=["claude", "-p"], parse="claude-json", delegate_args=self.CLAUDE_DELEGATE, inject_delegate=False
        )
        with patch_run(return_value=completed(0, CLAUDE_JSON_OK)) as run:
            adapter.run("hi")
        self.assertEqual(run.call_args.args[0], ["claude", "-p", "hi"])

    def test_injected_appends_substituted_args(self):
        adapter = CliAdapter(
            cmd=["claude", "-p"], parse="claude-json", delegate_args=self.CLAUDE_DELEGATE, inject_delegate=True
        )
        with patch_run(return_value=completed(0, CLAUDE_JSON_OK)) as run:
            adapter.run("hi")
        argv = run.call_args.args[0]
        # delegate flags land after the base cmd and before the prompt (the final positional).
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertEqual(argv[-1], "hi")
        self.assertIn("--mcp-config", argv)
        self.assertIn("mcp__tanglebrain-delegate__delegate_local", argv)
        # The {delegate_mcp_json} token must be substituted with real JSON naming the server.
        cfg = argv[argv.index("--mcp-config") + 1]
        self.assertNotIn("{delegate_mcp_json}", cfg)
        self.assertIn("tanglebrain-delegate", cfg)
        self.assertIn("tanglebrain.mcp_server", cfg)

    def test_command_token_substituted_for_codex_style(self):
        adapter = CliAdapter(
            cmd=["codex", "exec"],
            parse="plain",
            delegate_args=["-c", "mcp_servers.x.command={delegate_mcp_command}"],
            inject_delegate=True,
        )
        with patch_run(return_value=completed(0, "ok")) as run:
            adapter.run("hi")
        argv = run.call_args.args[0]
        override = argv[argv.index("-c") + 1]
        self.assertNotIn("{delegate_mcp_command}", override)
        self.assertTrue(override.startswith("mcp_servers.x.command="))

    def test_empty_delegate_args_is_noop_even_when_injecting(self):
        adapter = CliAdapter(cmd=["codex", "exec"], parse="plain", delegate_args=[], inject_delegate=True)
        with patch_run(return_value=completed(0, "ok")) as run:
            adapter.run("hi")
        self.assertEqual(run.call_args.args[0], ["codex", "exec", "hi"])

    def test_prompt_token_cmd_with_injection(self):
        # gemini-shaped cmd: {prompt} is substituted in place AND delegate flags are appended,
        # so the prompt must NOT also be appended at the end.
        adapter = CliAdapter(
            cmd=["gemini", "-p", "{prompt}", "--output-format", "json"],
            parse="gemini-json",
            delegate_args=["--allowed-mcp-server-names", "tanglebrain-delegate", "--approval-mode", "yolo"],
            inject_delegate=True,
        )
        with patch_run(return_value=completed(0, GEMINI_JSON_OK)) as run:
            adapter.run("do the task")
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-p") + 1], "do the task")  # prompt at the -p slot
        self.assertNotEqual(argv[-1], "do the task")  # not also appended at the end
        self.assertIn("--allowed-mcp-server-names", argv)
        self.assertIn("--approval-mode", argv)

    def test_from_entry_carries_delegate_args_and_flag(self):
        entry = RosterEntry(
            id="claude",
            tier="sub",
            invoke=Invoke(kind="cli", cmd=["claude", "-p"], parse="claude-json", delegate_args=self.CLAUDE_DELEGATE),
        )
        adapter = CliAdapter.from_entry(entry, inject_delegate=True)
        self.assertEqual(adapter.delegate_args, self.CLAUDE_DELEGATE)
        self.assertTrue(adapter.inject_delegate)


class ConstructionTest(unittest.TestCase):
    """Constructor and from_entry validation."""

    def test_empty_cmd_rejected(self):
        with self.assertRaises(AdapterError):
            CliAdapter(cmd=[])

    def test_unknown_parser_rejected(self):
        with self.assertRaises(AdapterError) as ctx:
            CliAdapter(cmd=["x"], parse="telepathy")
        self.assertIn("telepathy", str(ctx.exception))

    def test_default_parser_is_plain(self):
        self.assertEqual(CliAdapter(cmd=["x"]).parser_name, "plain")

    def test_from_entry_builds_each_sub(self):
        for entry_id, cmd, parse in (
            ("claude", ["claude", "-p", "--output-format", "json"], "claude-json"),
            ("codex", ["codex", "exec"], "plain"),
            ("gemini", ["gemini", "-p", "{prompt}", "--output-format", "json"], "gemini-json"),
        ):
            entry = RosterEntry(
                id=entry_id, tier="sub", invoke=Invoke(kind="cli", cmd=cmd, parse=parse)
            )
            adapter = CliAdapter.from_entry(entry)
            self.assertEqual(adapter.cmd, cmd)
            self.assertEqual(adapter.parser_name, parse)

    def test_from_entry_carries_scrub_env(self):
        entry = RosterEntry(
            id="claude",
            tier="sub",
            invoke=Invoke(kind="cli", cmd=["claude"], parse="plain", scrub_env=["ANTHROPIC_API_KEY"]),
        )
        self.assertEqual(CliAdapter.from_entry(entry).scrub_env, ["ANTHROPIC_API_KEY"])

    def test_from_entry_rejects_non_cli_kind(self):
        entry = RosterEntry(
            id="x", tier="local", invoke=Invoke(kind="openai-compat", base_url="u", model="m")
        )
        with self.assertRaises(AdapterError):
            CliAdapter.from_entry(entry)

    def test_from_entry_rejects_missing_cmd(self):
        # The roster loader normally guards this; from_entry guards it too (defence in depth).
        entry = RosterEntry(id="x", tier="sub", invoke=Invoke(kind="cli", cmd=None))
        with self.assertRaises(AdapterError):
            CliAdapter.from_entry(entry)


if __name__ == "__main__":
    unittest.main()
