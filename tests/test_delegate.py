"""Tests for the local-delegation logic (tanglebrain/delegate.py).

These never touch the network or the `mcp` SDK — the adapter and roster loader are mocked, so
they verify the roster → select_local → build → run path and error propagation in isolation.
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import json

from tanglebrain.adapters import AdapterError
from tanglebrain.delegate import (
    DEFAULT_DELEGATE_MAX_TOKENS,
    DELEGATE_SERVER_NAME,
    ROSTER_ENV_VAR,
    _render_target_menu,
    delegate_mcp_config_json,
    delegate_substitutions,
    delegate_targets,
    run_delegate,
    run_local_delegate,
)
from tanglebrain.selector import SelectionError


def _entry(entry_id, tier="sub", good_at=None, cost=None, kind="openai-compat", can_delegate=True):
    """Build a minimal roster-entry stand-in for the delegate-target tests."""
    return SimpleNamespace(
        id=entry_id,
        tier=tier,
        good_at=good_at or [],
        cost=cost,
        can_delegate=can_delegate,
        invoke=SimpleNamespace(kind=kind),
    )


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


class RunDelegateTargetTest(unittest.TestCase):
    """The generalized delegate: ``run_delegate(target=...)`` resolution, opt-in, and no-recursion."""

    def test_target_none_uses_local_as_a_leaf(self):
        # target=None must still route to the local tier AND build it as a leaf (inject_delegate
        # False) — a delegate target never gets its own delegate tool, so no recursive delegation.
        adapter = MagicMock()
        adapter.run.return_value = "x"
        with patch("tanglebrain.delegate.load_roster"), patch(
            "tanglebrain.delegate.select_local", return_value=object()
        ) as sel, patch(
            "tanglebrain.delegate.build_adapter", return_value=adapter
        ) as build:
            run_delegate("q", target=None)
        sel.assert_called_once()
        self.assertEqual(build.call_args.kwargs.get("inject_delegate"), False)

    def test_target_resolves_and_builds_leaf_adapter(self):
        entry = _entry("cheap", can_delegate=True)
        roster = MagicMock()
        roster.by_id.return_value = entry
        adapter = MagicMock()
        adapter.run.return_value = "sub result"
        with patch("tanglebrain.delegate.load_roster", return_value=roster), patch(
            "tanglebrain.delegate.build_adapter", return_value=adapter
        ) as build:
            out = run_delegate("do it", target="cheap")
        self.assertEqual(out, "sub result")
        roster.by_id.assert_called_once_with("cheap")
        self.assertIs(build.call_args.args[0], entry)
        self.assertEqual(build.call_args.kwargs.get("inject_delegate"), False)
        adapter.run.assert_called_once_with("do it", {"max_tokens": DEFAULT_DELEGATE_MAX_TOKENS})

    def test_unknown_target_raises_selection_error(self):
        roster = MagicMock()
        roster.by_id.side_effect = KeyError("nope")
        roster.delegate_targets.return_value = []
        with patch("tanglebrain.delegate.load_roster", return_value=roster):
            with self.assertRaises(SelectionError) as ctx:
                run_delegate("q", target="ghost")
        self.assertIn("ghost", str(ctx.exception))

    def test_non_delegate_target_is_refused(self):
        # An entry that exists but isn't flagged can_delegate must NOT be invokable by name — this
        # stops an orchestrator from delegating to an arbitrary entry (e.g. another orchestrator).
        entry = _entry("orch", can_delegate=False)
        roster = MagicMock()
        roster.by_id.return_value = entry
        roster.delegate_targets.return_value = []
        with patch("tanglebrain.delegate.load_roster", return_value=roster):
            with self.assertRaises(SelectionError) as ctx:
                run_delegate("q", target="orch")
        self.assertIn("can_delegate", str(ctx.exception))

    def test_adapter_error_on_target_propagates(self):
        # An api target with billing off makes build_adapter raise AdapterError (the gate lives in
        # build_adapter, covered in test_selector); run_delegate must surface it, never swallow it.
        entry = _entry("paid", tier="api", kind="api", can_delegate=True)
        roster = MagicMock()
        roster.by_id.return_value = entry
        with patch("tanglebrain.delegate.load_roster", return_value=roster), patch(
            "tanglebrain.delegate.build_adapter", side_effect=AdapterError("billing disabled")
        ):
            with self.assertRaises(AdapterError):
                run_delegate("q", target="paid")


class DelegateTargetsMenuTest(unittest.TestCase):
    """The delegate-target menu: structured listing + human-readable rendering, secret-safe."""

    def test_delegate_targets_shape_and_secret_safety(self):
        entries = [
            _entry("local-ollama", tier="local", good_at=["grunt"], cost="free"),
            _entry("cheap", tier="sub", good_at=["code", "summarization"], cost="cheap"),
        ]
        roster = MagicMock()
        roster.delegate_targets.return_value = entries
        with patch("tanglebrain.delegate.load_roster", return_value=roster):
            menu = delegate_targets()
        self.assertEqual(
            menu,
            [
                {"id": "local-ollama", "tier": "local", "good_at": ["grunt"], "cost": "free",
                 "kind": "openai-compat"},
                {"id": "cheap", "tier": "sub", "good_at": ["code", "summarization"], "cost": "cheap",
                 "kind": "openai-compat"},
            ],
        )
        for item in menu:  # never leak a credential reference into the menu
            self.assertNotIn("key_ref", item)

    def test_render_menu_lists_targets(self):
        menu = [{"id": "cheap", "tier": "sub", "good_at": ["code"], "cost": "cheap",
                 "kind": "openai-compat"}]
        rendered = _render_target_menu(menu)
        self.assertIn("cheap", rendered)
        self.assertIn("code", rendered)

    def test_render_menu_empty_has_note(self):
        rendered = _render_target_menu([])
        self.assertIn("no additional delegate targets", rendered)


if __name__ == "__main__":
    unittest.main()
