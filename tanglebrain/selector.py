"""C1 routing stub — local-first selection.

**This is NOT the cost-tiered router.** The real router (plan §6) — frontier-first decompose,
task-fit orchestrator selection, multi-orchestrator rotation, 429/limit failover — is C3.

C1's job is only to prove one request routes end-to-end: pick the free local entry, build its
adapter, run the prompt. The logic here is deliberately minimal; do not grow it into the §6
router — that belongs in its own module so the two don't get conflated.
"""
from __future__ import annotations

from tanglebrain.adapters import AdapterError, OpenAICompatAdapter
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


def build_adapter(entry: RosterEntry) -> Adapter:
    """Build the adapter for a roster entry.

    C1 supports only the ``openai-compat`` (free local) adapter. The subscription-CLI and
    paid-API adapters land in C2; selecting an entry that needs one raises clearly rather than
    pretending it is routable.

    Args:
        entry: The roster entry to build an adapter for.

    Returns:
        An :class:`~tanglebrain.adapters.base.Adapter` for the entry.

    Raises:
        AdapterError: If the entry's invoke kind has no adapter yet (``cli`` / ``api`` → C2).
    """
    if entry.invoke.kind == "openai-compat":
        return OpenAICompatAdapter.from_entry(entry)
    raise AdapterError(
        f"no adapter for invoke.kind {entry.invoke.kind!r} yet "
        f"(entry {entry.id!r}); CLI/API adapters land in C2"
    )
