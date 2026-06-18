"""View functions for the knob GUI — pure, transport-free, JSON-able dict builders.

These hold all the panel's logic so it can be unit-tested without binding a socket or making a
network call (mirroring how :mod:`tanglebrain.cli` is a thin wrapper over ``run_once``). The HTTP
layer in :mod:`tanglebrain.gui.server` only routes requests to these and serializes the result.

Read-only: nothing here writes config. Secret-safety — :func:`view_roster` emits ``key_ref`` as the
stored *reference string* only (e.g. ``file:…``, ``env:NAME``); it never resolves the reference or
reads a key file, so no secret material can reach the browser.
"""
from __future__ import annotations

from tanglebrain.adapters import AdapterError
from tanglebrain.cli import run_once
from tanglebrain.measurement import load_pricing, read_records, rollup, save_pricing, validate_pricing
from tanglebrain.roster import RosterError, load_roster
from tanglebrain.roster_edit import RosterEditError, save_roster_edits
from tanglebrain.router import RouterError
from tanglebrain.selector import SelectionError
from tanglebrain.settings import load_settings

# Default panel port (3250).
DEFAULT_PORT = 3250

# Exceptions that represent an expected, user-facing failure of a run (mirrors cli.main()).
_RUN_ERRORS = (RosterError, SelectionError, RouterError, AdapterError)


def view_roster() -> dict:
    """Build the roster view: every entry with its tier, cost, tags, and invoke summary.

    ``key_ref`` is passed through verbatim as the reference string — never resolved, so no key
    file contents are read or exposed. ``cmd``/``scrub_env``/``delegate_args`` are deliberately
    omitted (not needed for the panel and keep the payload focused).

    Returns:
        ``{"entries": [ {id, tier, cost, good_at, can_orchestrate, enabled, budget_usd_month,
        invoke{...}}, ... ]}``. ``enabled`` / ``budget_usd_month`` matter for ``tier: api`` entries:
        ``enabled`` is the per-key kill-switch and ``budget_usd_month`` a display-only cap (enforced
        gateway-side). Whether paid entries are actually routable also depends on the global gate —
        see :func:`view_settings`.
    """
    roster = load_roster()
    entries = []
    for e in roster.entries:
        entries.append(
            {
                "id": e.id,
                "tier": e.tier,
                "cost": e.cost,
                "good_at": list(e.good_at),
                "can_orchestrate": e.can_orchestrate,
                "enabled": e.enabled,
                "budget_usd_month": e.budget_usd_month,
                "invoke": {
                    "kind": e.invoke.kind,
                    "base_url": e.invoke.base_url,
                    "model": e.invoke.model,
                    "parse": e.invoke.parse,
                    # Reference string only — see module docstring. Never resolved.
                    "key_ref": e.invoke.key_ref,
                },
            }
        )
    return {"entries": entries}


def view_settings() -> dict:
    """Build the global-settings view — the paid-API billing gate.

    ``api_billing_enabled`` is the master switch: when ``false`` (the default) no
    ``tier: api`` entry is routable regardless of its own ``enabled`` flag. The panel surfaces it so
    an operator sees at a glance whether paid routing is live. Reads only ``config/settings.yaml`` —
    no key file or secret is touched.

    Returns:
        ``{"api_billing_enabled": bool}``.
    """
    return {"api_billing_enabled": load_settings().api_billing_enabled}


def view_pricing() -> dict:
    """Build the pricing view from ``config/pricing.yaml``.

    Returns:
        ``{reference_model, input_per_mtok, output_per_mtok, is_placeholder}``.
    """
    p = load_pricing()
    return {
        "reference_model": p.reference_model,
        "input_per_mtok": p.input_per_mtok,
        "output_per_mtok": p.output_per_mtok,
        "is_placeholder": p.is_placeholder,
    }


def view_stats() -> dict:
    """Build the spend-avoided rollup view (the local ``--stats`` data).

    Returns:
        ``{summary, pricing_ref, is_placeholder}`` where ``summary`` is :func:`rollup`'s dict.
    """
    pricing = load_pricing()
    return {
        "summary": rollup(read_records()),
        "pricing_ref": pricing.reference_model,
        "is_placeholder": pricing.is_placeholder,
    }


def run_prompt(payload: dict) -> dict:
    """Run one prompt through the router and report the result + which tier served it.

    Args:
        payload: ``{prompt, task?, local?, model?}`` from the panel's run box.

    Returns:
        ``{"ok": True, "text": ..., "served": {path, tier, model} | None}`` on success, or
        ``{"ok": False, "error": ...}`` on an empty prompt or an expected backend error.
    """
    prompt = (payload or {}).get("prompt")
    if not prompt or not str(prompt).strip():
        return {"ok": False, "error": "prompt is required"}

    task = payload.get("task") or None
    model = payload.get("model") or None
    local = bool(payload.get("local", False))

    try:
        # return_served gives us the served tier/model directly — no usage-log re-read, no race.
        text, served = run_once(str(prompt), model=model, local=local, task=task, return_served=True)
    except _RUN_ERRORS as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "text": text, "served": served}


def save_roster_view(payload: dict) -> dict:
    """Apply edits to one roster entry's editable fields.

    Only the focused, comment-safe scalar fields are editable (see
    :mod:`tanglebrain.roster_edit`): ``enabled``, ``can_orchestrate``, ``budget_usd_month``,
    ``good_at``. Entries are never added/removed/reordered here and the ``invoke`` block is not
    editable — those stay hand-edits. The write is validated, backed up, and atomic.

    Args:
        payload: ``{id, fields: {field: value, ...}}``.

    Returns:
        ``{"ok": True, "roster": {...}}`` with the re-read roster view on success, or
        ``{"ok": False, "error": ...}`` if the id/fields are missing or an edit is rejected
        (nothing is written on failure).
    """
    entry_id = (payload or {}).get("id")
    fields = (payload or {}).get("fields")
    if not entry_id or not isinstance(fields, dict) or not fields:
        return {"ok": False, "error": "id and a non-empty 'fields' object are required"}
    try:
        save_roster_edits(str(entry_id), fields)
    except RosterEditError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "roster": view_roster()}


def save_pricing_view(payload: dict) -> dict:
    """Validate and persist edited pricing from the panel.

    Args:
        payload: ``{reference_model, input_per_mtok, output_per_mtok, placeholder}``.

    Returns:
        ``{"ok": True, "pricing": {...}}`` with the re-read pricing on success, or
        ``{"ok": False, "error": ...}`` if validation fails (nothing is written on failure).
    """
    try:
        pricing = validate_pricing(payload or {})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    save_pricing(pricing)
    return {"ok": True, "pricing": view_pricing()}
