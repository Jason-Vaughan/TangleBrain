"""Tests for the CLI end-to-end wiring (tanglebrain/cli.py).

The adapter is mocked, so these tests verify the roster → select → build → run → print path
without touching the network.
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from tanglebrain.adapters import AdapterError
from tanglebrain.cli import main, run_once
from tanglebrain.measurement import read_records
from tanglebrain.roster import packaged_roster_path
from tanglebrain.selector import SelectionError


# A self-contained roster (a local entry + a 'claude' sub) for tests that pin --model/--local to a
# known id. Pinned via roster_path so these tests never depend on the ambient
# ~/.config/tanglebrain/roster.yaml: the packaged example ships local-only since the sub entries
# became opt-in/commented (R2a), so an unpinned model="claude" only resolves on a machine that
# happens to have a real roster — which is exactly the non-hermetic gap CI caught.
_PINNED_ROSTER_YAML = """\
- id: local-ollama
  tier: local
  invoke: {kind: openai-compat, base_url: "http://localhost:11434/v1", model: "llama3.2"}
  cost: free
  good_at: [grunt]
- id: claude
  tier: sub
  invoke: {kind: cli, cmd: ["claude"], parse: claude-json}
  cost: subscription
  good_at: [reasoning]
  can_orchestrate: true
"""


def _pinned_roster(test: unittest.TestCase) -> str:
    """Write the pinned local+claude roster to a temp file and return its path (auto-cleaned)."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(_PINNED_ROSTER_YAML)
    handle.close()
    test.addCleanup(os.unlink, handle.name)
    return handle.name


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        # Redirect the usage log into a temp dir so metering side-effects stay hermetic
        # (run_once now records each routed task) and never touch the real ~/.cache.
        self.tmp = tempfile.mkdtemp()
        self._env = patch.dict(os.environ, {"TANGLEBRAIN_STATE_DIR": self.tmp}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _records(self):
        return read_records(Path(self.tmp) / "usage.jsonl")

    def test_default_routes_through_router(self):
        # The default (no flags) routes through the frontier-first Router (not local-first).
        fake_router = MagicMock()
        fake_router.route.return_value = "routed reply"
        with patch("tanglebrain.cli.load_roster"), patch(
            "tanglebrain.cli.Router", return_value=fake_router
        ):
            self.assertEqual(run_once("hello"), "routed reply")
        fake_router.route.assert_called_once()

    def test_local_flag_forces_local_tier(self):
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "local reply"
        with patch("tanglebrain.cli.build_adapter", return_value=fake_adapter) as build:
            # Pin to the packaged example so the asserted local id is machine-independent.
            self.assertEqual(
                run_once("hello", local=True, roster_path=str(packaged_roster_path())), "local reply"
            )
        selected = build.call_args.args[0]
        self.assertEqual(selected.tier, "local")
        self.assertEqual(selected.id, "local-ollama")

    def test_max_tokens_threaded_through_local(self):
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "x"
        with patch("tanglebrain.cli.build_adapter", return_value=fake_adapter):
            run_once("hello", local=True, max_tokens=256)
        self.assertEqual(fake_adapter.run.call_args.args[1], {"max_tokens": 256})

    def test_no_max_tokens_passes_none_opts_local(self):
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "x"
        with patch("tanglebrain.cli.build_adapter", return_value=fake_adapter):
            run_once("hello", local=True)
        self.assertIsNone(fake_adapter.run.call_args.args[1])

    def test_model_routes_to_named_entry(self):
        # --model selects a specific roster entry (here, a sub) instead of local-first.
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "claude reply"
        with patch("tanglebrain.cli.build_adapter", return_value=fake_adapter) as build:
            self.assertEqual(
                run_once("hello", model="claude", roster_path=_pinned_roster(self)), "claude reply"
            )
        selected = build.call_args.args[0]
        self.assertEqual(selected.id, "claude")
        self.assertEqual(selected.tier, "sub")

    def test_unknown_model_raises_selection_error(self):
        with self.assertRaises(SelectionError):
            run_once("hello", model="no-such-model")

    def test_model_to_paid_api_entry_is_inert_with_gate_off(self):
        # The boundary the user actually touches: `--model <api-id>` must NOT reach a paid endpoint
        # while billing is gated off. This exercises the REAL build_adapter -> load_settings()
        # default-load (packaged settings.yaml ships the gate off), no mocks on the gate.
        roster_yaml = (
            "- id: gpt5\n  tier: api\n"
            "  invoke: {kind: api, base_url: 'http://x/v1', model: gpt-5, key_ref: 'env:NOPE'}\n"
        )
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        handle.write(roster_yaml)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(AdapterError) as ctx:
            run_once("expensive please", roster_path=handle.name, model="gpt5")
        self.assertIn("billing is disabled", str(ctx.exception))
        # And it must not have metered a task (it never ran).
        self.assertEqual(self._records(), [])

    def test_router_threads_task_hint(self):
        # The default router path passes the task hint through to Router.route.
        fake_router = MagicMock()
        fake_router.route.return_value = "routed by orchestrator"
        with patch("tanglebrain.cli.load_roster"), patch(
            "tanglebrain.cli.Router", return_value=fake_router
        ) as RouterCls:
            self.assertEqual(run_once("hello", task="code"), "routed by orchestrator")
        RouterCls.assert_called_once()
        self.assertEqual(fake_router.route.call_args.kwargs.get("task"), "code")

    def test_model_takes_precedence_over_router(self):
        # model is an explicit override and wins over the default router path.
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "from-model"
        with patch("tanglebrain.cli.load_roster"), patch(
            "tanglebrain.cli.select_by_id"
        ), patch("tanglebrain.cli.build_adapter", return_value=fake_adapter), patch(
            "tanglebrain.cli.Router"
        ) as RouterCls:
            self.assertEqual(run_once("hi", model="claude"), "from-model")
        RouterCls.assert_not_called()

    def test_local_takes_precedence_over_router(self):
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "from-local"
        with patch("tanglebrain.cli.load_roster"), patch(
            "tanglebrain.cli.select_local"
        ), patch("tanglebrain.cli.build_adapter", return_value=fake_adapter), patch(
            "tanglebrain.cli.Router"
        ) as RouterCls:
            self.assertEqual(run_once("hi", local=True), "from-local")
        RouterCls.assert_not_called()

    def test_local_path_records_a_task(self):
        # Metering seam: the --local path writes one usage record tagged tier=local.
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "local reply"
        with patch("tanglebrain.cli.build_adapter", return_value=fake_adapter):
            run_once("hello there", local=True)
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], "local")
        self.assertEqual(records[0]["tier"], "local")
        self.assertGreater(records[0]["spend_avoided_usd"], 0.0)

    def test_return_served_local_path(self):
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "local reply"
        with patch("tanglebrain.cli.build_adapter", return_value=fake_adapter):
            result = run_once(
                "hi", local=True, return_served=True, roster_path=str(packaged_roster_path())
            )
        text, served = result
        self.assertEqual(text, "local reply")
        self.assertEqual(served["path"], "local")
        self.assertEqual(served["tier"], "local")
        self.assertEqual(served["model"], "local-ollama")

    def test_return_served_router_path(self):
        served_entry = MagicMock()
        served_entry.tier = "sub"
        served_entry.id = "codex"
        fake_router = MagicMock()
        fake_router.route.return_value = "routed"
        fake_router.last_served = served_entry
        with patch("tanglebrain.cli.load_roster"), patch(
            "tanglebrain.cli.Router", return_value=fake_router
        ):
            text, served = run_once("hi", return_served=True)
        self.assertEqual(text, "routed")
        self.assertEqual(served, {"path": "router", "tier": "sub", "model": "codex"})

    def test_default_return_is_plain_str(self):
        # Back-compat: without return_served, run_once still returns a bare string.
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "x"
        with patch("tanglebrain.cli.build_adapter", return_value=fake_adapter):
            self.assertEqual(run_once("hi", local=True), "x")

    def test_model_path_records_a_task(self):
        # The --model override path also meters, tagged path=model with the pinned entry's tier.
        fake_adapter = MagicMock()
        fake_adapter.run.return_value = "claude reply"
        with patch("tanglebrain.cli.build_adapter", return_value=fake_adapter):
            run_once("summarize this", model="claude", roster_path=_pinned_roster(self))
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], "model")
        self.assertEqual(records[0]["tier"], "sub")
        self.assertEqual(records[0]["model"], "claude")

    def test_gate_trivial_routes_to_local(self):
        # gate on + classifier says trivial → served by free local (path 'gate-local'), no Router.
        fake_adapter = MagicMock(); fake_adapter.run.return_value = "local ans"
        with patch("tanglebrain.cli.classify", return_value="trivial") as clf, \
             patch("tanglebrain.cli.build_adapter", return_value=fake_adapter), \
             patch("tanglebrain.cli.Router") as RouterCls:
            text, served = run_once("2+2?", gate=True, return_served=True)
        self.assertEqual(text, "local ans")
        self.assertEqual(served["path"], "gate-local")
        self.assertEqual(served["tier"], "local")
        RouterCls.assert_not_called()
        clf.assert_called_once()

    def test_gate_frontier_falls_through_to_router(self):
        served_entry = MagicMock(); served_entry.tier = "sub"; served_entry.id = "claude"
        fake_router = MagicMock(); fake_router.route.return_value = "routed"; fake_router.last_served = served_entry
        with patch("tanglebrain.cli.classify", return_value="frontier"), \
             patch("tanglebrain.cli.Router", return_value=fake_router):
            text, served = run_once("design a system", gate=True, return_served=True)
        self.assertEqual(text, "routed")
        self.assertEqual(served["path"], "router")

    def test_gate_false_skips_classifier(self):
        fake_router = MagicMock(); fake_router.route.return_value = "routed"; fake_router.last_served = None
        with patch("tanglebrain.cli.classify") as clf, patch("tanglebrain.cli.Router", return_value=fake_router):
            run_once("hi", gate=False)
        clf.assert_not_called()

    def test_gate_not_consulted_for_model_or_local(self):
        # The gate lives only on the default path — an explicit --model/--local must bypass it
        # entirely (classify never called), even with gate forced on.
        fake_adapter = MagicMock(); fake_adapter.run.return_value = "x"
        roster = _pinned_roster(self)
        with patch("tanglebrain.cli.classify") as clf, \
             patch("tanglebrain.cli.build_adapter", return_value=fake_adapter):
            run_once("hi", model="claude", gate=True, roster_path=roster)
            run_once("hi", local=True, gate=True, roster_path=roster)
        clf.assert_not_called()

    def test_gate_none_uses_setting(self):
        from tanglebrain.settings import Settings
        fake_adapter = MagicMock(); fake_adapter.run.return_value = "x"
        with patch("tanglebrain.cli.load_settings", return_value=Settings(classifier_gate_enabled=True)), \
             patch("tanglebrain.cli.classify", return_value="trivial"), \
             patch("tanglebrain.cli.build_adapter", return_value=fake_adapter), \
             patch("tanglebrain.cli.Router") as RouterCls:
            _, served = run_once("2+2?", return_served=True)  # gate defaults None → reads the setting
        self.assertEqual(served["path"], "gate-local")
        RouterCls.assert_not_called()

    def test_router_path_records_served_entry(self):
        # The router surfaces last_served; run_once records that tier/model.
        served = MagicMock()
        served.tier = "sub"
        served.id = "claude"
        fake_router = MagicMock()
        fake_router.route.return_value = "routed reply"
        fake_router.last_served = served
        with patch("tanglebrain.cli.load_roster"), patch(
            "tanglebrain.cli.Router", return_value=fake_router
        ):
            run_once("hello")
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], "router")
        self.assertEqual(records[0]["tier"], "sub")
        self.assertEqual(records[0]["model"], "claude")


class MainTest(unittest.TestCase):
    def test_success_prints_and_returns_zero(self):
        with patch("tanglebrain.cli.run_once", return_value="the answer"):
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["what is 2+2?"])
        self.assertEqual(code, 0)
        self.assertIn("the answer", out.getvalue())

    def test_model_flag_threaded_to_run_once(self):
        with patch("tanglebrain.cli.run_once", return_value="ok") as run:
            with redirect_stdout(io.StringIO()):
                code = main(["--model", "gemini", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["model"], "gemini")

    def test_local_and_task_flags_threaded_to_run_once(self):
        with patch("tanglebrain.cli.run_once", return_value="ok") as run:
            with redirect_stdout(io.StringIO()):
                code = main(["--local", "--task", "code", "hello"])
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.kwargs["local"])
        self.assertEqual(run.call_args.kwargs["task"], "code")

    def test_default_no_flags_routes(self):
        # No flags: local defaults False -> run_once takes the router path.
        with patch("tanglebrain.cli.run_once", return_value="ok") as run:
            with redirect_stdout(io.StringIO()):
                code = main(["hello"])
        self.assertEqual(code, 0)
        self.assertFalse(run.call_args.kwargs["local"])
        self.assertIsNone(run.call_args.kwargs["model"])
        self.assertIsNone(run.call_args.kwargs["gate"])  # no --gate/--no-gate → defer to the setting

    def test_gate_flag_threaded(self):
        with patch("tanglebrain.cli.run_once", return_value="ok") as run:
            with redirect_stdout(io.StringIO()):
                main(["--gate", "hi"])
        self.assertIs(run.call_args.kwargs["gate"], True)

    def test_no_gate_flag_threaded(self):
        with patch("tanglebrain.cli.run_once", return_value="ok") as run:
            with redirect_stdout(io.StringIO()):
                main(["--no-gate", "hi"])
        self.assertIs(run.call_args.kwargs["gate"], False)

    def test_router_error_returns_one_and_writes_stderr(self):
        from tanglebrain.router import RouterError

        with patch("tanglebrain.cli.run_once", side_effect=RouterError("all orchestrators failed")):
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["hello"])
        self.assertEqual(code, 1)
        self.assertIn("all orchestrators failed", err.getvalue())

    def test_adapter_error_returns_one_and_writes_stderr(self):
        with patch("tanglebrain.cli.run_once", side_effect=AdapterError("endpoint down")):
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["--local", "hello"])
        self.assertEqual(code, 1)
        self.assertIn("endpoint down", err.getvalue())

    def test_stats_prints_rollup_without_prompt(self):
        summary = {"tasks": 2, "by_tier": {"local": 2}, "in_tokens_est": 10,
                   "out_tokens_est": 20, "cloud_equiv_usd": 1.0, "spend_avoided_usd": 1.0}
        with patch("tanglebrain.cli.read_records", return_value=[]), patch(
            "tanglebrain.cli.rollup", return_value=summary
        ), patch("tanglebrain.cli.run_once") as run:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--stats"])
        self.assertEqual(code, 0)
        self.assertIn("Tasks routed:", out.getvalue())
        run.assert_not_called()  # --stats short-circuits before routing

    def test_missing_prompt_without_stats_errors(self):
        # argparse parser.error exits with code 2.
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_version_flag_prints_version_and_exits_zero(self):
        # argparse's version action prints to stdout and exits 0; it short-circuits before routing.
        from tanglebrain import __version__

        out = io.StringIO()
        with patch("tanglebrain.cli.run_once") as run:
            with redirect_stdout(out):
                with self.assertRaises(SystemExit) as ctx:
                    main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(__version__, out.getvalue())
        run.assert_not_called()  # --version never routes


if __name__ == "__main__":
    unittest.main()
