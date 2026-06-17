"""C1 routing stub — local-first selection.

**This is NOT the cost-tiered router.** The real router (plan §6) — frontier-first decompose,
task-fit orchestrator selection, multi-orchestrator rotation, 429/limit failover — is C3.

C1's job is only to prove one request routes end-to-end: pick the free local entry, build its
adapter, run the prompt. The logic here is deliberately minimal; do not grow it into the §6
router — that belongs in its own module so the two don't get conflated.
"""
from __future__ import annotations

from tanglebrain.adapters import ApiAdapter, AdapterError, CliAdapter, OpenAICompatAdapter
from tanglebrain.adapters.base import Adapter
from tanglebrain.roster import Roster, RosterEntry
from tanglebrain.settings import Settings, load_settings


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


def build_adapter(
    entry: RosterEntry,
    inject_delegate: bool = False,
    settings: Settings | None = None,
) -> Adapter:
    """Build the adapter for a roster entry.

    Supports the ``openai-compat`` (free local), ``cli`` (subscription), and ``api`` (paid,
    LiteLLM-fronted) adapters. The paid tier is **gated**: an ``api`` entry only builds when the
    global ``api_billing_enabled`` flag is on *and* the entry's own ``enabled`` flag is on — the
    durable "no paid billing without the explicit toggle" rule (plan §9.6, issue #2). Otherwise it
    raises clearly rather than pretending the entry is routable, so a ``tier: api`` entry parses but
    stays inert by default.

    Args:
        entry: The roster entry to build an adapter for.
        inject_delegate: For ``cli`` entries, make the gpt-oss MCP delegate available to the CLI as
            an orchestrator (C3b) — the router sets this so an orchestrator can offload grunt to
            free local. Ignored for non-``cli`` kinds.
        settings: Global settings carrying the billing gate. Loaded from the packaged
            ``config/settings.yaml`` when ``None`` (and only when an ``api`` entry is actually being
            built, so non-paid builds never touch the file). Injectable for tests.

    Returns:
        An :class:`~tanglebrain.adapters.base.Adapter` for the entry.

    Raises:
        AdapterError: If an ``api`` entry is selected while billing is gated off or the entry is
            disabled, or if the invoke kind is unknown.
    """
    if entry.invoke.kind == "openai-compat":
        return OpenAICompatAdapter.from_entry(entry)
    if entry.invoke.kind == "cli":
        return CliAdapter.from_entry(entry, inject_delegate=inject_delegate)
    if entry.invoke.kind == "api":
        if settings is None:
            settings = load_settings()
        if not settings.api_billing_enabled:
            raise AdapterError(
                f"entry {entry.id!r} is a paid-API tier but billing is disabled "
                "(api_billing_enabled=false in config/settings.yaml); it is inert (issue #2)"
            )
        if not entry.enabled:
            raise AdapterError(
                f"paid-API entry {entry.id!r} is disabled (enabled=false); not routable"
            )
        return ApiAdapter.from_entry(entry)
    raise AdapterError(
        f"no adapter for invoke.kind {entry.invoke.kind!r} (entry {entry.id!r})"
    )
