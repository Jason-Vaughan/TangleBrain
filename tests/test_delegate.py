"""Tests for the local-delegation logic (tanglebrain/delegate.py).

These never touch the network or the `mcp` SDK — the adapter and roster loader are mocked, so
they verify the roster → select_local → build → run path and error propagation in isolation.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import json

from tanglebrain.adapters import AdapterError
from tanglebrain.delegate import (
    DEFAULT_DELEGATE_MAX_TOKENS,
    DELEGATE_SERVER_NAME,
    ROSTER_ENV_VAR,
    delegate_mcp_config_json,
    delegate_substitutions,
    run_local_delegate,
)
from tanglebrain.selector import SelectionError


class DelegateMcpConfigTest(unittest.TestCase):
    def test_config_json_is_valid_and_names_server(self):
        cfg = json.loads(delegate_mcp_config_json())
        self.assertIn(DELEGATE_SERVER_NAME, cfg["mcpServers"])
        server = cfg["mcpServers"][DELEGATE_SERVER_NAME]
        # Launches via `python -m tanglebrain.mcp_server` so it resolves without PATH assumptions.
        self.assertEqual(server["args"], ["-m", "tanglebrain.mcp_server"])
        self.assertTrue(server["command"])

    def test_substitutions_cover_both_tokens(self):
        subs = delegate_substitutions()
        self.assertIn("{delegate_mcp_json}", subs)
        self.assertIn("{delegate_mcp_command}", subs)
        # The command token is the interpreter path (non-empty).
        self.assertTrue(subs["{delegate_mcp_command}"])


class RunLocalDelegateTest(unittest.TestCase):
    def test_returns_adapter_text(self):
        adapter = MagicMock()
        adapter.run.return_value = "grunt result"
        with patch("tanglebrain.delegate.load_roster"), patch(
            "tanglebrain.delegate.select_local"
        ), patch("tanglebrain.delegate.build_adapter", return_value=adapter):
            self.assertEqual(run_local_delegate("do the grunt"), "grunt result")
        adapter.run.assert_called_once()
        self.assertEqual(adapter.run.call_args.args[0], "do the grunt")

    def test_wires_roster_into_select_into_build(self):
        # The three reused functions must be chained: load_roster -> select_local(roster)
        # -> build_adapter(entry). Use sentinels so a mis-wire (e.g. passing roster to
        # build_adapter, or dropping select_local) is caught.
        roster_sentinel = object()
        entry_sentinel = object()
        adapter = MagicMock()
        adapter.run.return_value = "x"
        with patch("tanglebrain.delegate.load_roster", return_value=roster_sentinel), patch(
            "tanglebrain.delegate.select_local", return_value=entry_sentinel
        ) as select, patch(
            "tanglebrain.delegate.build_adapter", return_value=adapter
        ) as build:
            run_local_delegate("q")
        self.assertIs(select.call_args.args[0], roster_sentinel)
        self.assertIs(build.call_args.args[0], entry_sentinel)

    def test_roster_error_propagates(self):
        from tanglebrain.roster import RosterError

        with patch("tanglebrain.delegate.load_roster", side_effect=RosterError("bad roster")):
            with self.assertRaises(RosterError):
                run_local_delegate("q")

    def test_default_max_tokens_is_2048(self):
        adapter = MagicMock()
        adapter.run.return_value = "x"
        with patch("tanglebrain.delegate.load_roster"), patch(
            "tanglebrain.delegate.select_local"
        ), patch("tanglebrain.delegate.build_adapter", return_value=adapter):
            run_local_delegate("q")
        self.assertEqual(adapter.run.call_args.args[1], {"max_tokens": DEFAULT_DELEGATE_MAX_TOKENS})
        self.assertEqual(DEFAULT_DELEGATE_MAX_TOKENS, 2048)

    def test_max_tokens_override_threaded_to_adapter(self):
        adapter = MagicMock()
        adapter.run.return_value = "x"
        with patch("tanglebrain.delegate.load_roster"), patch(
            "tanglebrain.delegate.select_local"
        ), patch("tanglebrain.delegate.build_adapter", return_value=adapter):
            run_local_delegate("q", max_tokens=512)
        self.assertEqual(adapter.run.call_args.args[1], {"max_tokens": 512})

    def test_explicit_roster_path_wins_over_env(self):
        adapter = MagicMock()
        adapter.run.return_value = "x"
        with patch.dict(os.environ, {ROSTER_ENV_VAR: "/from/env.yaml"}, clear=False):
            with patch("tanglebrain.delegate.load_roster") as load, patch(
                "tanglebrain.delegate.select_local"
            ), patch("tanglebrain.delegate.build_adapter", return_value=adapter):
                run_local_delegate("q", roster_path="/explicit.yaml")
        self.assertEqual(load.call_args.args[0], "/explicit.yaml")

    def test_no_explicit_path_delegates_resolution_to_load_roster(self):
        # Env/XDG resolution now lives in tanglebrain.roster.default_roster_path (covered by
        # test_roster.RosterDiscoveryTest); the delegate just passes the path through (None here).
        adapter = MagicMock()
        adapter.run.return_value = "x"
        with patch.dict(os.environ, {ROSTER_ENV_VAR: "/from/env.yaml"}, clear=False):
            with patch("tanglebrain.delegate.load_roster") as load, patch(
                "tanglebrain.delegate.select_local"
            ), patch("tanglebrain.delegate.build_adapter", return_value=adapter):
                run_local_delegate("q")
        self.assertIsNone(load.call_args.args[0])

    def test_selection_error_propagates(self):
        with patch("tanglebrain.delegate.load_roster"), patch(
            "tanglebrain.delegate.select_local", side_effect=SelectionError("no local")
        ):
            with self.assertRaises(SelectionError):
                run_local_delegate("q")

    def test_adapter_error_propagates(self):
        adapter = MagicMock()
        adapter.run.side_effect = AdapterError("endpoint down")
        with patch("tanglebrain.delegate.load_roster"), patch(
            "tanglebrain.delegate.select_local"
        ), patch("tanglebrain.delegate.build_adapter", return_value=adapter):
            with self.assertRaises(AdapterError):
                run_local_delegate("q")


if __name__ == "__main__":
    unittest.main()
