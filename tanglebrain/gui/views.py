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
from tanglebrain.measurement import load_pricing, read_records, rollup
from tanglebrain.roster import RosterError, load_roster
from tanglebrain.router import RouterError
from tanglebrain.selector import SelectionError

# Default port for the panel; registered permanently in TangleClaw PortHub for project TangleBrain.
DEFAULT_PORT = 3250

# Exceptions that represent an expected, user-facing failure of a run (mirrors cli.main()).
_RUN_ERRORS = (RosterError, SelectionError, RouterError, AdapterError)


def view_roster() -> dict:
    """Build the roster view: every entry with its tier, cost, tags, and invoke summary.

    ``key_ref`` is passed through verbatim as the reference string — never resolved, so no key
    file contents are read or exposed. ``cmd``/``scrub_env``/``delegate_args`` are deliberately
    omitted (not needed for the panel and keep the payload focused).

    Returns:
        ``{"entries": [ {id, tier, cost, good_at, can_orchestrate, invoke{...}}, ... ]}``.
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
    """Build the spend-avoided rollup view (the local C4 ``--stats`` data).

    Returns:
        ``{summary, pricing_ref, is_placeholder}`` where ``summary`` is :func:`rollup`'s dict.
    """
    pricing = load_pricing()
    return {
        "summary": rollup(read_records()),
        "pricing_ref": pricing.reference_model,
        "is_placeholder": pricing.is_placeholder,
    }


def _last_served() -> dict | None:
    """Return the tier/model that served the most recent routed task, from the C4 usage log.

    Best-effort and single-user: the panel reads the last usage record right after a run to show
    which tier handled it. Returns ``None`` if no record is available (e.g. logging was dropped).
    """
    records = read_records()
    if not records:
        return None
    last = records[-1]
    return {"path": last.get("path"), "tier": last.get("tier"), "model": last.get("model")}


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
        text = run_once(str(prompt), model=model, local=local, task=task)
    except _RUN_ERRORS as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "text": text, "served": _last_served()}
