"""Tests for the frontier-first router (tanglebrain/router.py).

Adapters are faked and the rotation-state file is a temp path, so these are fully hermetic —
no subprocesses, no network, no touching the real ~/.cache state.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tanglebrain.adapters.base import AdapterError
from tanglebrain.roster import Invoke, Roster, RosterEntry
from tanglebrain.router import (
    STATE_DIR_ENV,
    Router,
    RouterError,
    _looks_like_rate_limit,
    _read_cursor,
    _write_cursor,
    default_state_path,
)


def orch(entry_id: str, good_at=()) -> RosterEntry:
    """An orchestrator-capable sub entry."""
    return RosterEntry(
        id=entry_id,
        tier="sub",
        invoke=Invoke(kind="cli", cmd=[entry_id]),
        good_at=list(good_at),
        can_orchestrate=True,
    )


def worker(entry_id: str) -> RosterEntry:
    """A non-orchestrator entry (the local tier)."""
    return RosterEntry(id=entry_id, tier="local", invoke=Invoke(kind="openai-compat", base_url="u", model="m"))


def factory(outcomes: dict[str, tuple[str, str]]):
    """Build an adapter_factory from {id: ('ok', text) | ('err', message)}.

    Accepts the ``inject_delegate`` kwarg the Router passes (C3b), ignored here.
    """

    def make(entry: RosterEntry, inject_delegate: bool = False):
        adapter = MagicMock()
        kind, value = outcomes[entry.id]
        if kind == "ok":
            adapter.run.return_value = value
        else:
            adapter.run.side_effect = AdapterError(value)
        return adapter

    return make


class RouterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = Path(self.tmp) / "router-state.json"
        # claude=reasoning, codex=code, gemini=long-context — the §5 starting roster.
        self.roster = Roster(
            [
                orch("claude", ["reasoning", "decomposition"]),
                orch("codex", ["code", "agentic-code"]),
                orch("gemini", ["long-context"]),
            ]
        )

    def _router(self, outcomes):
        return Router(self.roster, state_path=self.state, adapter_factory=factory(outcomes))


class StateHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "sub" / "router-state.json"

    def test_missing_file_reads_zero(self):
        self.assertEqual(_read_cursor(self.path), 0)

    def test_roundtrip(self):
        _write_cursor(self.path, 2)
        self.assertEqual(_read_cursor(self.path), 2)

    def test_corrupt_json_reads_zero(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json")
        self.assertEqual(_read_cursor(self.path), 0)

    def test_negative_cursor_clamped_to_zero(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"cursor": -5}))
        self.assertEqual(_read_cursor(self.path), 0)

    def test_default_state_path_honors_env(self):
        with patch.dict(os.environ, {STATE_DIR_ENV: "/tmp/tb-state"}, clear=False):
            self.assertEqual(default_state_path(), Path("/tmp/tb-state/router-state.json"))

    def test_default_state_path_falls_back_to_home_cache(self):
        env = {k: v for k, v in os.environ.items() if k != STATE_DIR_ENV}
        with patch.dict(os.environ, env, clear=True):
            path = default_state_path()
        self.assertEqual(path, Path.home() / ".cache" / "tanglebrain" / "router-state.json")


class SelectionTest(RouterTestBase):
    def test_no_orchestrators_raises(self):
        router = Router(Roster([worker("gpt-oss")]), state_path=self.state)
        with self.assertRaises(RouterError):
            router.route("q")

    def test_task_fit_prefers_matching_orchestrator(self):
        out = self._router({"claude": ("err", "x"), "codex": ("ok", "from-codex"), "gemini": ("err", "x")})
        # task=code should pick codex even though claude is first in rotation.
        self.assertEqual(out.route("write a function", task="code"), "from-codex")

    def test_unknown_task_falls_back_to_all(self):
        # No orchestrator is good_at 'astrology' -> fall back to full rotation (starts at claude).
        out = self._router({"claude": ("ok", "from-claude"), "codex": ("ok", "x"), "gemini": ("ok", "x")})
        self.assertEqual(out.route("q", task="astrology"), "from-claude")

    def test_no_task_uses_rotation_start(self):
        out = self._router({"claude": ("ok", "from-claude"), "codex": ("ok", "x"), "gemini": ("ok", "x")})
        self.assertEqual(out.route("q"), "from-claude")


class RotationTest(RouterTestBase):
    def test_cursor_advances_after_success(self):
        self._router({"claude": ("ok", "a"), "codex": ("ok", "b"), "gemini": ("ok", "c")}).route("q")
        # Served claude (pos 0) -> cursor moves to 1.
        self.assertEqual(_read_cursor(self.state), 1)

    def test_successive_calls_spread_across_subs(self):
        outcomes = {"claude": ("ok", "claude"), "codex": ("ok", "codex"), "gemini": ("ok", "gemini")}
        served = [self._router(outcomes).route("q") for _ in range(4)]
        # Fresh Router each call re-reads the persisted cursor: round-robin then wrap.
        self.assertEqual(served, ["claude", "codex", "gemini", "claude"])

    def test_wraparound(self):
        _write_cursor(self.state, 2)  # start at gemini
        out = self._router({"claude": ("ok", "a"), "codex": ("ok", "b"), "gemini": ("ok", "gemini")})
        self.assertEqual(out.route("q"), "gemini")
        self.assertEqual(_read_cursor(self.state), 0)  # past gemini -> wraps to 0

    def test_task_fit_with_midlist_cursor_advances_to_full_list_pos(self):
        # The cursor must track the served orchestrator's position in the FULL orchestrator list,
        # not its index within the task-filtered candidate sublist. Start mid-list (at gemini) and
        # filter to a single fitting sub (codex, full-list pos 1).
        _write_cursor(self.state, 2)
        out = self._router({"claude": ("ok", "x"), "codex": ("ok", "from-codex"), "gemini": ("ok", "x")})
        self.assertEqual(out.route("q", task="code"), "from-codex")
        self.assertEqual(_read_cursor(self.state), 2)  # codex full-list pos 1 + 1 — NOT a sublist index


class FailoverTest(RouterTestBase):
    def test_fails_over_to_next_on_error(self):
        out = self._router({"claude": ("err", "boom"), "codex": ("ok", "from-codex"), "gemini": ("ok", "x")})
        self.assertEqual(out.route("q"), "from-codex")
        # Cursor advances past the orchestrator that actually served (codex, pos 1) -> 2.
        self.assertEqual(_read_cursor(self.state), 2)

    def test_all_fail_raises_with_each_failure(self):
        out = self._router({"claude": ("err", "e1"), "codex": ("err", "e2"), "gemini": ("err", "e3")})
        with self.assertRaises(RouterError) as ctx:
            out.route("q")
        msg = str(ctx.exception)
        for eid in ("claude", "codex", "gemini"):
            self.assertIn(eid, msg)

    def test_total_failure_does_not_advance_cursor(self):
        out = self._router({"claude": ("err", "e"), "codex": ("err", "e"), "gemini": ("err", "e")})
        with self.assertRaises(RouterError):
            out.route("q")
        self.assertEqual(_read_cursor(self.state), 0)  # unchanged — only success advances

    def test_rate_limit_annotated_in_error(self):
        out = self._router(
            {"claude": ("err", "HTTP 429 Too Many Requests"), "codex": ("err", "boom"), "gemini": ("err", "boom")}
        )
        with self.assertRaises(RouterError) as ctx:
            out.route("q")
        self.assertIn("[rate-limit]", str(ctx.exception))

    def test_opts_passed_through_to_adapter(self):
        captured = {}

        def fac(entry, inject_delegate=False):
            adapter = MagicMock()
            adapter.run.side_effect = lambda p, o: captured.update(prompt=p, opts=o) or "ok"
            return adapter

        Router(self.roster, state_path=self.state, adapter_factory=fac).route(
            "q", opts={"max_tokens": 99}
        )
        self.assertEqual(captured["opts"], {"max_tokens": 99})

    def test_router_enables_delegate_injection_by_default(self):
        seen = {}

        def fac(entry, inject_delegate=False):
            seen[entry.id] = inject_delegate
            adapter = MagicMock()
            adapter.run.return_value = "ok"
            return adapter

        Router(self.roster, state_path=self.state, adapter_factory=fac).route("q")
        self.assertTrue(all(seen.values()), "router should build orchestrator adapters with the delegate")

    def test_inject_delegate_false_propagates(self):
        seen = {}

        def fac(entry, inject_delegate=False):
            seen[entry.id] = inject_delegate
            adapter = MagicMock()
            adapter.run.return_value = "ok"
            return adapter

        Router(self.roster, state_path=self.state, adapter_factory=fac, inject_delegate=False).route("q")
        self.assertFalse(any(seen.values()))


class RateLimitClassifierTest(unittest.TestCase):
    def test_positive_cases(self):
        for m in ("HTTP 429", "rate limit exceeded", "RESOURCE_EXHAUSTED", "quota reached", "overloaded", "Too Many Requests"):
            self.assertTrue(_looks_like_rate_limit(m), m)

    def test_negative_cases(self):
        for m in ("connection refused", "binary not found", "", "exit 1: bad flag"):
            self.assertFalse(_looks_like_rate_limit(m), m)


if __name__ == "__main__":
    unittest.main()
