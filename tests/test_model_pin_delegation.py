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

    def test_pinned_orchestrator_gets_the_delegate_tool(self) -> None:
        """The regression: the pinned orchestrator must be built with delegation on."""
        adapter = MagicMock()
        adapter.run.return_value = "reply"
        with patch("tanglebrain.cli.build_adapter", return_value=adapter) as build:
            run_once("hello", model="claude", roster_path=_roster(self))
        self.assertTrue(
            build.call_args.kwargs.get("inject_delegate"),
            "a pinned can_orchestrate entry was built without its delegate tool",
        )

    def test_pinned_non_orchestrator_does_not_get_it(self) -> None:
        """Delegation tracks the entry's own flag, not merely the pinned code path."""
        adapter = MagicMock()
        adapter.run.return_value = "reply"
        with patch("tanglebrain.cli.build_adapter", return_value=adapter) as build:
            run_once("hello", model="plain-sub", roster_path=_roster(self))
        self.assertFalse(build.call_args.kwargs.get("inject_delegate"))

    def test_streaming_path_matches(self) -> None:
        """``run_once_stream`` carried the identical defect and must behave identically."""
        adapter = MagicMock(spec=["run"])
        adapter.run.return_value = "reply"
        with patch("tanglebrain.cli.build_adapter", return_value=adapter) as build:
            list(run_once_stream("hello", model="claude", roster_path=_roster(self)))
        self.assertTrue(
            build.call_args.kwargs.get("inject_delegate"),
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
