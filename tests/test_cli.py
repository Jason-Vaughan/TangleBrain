"""Tests for the CLI end-to-end wiring (tanglebrain/cli.py).

The adapter is mocked, so these tests verify the roster → select → build → run → print path
without touching the network.
"""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from tanglebrain.adapters import AdapterError
from tanglebrain.cli import main, run_once
from tanglebrain.selector import SelectionError


class RunOnceTest(unittest.TestCase):
    def test_default_routes_through_router(self):
        # C3b flipped the default: no flags -> frontier-first Router (not local-first).
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
            self.assertEqual(run_once("hello", local=True), "local reply")
        selected = build.call_args.args[0]
        self.assertEqual(selected.tier, "local")
        self.assertEqual(selected.id, "gpt-oss-120b")

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
            self.assertEqual(run_once("hello", model="claude"), "claude reply")
        selected = build.call_args.args[0]
        self.assertEqual(selected.id, "claude")
        self.assertEqual(selected.tier, "sub")

    def test_unknown_model_raises_selection_error(self):
        with self.assertRaises(SelectionError):
            run_once("hello", model="no-such-model")

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


if __name__ == "__main__":
    unittest.main()
