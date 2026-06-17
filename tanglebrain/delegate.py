"""Local-tier delegation logic (C2b) — the grunt offload behind the MCP tool.

A frontier orchestrator (claude / codex / gemini) decomposes a task and hands the bulk
sub-tasks to TangleBrain's **free local tier** (gpt-oss-120b) at $0 marginal cost, then reviews
the result. This module is the routing half of that: take a prompt, route it to the roster's
local entry, return the text.

It is deliberately **free of any MCP dependency** so the delegation logic is importable and
hermetically testable without the `mcp` SDK installed — the MCP server in
:mod:`tanglebrain.mcp_server` is a thin wrapper over :func:`run_local_delegate`.

It **reuses** C1's roster + selector + ``OpenAICompatAdapter`` rather than re-implementing the
LiteLLM call, so the endpoint, model, and key live in exactly one place (the roster). Failures
surface to the caller — the orchestrator decides whether to retry, fall back, or surface (no
transparent retry/swap here), matching the adapter contract and the C0 reference.
"""
from __future__ import annotations

import json
import sys

from tanglebrain.roster import ROSTER_ENV_VAR, load_roster
from tanglebrain.selector import build_adapter, select_local

#: gpt-oss spends part of its budget on internal reasoning before the final answer, so the
#: delegate is generous by default (the C0 budget lesson). Matches the adapter's own default.
DEFAULT_DELEGATE_MAX_TOKENS = 2048

#: The MCP server name an orchestrator registers the delegate under. The tool an orchestrator
#: calls is then ``mcp__<DELEGATE_SERVER_NAME>__delegate_local``.
DELEGATE_SERVER_NAME = "tanglebrain-delegate"


def delegate_mcp_config_json() -> str:
    """Return the MCP-server config JSON that exposes the delegate to an orchestrator (C3b).

    Launches the server as ``<python> -m tanglebrain.mcp_server`` (not the ``tanglebrain-delegate``
    console script) so it resolves regardless of whether the script is on the orchestrator's PATH
    — the current interpreter is always reachable. This is the shape claude's ``--mcp-config``
    accepts; other CLIs reference the same server by name (see each entry's ``delegate_args``).

    Returns:
        A JSON string: ``{"mcpServers": {"tanglebrain-delegate": {"command": ..., "args": ...}}}``.
    """
    return json.dumps(
        {
            "mcpServers": {
                DELEGATE_SERVER_NAME: {
                    "command": sys.executable,
                    "args": ["-m", "tanglebrain.mcp_server"],
                }
            }
        }
    )


def delegate_substitutions() -> dict[str, str]:
    """Return the token→value map applied to a roster entry's ``delegate_args`` (C3b).

    Tokens (so per-CLI flags stay config-driven in the roster, not hardcoded in adapters):

    - ``{delegate_mcp_json}`` → the full MCP-server JSON (:func:`delegate_mcp_config_json`), for
      CLIs that take a config blob (claude's ``--mcp-config``).
    - ``{delegate_mcp_command}`` → the interpreter that launches the server (``sys.executable``),
      for CLIs configured field-by-field (codex's ``-c mcp_servers...`` overrides).

    Returns:
        A mapping of literal token to replacement string.
    """
    return {
        "{delegate_mcp_json}": delegate_mcp_config_json(),
        "{delegate_mcp_command}": sys.executable,
    }

# ``ROSTER_ENV_VAR`` ("TANGLEBRAIN_ROSTER") is re-exported from :mod:`tanglebrain.roster`, where the
# whole resolution order (env → ~/.config/tanglebrain/roster.yaml → packaged) now lives. An MCP
# client can still set it to point the delegate server at a non-default roster.


def run_local_delegate(
    prompt: str,
    max_tokens: int = DEFAULT_DELEGATE_MAX_TOKENS,
    roster_path: str | None = None,
) -> str:
    """Route ``prompt`` to the roster's free local tier and return its final text.

    Args:
        prompt: The self-contained sub-task to delegate to the local grunt model.
        max_tokens: Completion token cap (default 2048 — gpt-oss needs headroom for its
            internal reasoning before the final answer).
        roster_path: Optional roster YAML path. When ``None``, the roster is resolved by
            :func:`tanglebrain.roster.default_roster_path` (``TANGLEBRAIN_ROSTER`` env →
            ``~/.config/tanglebrain/roster.yaml`` → the packaged generic example).

    Returns:
        The local tier's final response text.

    Raises:
        RosterError: If the roster cannot be loaded.
        SelectionError: If the roster has no invocable local entry.
        AdapterError: If the local adapter cannot produce text.
    """
    roster = load_roster(roster_path)
    entry = select_local(roster)
    adapter = build_adapter(entry)
    return adapter.run(prompt, {"max_tokens": max_tokens})
