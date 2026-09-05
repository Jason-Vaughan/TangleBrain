"""Pinning a backend with ``--model`` must not disable its delegation.

Pinning *which* backend serves a request is a different decision from *whether* that backend
may delegate, and one must not silently imply the other. The symptom of the old behaviour was
almost invisible: no warning, no error, only a lower spend-avoided figure in ``--stats``,
because the orchestrator did the whole job alone instead of offloading grunt sub-tasks.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tanglebrain.cli import run_once, run_once_stream
from tanglebrain.selector import build_adapter as _real_build_adapter

# An orchestrator-capable CLI entry carrying delegate_args, plus a local backend for it to
# offload to, plus a non-orchestrator sub to prove the flag tracks the entry rather than the
# code path.
_ROSTER_YAML = """\
- id: local-ollama
  tier: local
  invoke: {kind: openai-compat, base_url: "http://localhost:11434/v1", model: "llama3.2"}
  cost: free
  good_at: [grunt]
- id: claude
  tier: sub
  invoke:
    kind: cli
    cmd: ["claude"]
    parse: claude-json
    delegate_args: ["--mcp-config", "{delegate_mcp_json}"]
  cost: subscription
  good_at: [reasoning]
  can_orchestrate: true
- id: plain-sub
  tier: sub
  invoke: {kind: cli, cmd: ["plain"], parse: plain}
  cost: subscription
  good_at: [writing]
"""


def _roster(test: unittest.TestCase) -> str:
    """Write the fixture roster to a temp file and return its path (auto-cleaned).

    Args:
        test: The test case, used to register cleanup.

    Returns:
        Path to the temporary roster YAML.
    """
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(_ROSTER_YAML)
    handle.close()
    test.addCleanup(os.unlink, handle.name)
    return handle.name


class ModelPinDelegationTest(unittest.TestCase):
    """``--model`` on a ``can_orchestrate`` entry keeps the delegate tool."""

    def setUp(self) -> None:
        """Keep metering side-effects out of the real cache directory."""
        self.tmp = tempfile.mkdtemp()
        env = patch.dict(os.environ, {"TANGLEBRAIN_STATE_DIR": self.tmp}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def _delegation_of(self, model: str, stream: bool = False) -> bool:
        """Return whether the adapter actually built for ``model`` carries the delegate tool.

        Runs the real ``build_adapter`` and inspects the adapter it produces, rather than the
        argument the CLI passed. The rule lives inside ``build_adapter``, so asserting the
        argument would pin the mechanism instead of the behaviour.

        Args:
            model: Roster id to pin with ``--model``.
            stream: Exercise ``run_once_stream`` instead of ``run_once``.

        Returns:
            The built adapter's effective ``inject_delegate``.
        """
        seen: dict[str, bool] = {}

        def spy(entry, **kwargs):
            built = _real_build_adapter(entry, **kwargs)
            seen["injected"] = getattr(built, "inject_delegate", False)
            stub = MagicMock(spec=["run"])
            stub.run.return_value = "reply"
            return stub

        with patch("tanglebrain.cli.build_adapter", side_effect=spy):
            if stream:
                deltas, _served = run_once_stream("hello", model=model, roster_path=_roster(self))
                # Drain the delta iterator: listing the returned 2-tuple would never enter the
                # streaming branch, so the assertion would pass without reaching the subject.
                self.assertEqual("".join(deltas), "reply")
            else:
                run_once("hello", model=model, roster_path=_roster(self))
        return seen["injected"]

    def test_pinned_orchestrator_gets_the_delegate_tool(self) -> None:
        """The regression: the pinned orchestrator must end up holding delegation."""
        self.assertTrue(
            self._delegation_of("claude"),
            "a pinned can_orchestrate entry was built without its delegate tool",
        )

    def test_pinned_non_orchestrator_does_not_get_it(self) -> None:
        """Delegation tracks the entry's own flag, not merely the pinned code path."""
        self.assertFalse(self._delegation_of("plain-sub"))

    def test_streaming_path_matches(self) -> None:
        """``run_once_stream`` carried the identical defect and must behave identically."""
        self.assertTrue(
            self._delegation_of("claude", stream=True),
            "the streaming path built a pinned orchestrator without its delegate tool",
        )

    def test_delegate_args_reach_the_command(self) -> None:
        """End-to-end: the injected flags actually land on the argv the CLI runs.

        Asserting the ``inject_delegate`` argument alone would pass even if the adapter
        ignored it, so this exercises the real :class:`CliAdapter` and inspects the command.
        """
        from tanglebrain.roster import load_roster
        from tanglebrain.selector import build_adapter, select_by_id

        entry = select_by_id(load_roster(_roster(self)), "claude")
        with_delegate = build_adapter(entry, inject_delegate=True)._effective_cmd()
        without = build_adapter(entry, inject_delegate=False)._effective_cmd()
        self.assertIn("--mcp-config", with_delegate)
        self.assertNotIn("--mcp-config", without)


if __name__ == "__main__":
    unittest.main()


class DelegationIsDerivedFromTheEntryTest(unittest.TestCase):
    """Delegation is a property of the entry, resolved once inside ``build_adapter``.

    The rule was previously restated at each call site, so a site that omitted the argument got
    the default rather than the right answer. These tests pin the derivation itself, which is
    what stops the defect class returning at the next new call site.
    """

    def test_orchestrator_gets_it_without_the_caller_asking(self) -> None:
        """Omitting the argument must yield the correct answer, not the old default."""
        from tanglebrain.roster import Invoke, RosterEntry
        from tanglebrain.selector import build_adapter

        entry = RosterEntry(
            id="claude", tier="sub",
            invoke=Invoke(kind="cli", cmd=["claude"], delegate_args=["--mcp-config", "x"]),
            can_orchestrate=True,
        )
        self.assertTrue(build_adapter(entry).inject_delegate)

    def test_paid_cli_entry_reached_by_failover_does_not_get_it(self) -> None:
        """The live case the per-call-site rule missed.

        The router's last-resort paid loop iterates ``in_tier("api")``, which is not the
        orchestrator rotation. A paid entry invoked as a CLI, carrying ``delegate_args`` but not
        flagged ``can_orchestrate``, must not receive the delegate tool merely by being reached
        after every orchestrator failed.
        """
        from tanglebrain.roster import Invoke, RosterEntry
        from tanglebrain.selector import build_adapter

        entry = RosterEntry(
            id="paid-cli", tier="sub",
            invoke=Invoke(kind="cli", cmd=["paid"], delegate_args=["--mcp-config", "x"]),
            can_orchestrate=False,
        )
        adapter = build_adapter(entry)
        self.assertFalse(adapter.inject_delegate)
        self.assertNotIn("--mcp-config", adapter._effective_cmd())

    def test_explicit_false_still_overrides(self) -> None:
        """The delegate path passes False explicitly to stop a sub-call recursing."""
        from tanglebrain.roster import Invoke, RosterEntry
        from tanglebrain.selector import build_adapter

        entry = RosterEntry(
            id="claude", tier="sub",
            invoke=Invoke(kind="cli", cmd=["claude"], delegate_args=["--mcp-config", "x"]),
            can_orchestrate=True,
        )
        self.assertFalse(build_adapter(entry, inject_delegate=False).inject_delegate)
