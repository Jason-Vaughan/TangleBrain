"""Tests for the frontier-first router (tanglebrain/router.py).

Adapters are faked and the rotation-state file is a temp path, so these are fully hermetic —
no subprocesses, no network, no touching the operator's real state root.
"""
from __future__ import annotations

import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — covered by the 3.11/3.12 CI jobs.
    tomllib = None  # type: ignore[assignment]

from tanglebrain.adapters.base import AdapterError
from tanglebrain.roster import Invoke, Roster, RosterEntry
from tanglebrain.router import (
    LEGACY_STATE_SUBDIR,
    STATE_DIR_ENV,
    XDG_DATA_HOME_ENV,
    Router,
    RouterError,
    _looks_like_rate_limit,
    _read_cursor,
    _write_cursor,
    default_state_path,
    legacy_state_root,
    migrate_state_root,
    state_root,
)
from tanglebrain.settings import Settings


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


def api(entry_id: str, enabled: bool = True) -> RosterEntry:
    """A paid-API tier entry (the last-resort fallback)."""
    return RosterEntry(
        id=entry_id,
        tier="api",
        invoke=Invoke(kind="api", base_url="u", model="m", key_ref="none"),
        enabled=enabled,
    )


def factory(outcomes: dict[str, tuple[str, str]]):
    """Build an adapter_factory from {id: ('ok', text) | ('err', message)}.

    Accepts the ``inject_delegate`` kwarg the Router passes, ignored here.
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
        # claude=reasoning, codex=code, gemini=long-context — a starting roster.
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


def _env_without(*names: str) -> dict:
    """Return a copy of the environment with ``names`` removed, for `patch.dict(clear=True)`."""
    return {k: v for k, v in os.environ.items() if k not in names}


class StateRootTest(unittest.TestCase):
    """Where persistent state resolves to, and the precedence between the three inputs.

    The root moved off ``~/.cache`` because nothing under it is a cache — the usage log carries
    the product's lifetime spend-avoided claim and is not reconstructible, and a config backup is
    the only copy of something the operator hand-edited. ``~/.cache`` is *defined* as deletable at
    will, so the old default made both one cleanup run from gone.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_explicit_override_wins_over_everything(self):
        env = {STATE_DIR_ENV: "/tmp/tb-explicit", XDG_DATA_HOME_ENV: "/tmp/tb-xdg", "HOME": self.tmp}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(state_root(), Path("/tmp/tb-explicit"))

    def test_xdg_data_home_wins_over_the_default(self):
        env = _env_without(STATE_DIR_ENV)
        env.update({XDG_DATA_HOME_ENV: "/tmp/tb-xdg", "HOME": self.tmp})
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(state_root(), Path("/tmp/tb-xdg/tanglebrain"))

    def test_default_is_the_xdg_data_tier_not_the_cache_tier(self):
        # Updated deliberately when the root moved (was `~/.cache/tanglebrain`). The assertion is
        # the decision: a data-tier default is what makes the usage log safe to keep forever.
        env = _env_without(STATE_DIR_ENV, XDG_DATA_HOME_ENV)
        env["HOME"] = self.tmp
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(state_root(), Path(self.tmp) / ".local" / "share" / "tanglebrain")
            self.assertEqual(
                default_state_path(),
                Path(self.tmp) / ".local" / "share" / "tanglebrain" / "router-state.json",
            )

    def test_legacy_root_is_the_cache_tier(self):
        env = _env_without(STATE_DIR_ENV, XDG_DATA_HOME_ENV)
        env["HOME"] = self.tmp
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(legacy_state_root(), Path(self.tmp) / ".cache" / "tanglebrain")

    def test_an_explicit_override_collapses_both_roots(self):
        # The override was always the state root, wherever the operator put it — so there is
        # nothing to migrate, and `migrate_state_root` must recognise that rather than copy a
        # directory onto itself.
        with patch.dict(os.environ, {STATE_DIR_ENV: "/tmp/tb-same"}, clear=False):
            self.assertEqual(state_root(), legacy_state_root())


class MigrateStateRootTest(unittest.TestCase):
    """Moving a pre-0.21 cache-tier root forward, without ever losing a fact."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.legacy = self.home / LEGACY_STATE_SUBDIR
        self.new = self.home / ".local" / "share" / "tanglebrain"
        env = _env_without(STATE_DIR_ENV, XDG_DATA_HOME_ENV)
        env["HOME"] = str(self.home)
        self._env = patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _seed_legacy(self):
        """Write one of each thing the real legacy root holds: two files and a backups dir."""
        self.legacy.mkdir(parents=True, exist_ok=True)
        (self.legacy / "usage.jsonl").write_text('{"kind": "task"}\n', encoding="utf-8")
        (self.legacy / "router-state.json").write_text('{"cursor": 3}', encoding="utf-8")
        (self.legacy / "backups").mkdir()
        (self.legacy / "backups" / "pricing-x.yaml").write_text("placeholder: false\n", encoding="utf-8")

    def test_every_entry_migrates_not_just_the_usage_log(self):
        self._seed_legacy()
        migrated = migrate_state_root(stream=io.StringIO())
        self.assertEqual(sorted(migrated), ["backups", "router-state.json", "usage.jsonl"])
        self.assertEqual((self.new / "usage.jsonl").read_text(encoding="utf-8"), '{"kind": "task"}\n')
        self.assertEqual((self.new / "router-state.json").read_text(encoding="utf-8"), '{"cursor": 3}')
        self.assertTrue((self.new / "backups" / "pricing-x.yaml").is_file())

    def test_originals_are_left_in_place_so_a_downgrade_keeps_working(self):
        self._seed_legacy()
        migrate_state_root(stream=io.StringIO())
        self.assertTrue((self.legacy / "usage.jsonl").is_file())
        self.assertTrue((self.legacy / "router-state.json").is_file())
        self.assertTrue((self.legacy / "backups" / "pricing-x.yaml").is_file())

    def test_one_notice_for_the_move_not_one_per_file(self):
        self._seed_legacy()
        out = io.StringIO()
        migrate_state_root(stream=out)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f"expected a single notice, got {lines!r}")
        self.assertIn(str(self.legacy), lines[0])
        self.assertIn(str(self.new), lines[0])

    def test_second_run_copies_nothing_and_says_nothing(self):
        self._seed_legacy()
        migrate_state_root(stream=io.StringIO())
        out = io.StringIO()
        self.assertEqual(migrate_state_root(stream=out), [])
        self.assertEqual(out.getvalue(), "")

    def test_a_partially_migrated_root_completes_rather_than_skipping(self):
        self._seed_legacy()
        self.new.mkdir(parents=True)
        (self.new / "usage.jsonl").write_text("already here\n", encoding="utf-8")
        migrated = migrate_state_root(stream=io.StringIO())
        # The one already present is not re-copied (and not clobbered); the rest complete.
        self.assertEqual(sorted(migrated), ["backups", "router-state.json"])
        self.assertEqual((self.new / "usage.jsonl").read_text(encoding="utf-8"), "already here\n")
        self.assertTrue((self.new / "router-state.json").is_file())

    def test_a_fresh_install_migrates_nothing_and_prints_nothing(self):
        out = io.StringIO()
        self.assertEqual(migrate_state_root(stream=out), [])
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(self.new.exists(), "a fresh install must not create the root just to look")

    def test_an_explicit_override_is_a_no_op(self):
        self._seed_legacy()
        with patch.dict(os.environ, {STATE_DIR_ENV: str(self.legacy)}, clear=False):
            out = io.StringIO()
            self.assertEqual(migrate_state_root(stream=out), [])
            self.assertEqual(out.getvalue(), "")

    def test_a_failed_migration_warns_and_never_raises(self):
        self._seed_legacy()
        out = io.StringIO()
        with patch("tanglebrain.router.shutil.copy2", side_effect=OSError("disk full")):
            # Whatever got across before the failure is reported, so the next run finishes the
            # rest rather than starting over or declaring itself done.
            self.assertEqual(migrate_state_root(stream=out), ["backups"])
        text = out.getvalue()
        self.assertIn("disk full", text)
        self.assertIn(str(self.legacy), text)
        # The operator has to be able to tell a failed copy from a genuinely small figure.
        self.assertIn("--stats", text)

    def test_a_copy_killed_part_way_leaves_no_partial_file_at_the_real_path(self):
        """The re-run guard is "does the destination exist", so a truncated file is permanent.

        `copy2` streams into its destination and does not clean up on failure. Writing straight to
        the final name would leave a short `usage.jsonl` that every later run skips, understating
        the lifetime figure forever while the failure notice promises a retry that can never fix
        it. Staging under a temporary name and `os.replace`-ing into place is what makes the guard
        mean what it says.
        """
        self._seed_legacy()

        def truncated_write(src, dst, *args, **kwargs):
            Path(dst).write_text("half a fi", encoding="utf-8")  # bytes land, then it dies
            raise OSError("disk full")

        with patch("tanglebrain.router.shutil.copy2", side_effect=truncated_write):
            migrate_state_root(stream=io.StringIO())

        # Assert over whatever landed rather than a named file: `copy2` dies on the first entry it
        # is handed, and which one that is depends on iteration order. Naming a file here is how
        # this test passes without ever exercising the defect.
        landed = [f for f in self.new.rglob("*") if f.is_file()] if self.new.exists() else []
        self.assertTrue(landed, "the fixture never reached a copy — this test would prove nothing")
        for f in landed:
            original = self.legacy / f.relative_to(self.new)
            self.assertEqual(
                f.read_bytes(), original.read_bytes(),
                f"{f.name} is at its real path but truncated; every later run will skip it",
            )
        self.assertFalse(list(self.new.glob(".*.tmp")), "staging entries must not survive a failure")
        # And the retry the notice promises actually works.
        migrate_state_root(stream=io.StringIO())
        self.assertEqual(
            (self.new / "usage.jsonl").read_text(encoding="utf-8"), '{"kind": "task"}\n'
        )
        self.assertEqual(
            (self.new / "router-state.json").read_text(encoding="utf-8"), '{"cursor": 3}'
        )

    def test_a_concurrent_start_that_loses_the_race_is_not_reported_as_a_failure(self):
        """Two console scripts can start at once; the loser must stay quiet, not warn.

        `os.replace` onto an existing directory raises, and the entries are identical bytes from
        one source — so losing is a no-op, not an error worth alarming the operator about.
        """
        self._seed_legacy()
        real_replace = os.replace

        def replace_after_rival_wins(src, dst, *args, **kwargs):
            if Path(dst).name == "backups":
                # Simulate the rival landing its copy in the window before ours.
                shutil.copytree(self.legacy / "backups", dst)
                raise OSError("Directory not empty")
            return real_replace(src, dst, *args, **kwargs)

        out = io.StringIO()
        with patch("tanglebrain.router.os.replace", side_effect=replace_after_rival_wins):
            migrated = migrate_state_root(stream=out)
        self.assertNotIn("could not move state", out.getvalue())
        self.assertEqual(sorted(migrated), ["router-state.json", "usage.jsonl"])
        self.assertTrue((self.new / "backups" / "pricing-x.yaml").is_file())

    def test_a_failing_concurrent_copy_cannot_delete_another_process_staging_file(self):
        """A rival's failed staging cleanup must not remove this process's complete copy."""
        self.legacy.mkdir(parents=True)
        source = self.legacy / "usage.jsonl"
        source.write_text('{"kind": "task"}\n', encoding="utf-8")
        copy_calls = 0
        rival_out = io.StringIO()

        def copy_while_rival_fails(src, dst, *args, **kwargs):
            nonlocal copy_calls
            copy_calls += 1
            if copy_calls == 1:
                Path(dst).write_bytes(Path(src).read_bytes())
                migrate_state_root(stream=rival_out)
                return dst
            Path(dst).write_text("partial", encoding="utf-8")
            raise OSError("rival copy stopped")

        out = io.StringIO()
        with patch("tanglebrain.router.shutil.copy2", side_effect=copy_while_rival_fails):
            migrated = migrate_state_root(stream=out)

        self.assertEqual(migrated, ["usage.jsonl"])
        self.assertIn("state moved", out.getvalue())
        self.assertNotIn("could not move state", out.getvalue())
        self.assertIn("rival copy stopped", rival_out.getvalue())
        self.assertEqual((self.new / "usage.jsonl").read_bytes(), source.read_bytes())
        self.assertEqual(list(self.new.glob(".*.tmp")), [])

    def test_the_notice_never_goes_to_stdout(self):
        # stdout carries the routed answer and gets piped; a notice there corrupts it.
        self._seed_legacy()
        with patch.object(sys, "stdout", io.StringIO()) as fake_out, \
             patch.object(sys, "stderr", io.StringIO()) as fake_err:
            migrate_state_root()
            self.assertEqual(fake_out.getvalue(), "")
            self.assertIn("state moved", fake_err.getvalue())


@unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+; covered by the 3.11/3.12 CI jobs")
class EntryPointMigrationCoverageTest(unittest.TestCase):
    """Every console script must migrate before it reads state — enforced, not asserted in prose.

    The list is derived from `[project.scripts]` rather than written here, so adding a fifth
    entry point without wiring the migration fails this test. A hand-maintained list would have
    gone stale at exactly the moment it mattered.
    """

    def test_every_console_script_calls_migrate_state_root(self):
        repo_root = Path(__file__).resolve().parents[1]
        scripts = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["scripts"]
        self.assertGreaterEqual(len(scripts), 4, "entry points vanished — check pyproject.toml")
        for name, target in sorted(scripts.items()):
            with self.subTest(script=name):
                module_name, _, func_name = target.partition(":")
                module = __import__(module_name, fromlist=[func_name])
                source = inspect.getsource(getattr(module, func_name))
                self.assertIn(
                    "migrate_state_root()", source,
                    f"{name} ({target}) reads state without migrating a legacy root first",
                )


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

    def test_last_failures_surfaces_lost_attempts(self):
        # #100: route() exposes the attempts it lost — like last_served — so the CLI's
        # measurement seam can record failovers and total failures.
        out = self._router({"claude": ("err", "boom"), "codex": ("ok", "from-codex"), "gemini": ("ok", "x")})
        self.assertEqual(out.route("q"), "from-codex")
        self.assertEqual(out.last_failures, [("claude", "boom")])
        # A later first-try success (rotation now starts at gemini) resets the list.
        self.assertEqual(out.route("q"), "x")
        self.assertEqual(out.last_failures, [])

    def test_last_failures_on_total_failure(self):
        out = self._router({"claude": ("err", "e1"), "codex": ("err", "e2"), "gemini": ("err", "e3")})
        with self.assertRaises(RouterError):
            out.route("q")
        self.assertEqual(
            out.last_failures, [("claude", "e1"), ("codex", "e2"), ("gemini", "e3")]
        )

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
        """Orchestrators the router builds end up holding the delegate tool.

        Asserts the adapter that results rather than the argument the factory received: the
        router defers the decision to build_adapter, which derives it from the entry, so a test
        pinned to the argument would track the mechanism instead of the contract.
        """
        from tanglebrain.selector import build_adapter

        seen = {}

        def fac(entry, inject_delegate=None):
            seen[entry.id] = build_adapter(entry, inject_delegate=inject_delegate).inject_delegate
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


class LastResortApiFallbackTest(RouterTestBase):
    """Paid-API entries are the genuine last resort — reached only after every orchestrator
    fails AND the billing gate is on. Off by default, so the router never reaches a paid tier."""

    def mk(self, roster, outcomes, billing=True):
        return Router(
            roster,
            state_path=self.state,
            adapter_factory=factory(outcomes),
            settings=Settings(api_billing_enabled=billing),
        )

    _ALL_SUBS_FAIL = {"claude": ("err", "e"), "codex": ("err", "e"), "gemini": ("err", "e")}

    def test_falls_through_to_api_when_all_orchestrators_fail(self):
        roster = Roster([orch("claude"), orch("codex"), orch("gemini"), api("gpt5")])
        r = self.mk(roster, {**self._ALL_SUBS_FAIL, "gpt5": ("ok", "paid-answer")})
        self.assertEqual(r.route("q"), "paid-answer")
        self.assertEqual(r.last_served.id, "gpt5")
        self.assertEqual(r.last_served.tier, "api")

    def test_api_not_reached_when_gate_off(self):
        # gpt5 WOULD succeed, but with billing off the router must never attempt it.
        roster = Roster([orch("claude"), orch("codex"), orch("gemini"), api("gpt5")])
        r = self.mk(roster, {**self._ALL_SUBS_FAIL, "gpt5": ("ok", "paid")}, billing=False)
        with self.assertRaises(RouterError) as ctx:
            r.route("q")
        self.assertNotIn("gpt5", str(ctx.exception))  # never even tried

    def test_api_not_reached_when_an_orchestrator_succeeds(self):
        roster = Roster([orch("claude"), orch("codex"), orch("gemini"), api("gpt5")])
        r = self.mk(
            roster,
            {"claude": ("ok", "from-claude"), "codex": ("ok", "x"), "gemini": ("ok", "x"),
             "gpt5": ("err", "should-not-run")},
        )
        self.assertEqual(r.route("q"), "from-claude")
        self.assertEqual(r.last_served.id, "claude")

    def test_disabled_api_entry_is_skipped(self):
        roster = Roster([orch("claude"), orch("codex"), orch("gemini"),
                         api("paid-off", enabled=False), api("paid-on")])
        r = self.mk(roster, {**self._ALL_SUBS_FAIL, "paid-off": ("ok", "NO"), "paid-on": ("ok", "YES")})
        self.assertEqual(r.route("q"), "YES")
        self.assertEqual(r.last_served.id, "paid-on")

    def test_api_entries_tried_in_roster_order(self):
        roster = Roster([orch("claude"), orch("codex"), orch("gemini"), api("first"), api("second")])
        r = self.mk(roster, {**self._ALL_SUBS_FAIL, "first": ("ok", "FIRST"), "second": ("ok", "SECOND")})
        self.assertEqual(r.route("q"), "FIRST")

    def test_api_failure_fails_over_to_next_api(self):
        roster = Roster([orch("claude"), orch("codex"), orch("gemini"), api("first"), api("second")])
        r = self.mk(roster, {**self._ALL_SUBS_FAIL, "first": ("err", "paid boom"), "second": ("ok", "SECOND")})
        self.assertEqual(r.route("q"), "SECOND")

    def test_all_fail_including_api_raises_and_lists_api_with_rate_limit(self):
        roster = Roster([orch("claude"), orch("codex"), orch("gemini"), api("gpt5")])
        r = self.mk(roster, {"claude": ("err", "e1"), "codex": ("err", "e2"),
                             "gemini": ("err", "e3"), "gpt5": ("err", "HTTP 429 quota")})
        with self.assertRaises(RouterError) as ctx:
            r.route("q")
        msg = str(ctx.exception)
        self.assertIn("gpt5", msg)
        self.assertIn("[rate-limit]", msg)  # api failures get the same annotation as orchestrators

    def test_api_success_does_not_advance_orchestrator_cursor(self):
        # Seed a non-zero cursor so this proves "unchanged", not "coincidentally 0" (a missing
        # state file also reads 0). After a paid success the orchestrator cursor must be untouched.
        _write_cursor(self.state, 2)
        roster = Roster([orch("claude"), orch("codex"), orch("gemini"), api("gpt5")])
        r = self.mk(roster, {**self._ALL_SUBS_FAIL, "gpt5": ("ok", "paid")})
        r.route("q")
        self.assertEqual(_read_cursor(self.state), 2)  # unchanged — api is not in the rotation

    def test_api_orchestrator_is_not_double_attempted(self):
        # Degenerate config: a paid entry flagged can_orchestrate is in the rotation. With the gate
        # on it must be tried ONCE (as an orchestrator), not again in the api fallback block.
        calls = {"claude": 0}

        def fac(entry, inject_delegate=False):
            adapter = MagicMock()

            def run(p, o, _id=entry.id):
                calls[_id] = calls.get(_id, 0) + 1
                raise AdapterError("boom")

            adapter.run.side_effect = run
            return adapter

        paid_orch = RosterEntry(
            id="claude", tier="api",
            invoke=Invoke(kind="api", base_url="u", model="m", key_ref="none"),
            can_orchestrate=True,
        )
        roster = Roster([paid_orch, orch("codex")])
        r = Router(roster, state_path=self.state, adapter_factory=fac,
                   settings=Settings(api_billing_enabled=True))
        with self.assertRaises(RouterError):
            r.route("q")
        self.assertEqual(calls["claude"], 1)  # not re-run by the fallback loop

    def test_no_orchestrators_never_paid_routes(self):
        # A roster with only a paid entry + gate on must still raise — never silently paid-route.
        # (use --model for an explicit paid call; the router needs subs to exhaust first.)
        roster = Roster([api("gpt5")])
        r = self.mk(roster, {"gpt5": ("ok", "paid")})
        with self.assertRaises(RouterError):
            r.route("q")


class RateLimitClassifierTest(unittest.TestCase):
    def test_positive_cases(self):
        for m in ("HTTP 429", "rate limit exceeded", "RESOURCE_EXHAUSTED", "quota reached", "overloaded", "Too Many Requests"):
            self.assertTrue(_looks_like_rate_limit(m), m)

    def test_negative_cases(self):
        for m in ("connection refused", "binary not found", "", "exit 1: bad flag"):
            self.assertFalse(_looks_like_rate_limit(m), m)


if __name__ == "__main__":
    unittest.main()
