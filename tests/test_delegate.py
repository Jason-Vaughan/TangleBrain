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
    DEFAULT_CONCURRENCY_FALLBACK,
    DEFAULT_DELEGATE_MAX_TOKENS,
    DELEGATE_SERVER_NAME,
    ROSTER_ENV_VAR,
    NoDelegateFit,
    _default_concurrency,
    _effective_concurrency,
    _render_target_menu,
    _select_by_capability,
    available_capabilities,
    delegate_mcp_config_json,
    delegate_substitutions,
    delegate_targets,
    run_delegate,
    run_delegate_many,
    run_local_delegate,
)
from tanglebrain.selector import SelectionError
from tanglebrain.settings import Settings


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


def _roster_of(*entries):
    """A roster stand-in whose ``delegate_targets()`` returns the given (already can_delegate) entries."""
    roster = MagicMock()
    roster.delegate_targets.return_value = list(entries)
    return roster


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
    def setUp(self):
        # run_delegate now meters via record_task; stub it so these tests never touch the real log.
        patcher = patch("tanglebrain.delegate.record_task")
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def setUp(self):
        patcher = patch("tanglebrain.delegate.record_task")
        patcher.start()
        self.addCleanup(patcher.stop)

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


class SelectByCapabilityTest(unittest.TestCase):
    """Capability-routed selection: cheapest good_at fit, api excluded, no-fit signals NoDelegateFit."""

    def test_single_match_selected(self):
        roster = _roster_of(_entry("sub-a", tier="sub", good_at=["code"]))
        self.assertEqual(_select_by_capability(roster, "code").id, "sub-a")

    def test_cheapest_tier_wins_local_over_sub(self):
        # Both fit `code`; local (rank 0) must beat sub (rank 1) regardless of declared order.
        roster = _roster_of(
            _entry("sub-a", tier="sub", good_at=["code"]),
            _entry("local-a", tier="local", good_at=["code"]),
        )
        self.assertEqual(_select_by_capability(roster, "code").id, "local-a")

    def test_declared_order_breaks_ties_within_a_tier(self):
        roster = _roster_of(
            _entry("sub-a", tier="sub", good_at=["code"]),
            _entry("sub-b", tier="sub", good_at=["code"]),
        )
        self.assertEqual(_select_by_capability(roster, "code").id, "sub-a")

    def test_api_target_never_auto_selected(self):
        # An api target is the ONLY good_at match — it must still be excluded (paid never auto-routed),
        # so this is a no-fit, not a selection.
        roster = _roster_of(_entry("paid", tier="api", kind="api", good_at=["code"]))
        with self.assertRaises(NoDelegateFit):
            _select_by_capability(roster, "code")

    def test_no_match_raises_nodelegatefit_with_available_caps(self):
        roster = _roster_of(_entry("sub-a", tier="sub", good_at=["grunt"]))
        with self.assertRaises(NoDelegateFit) as ctx:
            _select_by_capability(roster, "code")
        msg = str(ctx.exception)
        self.assertIn("code", msg)
        self.assertIn("grunt", msg)  # lists available capabilities

    def test_available_capabilities_sorted_unique_excludes_api(self):
        roster = _roster_of(
            _entry("local-a", tier="local", good_at=["grunt", "code"]),
            _entry("sub-a", tier="sub", good_at=["code", "summarization"]),
            _entry("paid", tier="api", kind="api", good_at=["hard"]),  # excluded
        )
        self.assertEqual(available_capabilities(roster), ["code", "grunt", "summarization"])


class RunDelegateCapabilityTest(unittest.TestCase):
    """run_delegate routing by task, plus target>task precedence."""

    def setUp(self):
        patcher = patch("tanglebrain.delegate.record_task")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_task_routes_to_selected_leaf(self):
        entry = _entry("sub-a", tier="sub", good_at=["code"])
        roster = _roster_of(entry)
        adapter = MagicMock()
        adapter.run.return_value = "coded"
        with patch("tanglebrain.delegate.load_roster", return_value=roster), patch(
            "tanglebrain.delegate.build_adapter", return_value=adapter
        ) as build:
            out = run_delegate("write code", task="code")
        self.assertEqual(out, "coded")
        self.assertIs(build.call_args.args[0], entry)
        self.assertEqual(build.call_args.kwargs.get("inject_delegate"), False)

    def test_target_wins_over_task(self):
        # When both are given, the explicit target id is used and capability selection is not consulted.
        target_entry = _entry("explicit", can_delegate=True)
        roster = MagicMock()
        roster.by_id.return_value = target_entry
        adapter = MagicMock()
        adapter.run.return_value = "x"
        with patch("tanglebrain.delegate.load_roster", return_value=roster), patch(
            "tanglebrain.delegate.build_adapter", return_value=adapter
        ) as build:
            run_delegate("q", target="explicit", task="code")
        roster.by_id.assert_called_once_with("explicit")
        roster.delegate_targets.assert_not_called()  # capability path not taken
        self.assertIs(build.call_args.args[0], target_entry)

    def test_task_no_fit_propagates_nodelegatefit(self):
        roster = _roster_of(_entry("sub-a", tier="sub", good_at=["grunt"]))
        with patch("tanglebrain.delegate.load_roster", return_value=roster):
            with self.assertRaises(NoDelegateFit):
                run_delegate("q", task="code")

    def test_nodelegatefit_is_not_a_selectionerror(self):
        # Load-bearing invariant: NoDelegateFit is a routing SIGNAL, not a refusal. It must NOT be a
        # SelectionError subclass, or the MCP boundary's NoDelegateFit handler (and any `except
        # SelectionError`) would conflate "no fit, you handle it" with "bad target id" errors.
        self.assertFalse(issubclass(NoDelegateFit, SelectionError))


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


class FanOutConcurrencyTest(unittest.TestCase):
    """Concurrency-cap resolution for delegate_many: system-derived default, operator + per-call."""

    def test_default_concurrency_uses_cpu_count(self):
        with patch("tanglebrain.delegate.os.cpu_count", return_value=11):
            self.assertEqual(_default_concurrency(), 11)

    def test_default_concurrency_fallback_when_cpu_count_none(self):
        with patch("tanglebrain.delegate.os.cpu_count", return_value=None):
            self.assertEqual(_default_concurrency(), DEFAULT_CONCURRENCY_FALLBACK)

    def test_operator_setting_wins_over_derived(self):
        with patch("tanglebrain.delegate._default_concurrency", return_value=99):
            self.assertEqual(_effective_concurrency(Settings(delegate_max_concurrency=3), None), 3)

    def test_derived_used_when_setting_unset(self):
        with patch("tanglebrain.delegate._default_concurrency", return_value=7):
            self.assertEqual(_effective_concurrency(Settings(), None), 7)

    def test_per_call_lowers_but_cannot_exceed(self):
        s = Settings(delegate_max_concurrency=5)
        self.assertEqual(_effective_concurrency(s, 2), 2)
        self.assertEqual(_effective_concurrency(s, 100), 5)

    def test_clamps_to_at_least_one(self):
        self.assertEqual(_effective_concurrency(Settings(delegate_max_concurrency=5), 0), 1)


class RunDelegateManyTest(unittest.TestCase):
    """The parallel fan-out primitive: input-order results, per-item routing, partial-failure isolation."""

    def _patches(self, fake_run_delegate):
        return patch("tanglebrain.delegate.run_delegate", side_effect=fake_run_delegate), patch(
            "tanglebrain.delegate.load_settings", return_value=Settings()
        )

    def test_empty_list_returns_empty(self):
        self.assertEqual(run_delegate_many([]), [])

    def test_non_list_raises(self):
        with self.assertRaises(ValueError):
            run_delegate_many("not a list")

    def test_results_in_input_order(self):
        run_dp, load = self._patches(lambda prompt, **kw: f"out:{prompt}")
        with run_dp, load:
            out = run_delegate_many([{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}])
        self.assertEqual([r["index"] for r in out], [0, 1, 2])
        self.assertEqual([r["text"] for r in out], ["out:a", "out:b", "out:c"])
        self.assertTrue(all(r["status"] == "ok" for r in out))

    def test_per_item_routing_threaded_through(self):
        calls = {}

        def fake(prompt, **kw):
            calls[prompt] = kw
            return "x"

        run_dp, load = self._patches(fake)
        with run_dp, load:
            run_delegate_many(
                [
                    {"prompt": "p1", "target": "sub-a"},
                    {"prompt": "p2", "task": "code", "max_tokens": 256},
                ]
            )
        self.assertEqual(calls["p1"]["target"], "sub-a")
        self.assertIsNone(calls["p1"]["task"])
        self.assertEqual(calls["p1"]["max_tokens"], DEFAULT_DELEGATE_MAX_TOKENS)  # item default
        self.assertEqual(calls["p2"]["task"], "code")
        self.assertEqual(calls["p2"]["max_tokens"], 256)

    def test_partial_failure_isolated(self):
        def fake(prompt, **kw):
            if prompt == "bad":
                raise AdapterError("backend down")
            return "ok-text"

        run_dp, load = self._patches(fake)
        with run_dp, load:
            out = run_delegate_many([{"prompt": "good"}, {"prompt": "bad"}, {"prompt": "good2"}])
        self.assertEqual(out[0]["status"], "ok")
        self.assertEqual(out[1]["status"], "error")
        self.assertIn("backend down", out[1]["error"])
        self.assertEqual(out[2]["status"], "ok")

    def test_no_fit_item_reported(self):
        run_dp, load = self._patches(
            lambda prompt, **kw: (_ for _ in ()).throw(NoDelegateFit("no target good_at 'x'"))
        )
        with run_dp, load:
            out = run_delegate_many([{"prompt": "p", "task": "x"}])
        self.assertEqual(out[0]["status"], "no_fit")
        self.assertIn("yourself", out[0]["message"])

    def test_malformed_items_do_not_sink_batch(self):
        run_dp, load = self._patches(lambda prompt, **kw: "ok")
        with run_dp, load:
            out = run_delegate_many([{"prompt": "good"}, {"no_prompt": 1}, "notadict"])
        self.assertEqual(out[0]["status"], "ok")
        self.assertEqual(out[1]["status"], "error")
        self.assertEqual(out[2]["status"], "error")

    def test_concurrency_cap_passed_to_executor(self):
        captured = {}

        class FakeExecutor:
            def __init__(self, max_workers=None):
                captured["max_workers"] = max_workers

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def submit(self, fn, *args):
                future = MagicMock()
                future.result.return_value = fn(*args)
                return future

        with patch("tanglebrain.delegate.run_delegate", side_effect=lambda prompt, **kw: "x"), patch(
            "tanglebrain.delegate.ThreadPoolExecutor", FakeExecutor
        ):
            run_delegate_many([{"prompt": "a"}], settings=Settings(delegate_max_concurrency=3))
        self.assertEqual(captured["max_workers"], 3)


class DelegateMeteringTest(unittest.TestCase):
    """run_delegate meters each sub-call as a kind='delegate' usage record (observability)."""

    def test_records_delegate_kind(self):
        entry = _entry("local-x", tier="local", can_delegate=True)
        adapter = MagicMock()
        adapter.run.return_value = "result-text"
        with patch("tanglebrain.delegate.load_roster", return_value=MagicMock()), patch(
            "tanglebrain.delegate.select_local", return_value=entry
        ), patch("tanglebrain.delegate.build_adapter", return_value=adapter), patch(
            "tanglebrain.delegate.record_task"
        ) as rec:
            out = run_delegate("do it")
        self.assertEqual(out, "result-text")
        rec.assert_called_once()
        kw = rec.call_args.kwargs
        self.assertEqual(kw["kind"], "delegate")
        self.assertEqual(kw["path"], "delegate")
        self.assertIs(kw["entry"], entry)
        self.assertEqual(kw["prompt"], "do it")
        self.assertEqual(kw["response"], "result-text")

    def test_metering_failure_never_breaks_delegation(self):
        entry = _entry("local-x", tier="local")
        adapter = MagicMock()
        adapter.run.return_value = "answer"
        with patch("tanglebrain.delegate.load_roster", return_value=MagicMock()), patch(
            "tanglebrain.delegate.select_local", return_value=entry
        ), patch("tanglebrain.delegate.build_adapter", return_value=adapter), patch(
            "tanglebrain.delegate.record_task", side_effect=RuntimeError("log boom")
        ):
            out = run_delegate("q")
        self.assertEqual(out, "answer")  # metering error swallowed, answer still returned


if __name__ == "__main__":
    unittest.main()
