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

import os

from tanglebrain.roster import load_roster
from tanglebrain.selector import build_adapter, select_local

#: gpt-oss spends part of its budget on internal reasoning before the final answer, so the
#: delegate is generous by default (the C0 budget lesson). Matches the adapter's own default.
DEFAULT_DELEGATE_MAX_TOKENS = 2048

#: Env var an MCP client can set to point the server at a non-default roster. The server runs as
#: a subprocess launched by the orchestrator CLI, so it must be able to locate the roster; when
#: unset, the packaged ``tanglebrain/config/roster.yaml`` is used.
ROSTER_ENV_VAR = "TANGLEBRAIN_ROSTER"


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
        roster_path: Optional roster YAML path. When ``None``, falls back to the
            ``TANGLEBRAIN_ROSTER`` env var, then the packaged default roster.

    Returns:
        The local tier's final response text.

    Raises:
        RosterError: If the roster cannot be loaded.
        SelectionError: If the roster has no invocable local entry.
        AdapterError: If the local adapter cannot produce text.
    """
    path = roster_path or os.environ.get(ROSTER_ENV_VAR)
    roster = load_roster(path)
    entry = select_local(roster)
    adapter = build_adapter(entry)
    return adapter.run(prompt, {"max_tokens": max_tokens})
