"""C1 routing stub — local-first selection.

**This is NOT the cost-tiered router.** The real router (plan §6) — frontier-first decompose,
task-fit orchestrator selection, multi-orchestrator rotation, 429/limit failover — is C3.

C1's job is only to prove one request routes end-to-end: pick the free local entry, build its
adapter, run the prompt. The logic here is deliberately minimal; do not grow it into the §6
router — that belongs in its own module so the two don't get conflated.
"""
from __future__ import annotations

from tanglebrain.adapters import AdapterError, CliAdapter, OpenAICompatAdapter
from tanglebrain.adapters.base import Adapter
from tanglebrain.roster import Roster, RosterEntry


class SelectionError(RuntimeError):
    """Raised when no suitable roster entry can be selected for a request."""


def select_local(roster: Roster) -> RosterEntry:
    """Select the free local entry to route to (C1's only routing decision).

    Returns the first ``local``-tier entry invoked via ``openai-compat`` — the free tier the
    C1 adapter can actually call.

    Args:
        roster: The loaded roster.

    Returns:
        The selected local :class:`~tanglebrain.roster.RosterEntry`.

    Raises:
        SelectionError: If the roster has no invocable local entry.
    """
    for entry in roster:
        if entry.tier == "local" and entry.invoke.kind == "openai-compat":
            return entry
    raise SelectionError(
        "no invocable local entry in roster (need tier=local, invoke.kind=openai-compat)"
    )


def select_by_id(roster: Roster, entry_id: str) -> RosterEntry:
    """Select a roster entry by id (lets the CLI drive a named sub end-to-end).

    This is **not** the §6 router — it makes no routing decision, it just resolves an explicit
    id the caller named. The cost-tiered orchestrator selection / rotation / failover is C3.

    Args:
        roster: The loaded roster.
        entry_id: The id to select.

    Returns:
        The matching :class:`~tanglebrain.roster.RosterEntry`.

    Raises:
        SelectionError: If no entry has that id.
    """
    try:
        return roster.by_id(entry_id)
    except KeyError:
        known = ", ".join(e.id for e in roster) or "(empty roster)"
        raise SelectionError(f"no roster entry with id {entry_id!r}; known ids: {known}")


def build_adapter(entry: RosterEntry, inject_delegate: bool = False) -> Adapter:
    """Build the adapter for a roster entry.

    C2 supports the ``openai-compat`` (free local) and ``cli`` (subscription) adapters. The
    paid-API adapter is gated behind ``api_billing_enabled`` and lands later (issue #2);
    selecting an ``api`` entry raises clearly rather than pretending it is routable.

    Args:
        entry: The roster entry to build an adapter for.
        inject_delegate: For ``cli`` entries, make the gpt-oss MCP delegate available to the CLI as
            an orchestrator (C3b) — the router sets this so an orchestrator can offload grunt to
            free local. Ignored for non-``cli`` kinds.

    Returns:
        An :class:`~tanglebrain.adapters.base.Adapter` for the entry.

    Raises:
        AdapterError: If the entry's invoke kind has no adapter yet (``api`` → issue #2).
    """
    if entry.invoke.kind == "openai-compat":
        return OpenAICompatAdapter.from_entry(entry)
    if entry.invoke.kind == "cli":
        return CliAdapter.from_entry(entry, inject_delegate=inject_delegate)
    raise AdapterError(
        f"no adapter for invoke.kind {entry.invoke.kind!r} yet "
        f"(entry {entry.id!r}); the paid-API tier lands later (issue #2)"
    )
