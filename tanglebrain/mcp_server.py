"""MCP server exposing TangleBrain's delegate tools to an orchestrator.

A stdio MCP server an orchestrator (e.g. claude / codex / gemini) registers so it can offload
sub-tasks to a configured backend mid-task — the mechanism that makes frontier-first decompose
actually offload work rather than running everything on the orchestrator itself. It exposes three
tools: ``delegate_local`` (free local default), ``delegate`` (route to any configured
``can_delegate`` target by id), and ``delegate_targets`` (the configured target menu).

It is a **thin wrapper** over :mod:`tanglebrain.delegate` (which reuses the roster + selector +
adapters): the routing logic lives there, MCP plumbing lives here. The tools are **sync** — FastMCP
runs sync tools in a worker thread, so they can call the sync adapter directly without duplicating
the HTTP call as async.

The ``delegate`` tool's description enumerates the configured targets; it is built **once at server
startup** from the roster, so a roster edit is reflected on the next server launch (orchestrators
spawn the server per session). The live menu is always available via the ``delegate_targets`` tool.

Threat model: the server performs **no authentication** — any process that can reach its stdio is
trusted to delegate unlimited prompts to the local model. That matches the intended use (a local
orchestrator CLI launches it as a child); do not expose it beyond the launching CLI.

Requires the optional ``mcp`` dependency: ``pip install "tanglebrain[delegate]"``.

Run it directly for a manual smoke test::

    tanglebrain-delegate        # serves over stdio

or register it with an orchestrator CLI (flag shapes vary by version — see the README)::

    claude mcp add tanglebrain-delegate -- tanglebrain-delegate
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from tanglebrain.delegate import (
    DEFAULT_DELEGATE_MAX_TOKENS,
    _render_target_menu,
    delegate_targets as _list_delegate_targets,
    run_delegate,
    run_local_delegate,
)

mcp = FastMCP("tanglebrain-delegate")


def _delegate_tool_description() -> str:
    """Build the ``delegate`` tool description, enumerating the configured targets from the roster.

    Best-effort: if the roster can't be loaded at startup the menu is replaced with a short note
    (the server still starts; ``delegate_targets`` / ``delegate`` surface the real error on call).

    Returns:
        The full tool-description string handed to ``@mcp.tool(description=...)``.
    """
    header = (
        "Delegate a self-contained sub-task to a CONFIGURED backend and return its text.\n\n"
        "Use this to route a sub-task to a specific cheaper or better-fit backend, instead of the "
        "default free local model. Pass `target` = one of the configured target ids listed below; "
        "omit `target` (or use the delegate_local tool) to use the free local model. Pick a target "
        "by its `good_at` fit. Hand the result back for review rather than trusting it blind.\n\n"
        "Configured delegate targets:\n"
    )
    try:
        menu = _render_target_menu(_list_delegate_targets())
    except Exception as exc:  # roster unreadable at startup — keep the server usable
        menu = f"  (could not load the target menu: {exc}; call delegate_targets to retry)"
    return header + menu


@mcp.tool()
def delegate_local(prompt: str, max_tokens: int = DEFAULT_DELEGATE_MAX_TOKENS) -> str:
    """Delegate a self-contained sub-task to TangleBrain's free local model (gpt-oss-120b).

    Use this whenever you (the orchestrator) would otherwise spend your own rate-limited
    tokens on bulk work that doesn't need your full capability: code generation, refactoring,
    drafting, extraction, transformation, summarization, boilerplate, test writing. It runs on
    a local 120B model at **$0 marginal cost** and unlimited throughput, so offload freely and
    keep your own budget for decomposition and review.

    Hand the result back for review rather than trusting it blind — you decide whether to accept,
    re-delegate with a tighter prompt, or do it yourself.

    On failure (endpoint down, bad config, timeout) this raises and you see the error — there is
    no transparent retry or model swap here; you decide what to do next.

    Args:
        prompt: The self-contained sub-task to delegate. Give it everything it needs — the local
            model has no access to your conversation context.
        max_tokens: Completion token cap (default 2048 — the local model needs headroom for its
            internal reasoning before emitting the final answer).

    Returns:
        The local model's final response text.
    """
    return run_local_delegate(prompt, max_tokens=max_tokens)


# NB: the description is evaluated at import time (decorator argument), so importing this module
# reads the roster once to build the target menu — intentional ("built once at server startup"),
# and guarded inside _delegate_tool_description so an unreadable roster can't crash import.
@mcp.tool(description=_delegate_tool_description())
def delegate(
    prompt: str,
    target: str | None = None,
    max_tokens: int = DEFAULT_DELEGATE_MAX_TOKENS,
) -> str:
    """Delegate a sub-task to a configured backend (see the tool description for the target menu).

    The generalized delegate: ``target`` is the id of a configured ``can_delegate`` backend, or
    omitted/``None`` to use the free local model (same as the ``delegate_local`` tool). Targeting a
    paid (``api``) backend stays gated behind the operator's billing flag — it raises if billing is
    off rather than spending silently. The menu in this tool's description is from server startup;
    call ``delegate_targets`` for the live menu. The dynamic description is the authoritative target
    list handed to the model.

    Args:
        prompt: The self-contained sub-task to delegate. Give it everything it needs — the target
            backend has no access to your conversation context.
        target: The id of a configured ``can_delegate`` backend, or ``None`` for the free local
            model. Must be one of the ids in the target menu.
        max_tokens: Completion token cap (default 2048 — a local reasoning model needs headroom for
            its internal reasoning before emitting the final answer).

    Returns:
        The target backend's final response text.
    """
    return run_delegate(prompt, target=target, max_tokens=max_tokens)


@mcp.tool()
def delegate_targets() -> str:
    """List the configured delegate targets (the live menu) as a JSON array.

    Call this to see which backends you may delegate to and what each is good at, then pass a chosen
    id as ``delegate``'s ``target``. Each element is
    ``{"id", "tier", "good_at", "cost", "kind"}``; the array is empty when no ``can_delegate`` target
    is configured (only the default local model is then available, via ``delegate_local``). Emits no
    credentials. Reads the roster live, so it reflects edits since server startup.

    Returns:
        A JSON-encoded array of target descriptors.
    """
    return json.dumps(_list_delegate_targets())


def main() -> None:
    """Console entry point: serve the delegate over stdio (``tanglebrain-delegate``)."""
    mcp.run()


if __name__ == "__main__":
    main()
