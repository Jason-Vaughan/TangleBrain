"""MCP server exposing TangleBrain's free local tier as a delegate tool (C2b).

This is the "generalized gpt-oss MCP tool from C0" (plan §10): a stdio MCP server a frontier
orchestrator (claude / codex / gemini) registers so it can offload grunt work to free local
gpt-oss-120b mid-task — the mechanism that makes frontier-first decompose (§6) actually save
money instead of burning a subscription's rate limit on the whole task.

It is a **thin wrapper** over :func:`tanglebrain.delegate.run_local_delegate` (which reuses C1's
roster + ``OpenAICompatAdapter``): the routing logic lives there, MCP plumbing lives here. The
tool is **sync** — FastMCP runs sync tools in a worker thread, so it can call the sync adapter
directly without duplicating the HTTP call as async.

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

from mcp.server.fastmcp import FastMCP

from tanglebrain.delegate import DEFAULT_DELEGATE_MAX_TOKENS, run_local_delegate

mcp = FastMCP("tanglebrain-delegate")


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


def main() -> None:
    """Console entry point: serve the delegate over stdio (``tanglebrain-delegate``)."""
    mcp.run()


if __name__ == "__main__":
    main()
