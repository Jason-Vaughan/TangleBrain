"""Delegation logic — the sub-task offload behind the MCP tools.

A frontier orchestrator (e.g. claude / codex / gemini) decomposes a task and hands sub-tasks to a
**configured** backend, then reviews the result. The default target is TangleBrain's **free local
tier** at $0 marginal cost (:func:`run_local_delegate` / ``target=None``); the orchestrator may also
target any roster entry flagged ``can_delegate`` (:func:`run_delegate`) — a cheaper sub or a
better-fit backend you've configured. This module is the routing half of that: take a prompt, resolve
the target entry, route to it, return the text. The configured menu is exposed via
:func:`delegate_targets`.

It is deliberately **free of any MCP dependency** so the delegation logic is importable and
hermetically testable without the `mcp` SDK installed — the MCP server in
:mod:`tanglebrain.mcp_server` is a thin wrapper over these functions.

It **reuses** the roster + selector + ``OpenAICompatAdapter`` rather than re-implementing the call,
so the endpoint, model, and key live in exactly one place (the roster). Failures surface to the
caller — the orchestrator decides whether to retry, fall back, or surface (no transparent retry/swap
here), matching the adapter contract.
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from tanglebrain.roster import ROSTER_ENV_VAR, Roster, RosterEntry, load_roster
from tanglebrain.selector import SelectionError, build_adapter, select_local
from tanglebrain.settings import Settings, load_settings

#: A local reasoning model spends part of its budget on internal reasoning before the final answer,
#: so the delegate's token cap is generous by default. Matches the adapter's own default.
DEFAULT_DELEGATE_MAX_TOKENS = 2048

#: The MCP server name an orchestrator registers the delegate under. The tool an orchestrator
#: calls is then ``mcp__<DELEGATE_SERVER_NAME>__delegate_local``.
DELEGATE_SERVER_NAME = "tanglebrain-delegate"

#: Cost ordering for capability-routed delegation: cheapest tier first. ``api`` is deliberately
#: ABSENT — paid backends are never auto-selected by capability (the ratified "paid is last resort,
#: never preferred" invariant; the request-level router likewise never task-fits to ``api``). An
#: ``api`` backend stays reachable only via an explicit ``target=<id>`` through the billing gate.
TIER_RANK = {"local": 0, "sub": 1}


class NoDelegateFit(RuntimeError):
    """Signal that no delegate target fits a requested capability — *not* a failure.

    Raised by :func:`run_delegate` when ``task=`` is given but no ``can_delegate`` target's
    ``good_at`` matches (``api`` targets excluded — see :data:`TIER_RANK`). It is a **routing
    signal**, deliberately NOT a :class:`~tanglebrain.selector.SelectionError`: it means TangleBrain
    correctly found no cheaper/better-fit backend, so the frontier orchestrator should handle the
    sub-task itself (it is the most capable backend available). The MCP ``delegate`` tool catches it
    and returns that instruction to the orchestrator rather than surfacing an error.
    """


def delegate_mcp_config_json() -> str:
    """Return the MCP-server config JSON that exposes the delegate to an orchestrator.

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
    """Return the token→value map applied to a roster entry's ``delegate_args``.

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
        prompt: The self-contained sub-task to delegate to the local backend.
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
    return run_delegate(prompt, target=None, max_tokens=max_tokens, roster_path=roster_path)


def _resolve_target(roster: Roster, target: str) -> RosterEntry:
    """Resolve a named delegate target, enforcing the ``can_delegate`` opt-in.

    An orchestrator may only delegate to entries explicitly flagged ``can_delegate`` — this stops it
    from invoking arbitrary roster entries (e.g. another orchestrator, or an undeclared paid key) by
    naming an id. The free local default target is reached via ``target=None`` (see
    :func:`run_delegate`) and is intentionally **not** resolved here.

    Args:
        roster: The loaded roster.
        target: The roster id the orchestrator asked to delegate to.

    Returns:
        The resolved, delegate-eligible :class:`~tanglebrain.roster.RosterEntry`.

    Raises:
        SelectionError: If no entry has that id, or the entry exists but is not a delegate target.
    """
    try:
        entry = roster.by_id(target)
    except KeyError:
        configured = ", ".join(e.id for e in roster.delegate_targets()) or "(none configured)"
        raise SelectionError(
            f"unknown delegate target {target!r}; configured targets: {configured}"
        )
    if not entry.can_delegate:
        configured = ", ".join(e.id for e in roster.delegate_targets()) or "(none configured)"
        raise SelectionError(
            f"entry {target!r} is not a delegate target (set can_delegate: true to allow it); "
            f"configured targets: {configured}"
        )
    return entry


def available_capabilities(roster: Roster) -> list[str]:
    """Return the sorted unique ``good_at`` tags across the capability-routable delegate targets.

    Only ``local`` + ``sub`` ``can_delegate`` targets are considered (``api`` is never
    capability-routed — see :data:`TIER_RANK`). Used for the no-fit message and the tool description.

    Args:
        roster: The loaded roster.

    Returns:
        The sorted, de-duplicated capability tags an orchestrator may route to by ``task``.
    """
    caps: set[str] = set()
    for entry in roster.delegate_targets():
        if entry.tier in TIER_RANK:
            caps.update(entry.good_at)
    return sorted(caps)


def _select_by_capability(roster: Roster, task: str) -> RosterEntry:
    """Select the cheapest ``can_delegate`` target whose ``good_at`` contains ``task``.

    Mirrors the request-level router's task-fit at the sub-task level, but as a deterministic
    selection (not rotation): among the ``can_delegate`` targets whose ``good_at`` lists ``task``,
    pick the cheapest by :data:`TIER_RANK` (``local`` before ``sub``), ties broken by declared roster
    order (``min`` is stable). ``api`` targets are excluded entirely — paid is never auto-selected.

    Args:
        roster: The loaded roster.
        task: The capability tag the orchestrator needs (a ``good_at`` value, e.g. ``code``).

    Returns:
        The selected :class:`~tanglebrain.roster.RosterEntry`.

    Raises:
        NoDelegateFit: If no eligible target advertises ``task`` — the orchestrator should handle the
            sub-task itself.
    """
    candidates = [
        e for e in roster.delegate_targets() if e.tier in TIER_RANK and task in e.good_at
    ]
    if not candidates:
        available = ", ".join(available_capabilities(roster)) or "(none configured)"
        raise NoDelegateFit(
            f"no delegate target is good_at {task!r}; available capabilities: {available}"
        )
    return min(candidates, key=lambda e: TIER_RANK[e.tier])


def run_delegate(
    prompt: str,
    target: str | None = None,
    task: str | None = None,
    max_tokens: int = DEFAULT_DELEGATE_MAX_TOKENS,
    roster_path: str | None = None,
) -> str:
    """Route ``prompt`` to a configured delegate target and return its final text.

    Selection precedence:

    1. ``target`` (explicit id) — routes to that ``can_delegate`` entry (see :func:`_resolve_target`).
       Wins when both ``target`` and ``task`` are given (an explicit id is the most specific request).
    2. ``task`` (capability) — TangleBrain picks the cheapest ``can_delegate`` target whose
       ``good_at`` contains ``task`` (see :func:`_select_by_capability`); ``api`` targets are never
       auto-selected. No fit raises :class:`NoDelegateFit` (the orchestrator handles it itself).
    3. neither — the free local default tier (same as :func:`run_local_delegate`).

    The selected target is built as a **leaf** (``inject_delegate=False``) — a delegate target never
    receives its own delegate tool, so there is no recursive delegation. ``api`` targets (reachable
    only via explicit ``target``) flow through the existing billing gate in
    :func:`tanglebrain.selector.build_adapter`; ``cli`` targets keep their env-scrub. Non-local
    delegate spend is **not metered** in this version (consistent with the existing delegate posture).

    Args:
        prompt: The self-contained sub-task to delegate. Give it everything it needs — the target
            backend has no access to the orchestrator's conversation context.
        target: The roster id of a ``can_delegate`` backend (explicit). Takes precedence over ``task``.
        task: A capability tag (a ``good_at`` value) to route by fit when no explicit ``target`` is
            given. ``None`` with no ``target`` uses the free local default.
        max_tokens: Completion token cap (default 2048 — a local reasoning model needs headroom for
            its internal reasoning before the final answer).
        roster_path: Optional roster YAML path. When ``None``, the roster is resolved by
            :func:`tanglebrain.roster.default_roster_path` (``TANGLEBRAIN_ROSTER`` env →
            ``~/.config/tanglebrain/roster.yaml`` → the packaged generic example).

    Returns:
        The target backend's final response text.

    Raises:
        RosterError: If the roster cannot be loaded.
        SelectionError: If ``target`` is unknown, not a delegate target, or (for the local default)
            the roster has no invocable local entry.
        NoDelegateFit: If ``task`` is given (and no ``target``) but no eligible target fits it.
        AdapterError: If the target is an ``api`` entry while billing is gated off / disabled, or
            the target's adapter cannot produce text.
    """
    roster = load_roster(roster_path)
    if target is not None:
        entry = _resolve_target(roster, target)
    elif task is not None:
        entry = _select_by_capability(roster, task)
    else:
        entry = select_local(roster)
    adapter = build_adapter(entry, inject_delegate=False)
    return adapter.run(prompt, {"max_tokens": max_tokens})


#: Concurrency cap to use only when :func:`os.cpu_count` can't report a core count (rare).
DEFAULT_CONCURRENCY_FALLBACK = 4


def _default_concurrency() -> int:
    """Derive the default fan-out concurrency from this machine's core count.

    The true limit on concurrent delegation is the *backend's* parallelism (e.g. a local model
    server's ``OLLAMA_NUM_PARALLEL``), which TangleBrain can't portably introspect — so the default
    is a system-derived proxy that scales with the machine. An operator who knows their backend pins
    the real number via ``settings.delegate_max_concurrency``.

    Returns:
        ``os.cpu_count()``, or :data:`DEFAULT_CONCURRENCY_FALLBACK` if the OS won't report it.
    """
    return os.cpu_count() or DEFAULT_CONCURRENCY_FALLBACK


def _effective_concurrency(settings: Settings, max_concurrency: int | None) -> int:
    """Resolve the concurrency cap: operator setting (or derived) default, optionally lowered.

    Args:
        settings: Global settings; ``delegate_max_concurrency`` pins the cap when set.
        max_concurrency: Optional per-call override that may **lower** the cap, never raise it.

    Returns:
        The number of workers to use — at least 1.
    """
    base = settings.delegate_max_concurrency or _default_concurrency()
    if max_concurrency is not None:
        base = min(base, max_concurrency)
    return max(1, base)


def _run_one_of_many(item: object, index: int, roster_path: str | None) -> dict:
    """Run a single ``delegate_many`` item and map its outcome to a per-item result dict.

    Never raises — every failure mode becomes a result with a ``status`` so one bad sub-task can't
    sink the batch.

    Args:
        item: One task descriptor, expected to be a mapping with a ``prompt`` (str) and optional
            ``target`` / ``task`` / ``max_tokens``.
        index: The item's position in the input list (echoed back for correlation).
        roster_path: Optional roster path threaded to :func:`run_delegate`.

    Returns:
        ``{"index", "status", ...}`` — ``status`` is ``ok`` (+``text``), ``no_fit`` (+``message``),
        or ``error`` (+``error``).
    """
    if not isinstance(item, dict) or not isinstance(item.get("prompt"), str):
        return {
            "index": index,
            "status": "error",
            "error": "each task must be a mapping with a string 'prompt'",
        }
    try:
        text = run_delegate(
            item["prompt"],
            target=item.get("target"),
            task=item.get("task"),
            max_tokens=item.get("max_tokens", DEFAULT_DELEGATE_MAX_TOKENS),
            roster_path=roster_path,
        )
        return {"index": index, "status": "ok", "text": text}
    except NoDelegateFit as exc:
        return {
            "index": index,
            "status": "no_fit",
            "message": (
                f"{exc}. Handle this sub-task yourself — you are the most capable backend available."
            ),
        }
    except Exception as exc:  # AdapterError / SelectionError / RosterError / anything: isolate it
        return {"index": index, "status": "error", "error": str(exc)}


def run_delegate_many(
    tasks: list,
    max_concurrency: int | None = None,
    roster_path: str | None = None,
    settings: Settings | None = None,
) -> list[dict]:
    """Fan out several sub-tasks concurrently and collect their results in input order.

    The parallel-dispatch primitive: each item is routed independently via :func:`run_delegate` (so a
    batch can mix targets — grunt to local, code to a sub), run on a thread pool, and collected.
    Concurrency is bounded by :func:`_effective_concurrency` (operator setting or system-derived
    default, optionally lowered per call). This is **dispatch + collect only** — synthesising the
    results is the orchestrator's job.

    Partial failure never sinks the batch: each result carries a ``status`` (``ok`` / ``no_fit`` /
    ``error``), and results are returned **ordered by input index** even though workers finish out of
    order. Non-local delegate spend is **not metered** (consistent with the rest of the delegate).

    Args:
        tasks: A list of task descriptors, each a mapping ``{prompt, target?, task?, max_tokens?}``.
        max_concurrency: Optional per-call cap that may lower (never exceed) the effective concurrency.
        roster_path: Optional roster YAML path, threaded to each :func:`run_delegate`.
        settings: Global settings (for the concurrency cap). Defaults to :func:`load_settings`.

    Returns:
        One result dict per input task, in input order:
        ``{"index", "status": "ok"|"no_fit"|"error", "text"|"message"|"error"}``.

    Raises:
        ValueError: If ``tasks`` is not a list (a batch-level precondition). Per-item failures do
            **not** raise — they surface as ``status: "error"`` entries.
    """
    if not isinstance(tasks, list):
        raise ValueError(f"tasks must be a list of task mappings, got {type(tasks).__name__}")
    if not tasks:
        return []
    if settings is None:
        settings = load_settings()

    workers = _effective_concurrency(settings, max_concurrency)
    results: list[dict | None] = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_one_of_many, item, i, roster_path): i
            for i, item in enumerate(tasks)
        }
        for future in futures:
            index = futures[future]
            results[index] = future.result()
    # Every index is filled by exactly one worker (one future per task, _run_one_of_many never
    # raises). Preserve batch length + order; surface an (impossible) hole as a loud error entry
    # rather than silently shrinking the result list.
    return [
        r if r is not None else {"index": i, "status": "error", "error": "internal: no result produced"}
        for i, r in enumerate(results)
    ]


def delegate_targets(roster_path: str | None = None) -> list[dict]:
    """Return the configured delegate-target menu — the backends an orchestrator may target.

    Each target is described by what an orchestrator needs to pick by fit: its id, tier, ``good_at``
    tags, cost annotation, and invoke kind. Secret-safe — emits no ``key_ref`` or credential, and
    resolves nothing. The free local default target (reached via ``target=None``) is intentionally
    not listed here unless it is also explicitly flagged ``can_delegate``.

    Args:
        roster_path: Optional roster YAML path. When ``None``, resolved by
            :func:`tanglebrain.roster.default_roster_path`.

    Returns:
        One dict per ``can_delegate`` entry, in declared order:
        ``{"id", "tier", "good_at", "cost", "kind"}``.

    Raises:
        RosterError: If the roster cannot be loaded.
    """
    roster = load_roster(roster_path)
    return [
        {
            "id": entry.id,
            "tier": entry.tier,
            "good_at": list(entry.good_at),
            "cost": entry.cost,
            "kind": entry.invoke.kind,
        }
        for entry in roster.delegate_targets()
    ]


def _render_target_menu(targets: list[dict]) -> str:
    """Render the delegate-target menu as human-readable lines for a tool description.

    Args:
        targets: The menu from :func:`delegate_targets`.

    Returns:
        A newline-joined bullet list (one line per target), or a short note when the menu is empty.
    """
    if not targets:
        return (
            "  (no additional delegate targets configured — only the default local target, "
            "via delegate_local or delegate with target omitted, is available)"
        )
    lines = []
    for target in targets:
        skills = ", ".join(target["good_at"]) or "—"
        cost = target["cost"] or target["tier"]
        lines.append(f"  - {target['id']}: good_at [{skills}] (cost: {cost})")
    return "\n".join(lines)
