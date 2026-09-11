"""View functions for the knob GUI — pure, transport-free, JSON-able dict builders.

These hold all the panel's logic so it can be unit-tested without binding a socket or making a
network call (mirroring how :mod:`tanglebrain.cli` is a thin wrapper over ``run_once``). The HTTP
layer in :mod:`tanglebrain.gui.server` only routes requests to these and serializes the result.

Read-only: nothing here writes config. Secret-safety — :func:`view_roster` emits ``key_ref`` as the
stored *reference string* only (e.g. ``file:…``, ``env:NAME``); it never resolves the reference or
reads a key file, so no secret material can reach the browser.

Payload-safety — :func:`view_stats` **projects** the measurement rollup rather than forwarding it.
The endpoint names every field it returns, so a field added to the rollup for the CLI's benefit
does not silently become part of the panel's payload, and the per-day map cannot grow the response
as its retention does.
"""
from __future__ import annotations

from datetime import date, timedelta

from tanglebrain.adapters import AdapterError
from tanglebrain.cli import run_once
from tanglebrain.measurement import (
    load_pricing,
    probe_measurement_health,
    read_records,
    rollup,
    save_pricing,
    validate_pricing,
)
from tanglebrain.roster import RosterError, load_roster
from tanglebrain.roster_edit import RosterEditError, save_roster_edits
from tanglebrain.router import RouterError
from tanglebrain.selector import SelectionError
from tanglebrain.settings import load_settings
from tanglebrain.totals import as_float, as_int, is_day_key, read_totals

# Default panel port (3250).
DEFAULT_PORT = 3250

#: How many day buckets ``/api/stats`` ships, newest-last. The panel's widest window is 90 days, so
#: this is what it has a renderer for; the store retains far more (400) as headroom for a view that
#: does not exist yet. Sending the rest would put roughly 52 KB on every panel load to draw nothing.
#: Widening this is a one-line server change, which is the property that makes capping it safe.
STATS_DAY_WINDOW = 90

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


def _parse_day_buckets(by_day: object) -> dict[date, dict]:
    """Read the rollup's ``by_day`` map into real dates, dropping keys that are not days.

    Everything in the measurement store was written by another process and possibly another
    version, and the normalizer that reads it deliberately keys the map by whatever string it
    found — it validates the *aggregate*, not the key. So a damaged file can carry ``"banana"``
    beside ``"2026-09-10"``. Date arithmetic on that raises, and a chart is not worth a 500 when
    the rest of the payload is sound; the unreadable keys are dropped and their dollars surface in
    :func:`_spend_outside_days` as spend no day bucket holds.

    Both checks are needed. :func:`~tanglebrain.totals.is_day_key` is the project's one validator
    for the format and accepts any ten digits in the right places, so ``"2026-13-45"`` passes it
    and still is not a date.

    Args:
        by_day: The rollup's ``by_day`` value, of whatever type it turned out to be.

    Returns:
        ``{date: aggregate}`` for every key that is a real calendar day.
    """
    if not isinstance(by_day, dict):
        return {}
    parsed: dict[date, dict] = {}
    for key, bucket in by_day.items():
        if not is_day_key(key):
            continue
        try:
            day = date.fromisoformat(key)
        except ValueError:
            continue
        parsed[day] = bucket if isinstance(bucket, dict) else {}
    return parsed


def _day_series(days: dict[date, dict], window: int = STATS_DAY_WINDOW) -> list[dict]:
    """Order the per-day buckets into the newest ``window`` days, oldest first, gaps filled with 0.

    **The shape is the honesty.** In the stored map, an absent day means two different things: a
    day the router simply did not run on, and a day that predates per-day recording (or has been
    evicted past retention). Drawing the second as $0 asserts there was no activity when the truth
    is that none was kept — the exact failure the ``by_day_since`` stamp was added to prevent, one
    surface out. As a list the distinction stops being a rule the renderer has to know: the series
    *starts* at the first day actually covered, and every absent day after that is a real zero,
    materialized. Taking the last N entries is then correct for any window the panel offers.

    The span is data-defined — it ends at the newest bucket, not at today — so this stays pure and
    its tests stay deterministic. The caption states the range it actually drew, which is why an
    install that has been idle for a week shows a chart that ends a week ago and says so.

    Args:
        days: Parsed buckets from :func:`_parse_day_buckets`.
        window: How many days to keep, counted back from the newest bucket.

    Returns:
        ``[{"day": "YYYY-MM-DD", "spend_avoided_usd": float}, ...]``, ascending, at most ``window``
        long. Empty when nothing is bucketed.
    """
    if not days or window < 1:
        return []
    newest = max(days)
    # Counted back from the newest bucket rather than filled from the oldest: one stray far-past key
    # in a damaged store would otherwise materialize centuries of zeroed days.
    first = max(min(days), newest - timedelta(days=window - 1))
    series: list[dict] = []
    day = first
    while day <= newest:
        bucket = days.get(day)
        spend = as_float(bucket.get("spend_avoided_usd")) if bucket else 0.0
        series.append({"day": day.isoformat(), "spend_avoided_usd": round(spend, 4)})
        day += timedelta(days=1)
    return series


def _model_breakdown(by_model: object) -> list[dict]:
    """Order the per-model buckets into the panel's table rows, biggest saver first.

    Sorted here rather than in the browser because the ranking *is* the table, and a JSON object's
    key order is not a contract — ordering it server-side is what puts the order under test. Ties
    break on id so the table does not reshuffle between loads.

    Not truncated: the key set is bounded by the roster ids that have ever served, and a breakdown
    that must sum to the headline above it cannot drop its tail without lying.

    Args:
        by_model: The rollup's ``by_model`` value, of whatever type it turned out to be.

    Returns:
        ``[{"id": str, "count": int, "spend_avoided_usd": float}, ...]``, spend descending.
    """
    if not isinstance(by_model, dict):
        return []
    # Sorted as typed tuples rather than as the dicts themselves: a dict's values are `object` to
    # the checker, and a negated `object` is not a sort key it can accept.
    rows: list[tuple[float, str, int]] = []
    for model, bucket in by_model.items():
        entry = bucket if isinstance(bucket, dict) else {}
        rows.append(
            (
                round(as_float(entry.get("spend_avoided_usd")), 4),
                str(model),
                as_int(entry.get("count")),
            )
        )
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [{"id": model, "count": count, "spend_avoided_usd": spend} for spend, model, count in rows]


def _backend_breakdown(by_backend: object) -> list[dict]:
    """Order the delegates' per-backend counts, busiest first — the ``by_model`` treatment, counted.

    The delegate block reports calls, not dollars: a sub-call's saving is already credited to the
    task that spawned it, so counting is the only honest ordering here.

    Args:
        by_backend: The rollup's ``delegates.by_backend`` value, of whatever type it turned out
            to be.

    Returns:
        ``[{"id": str, "count": int}, ...]``, count descending then id ascending.
    """
    if not isinstance(by_backend, dict):
        return []
    rows: list[tuple[int, str]] = []
    for model, info in by_backend.items():
        entry = info if isinstance(info, dict) else {}
        rows.append((as_int(entry.get("count")), str(model)))
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [{"id": model, "count": count} for count, model in rows]


def _spend_outside_days(lifetime_spend: float, days: dict[date, dict]) -> float:
    """Report how many lifetime dollars no day bucket accounts for.

    The panel cannot work this out for itself, and the caption under the chart depends on it. It
    receives a 90-day series and a lifetime total, and from those two alone *the window is
    narrower than the store's life* is indistinguishable from *the per-day data starts later than
    the store does*. This is the second: the lifetime figure minus **every** retained day bucket,
    not just the charted ones.

    Non-zero means dollars in the headline that the chart structurally cannot show — spend from
    before per-day recording began on this install, from days evicted past retention, or from
    records whose timestamp was unreadable and so reached ``by_model`` but no day.

    Floored at zero: the two figures are summed over the same records through the same body, so a
    negative can only be float noise or a damaged store, and neither is worth rendering as a
    negative dollar amount.

    Args:
        lifetime_spend: The rollup's lifetime ``spend_avoided_usd``.
        days: Parsed buckets from :func:`_parse_day_buckets` — all of them, not the charted window.

    Returns:
        The unbucketed remainder in dollars, rounded to 4dp, never below 0.
    """
    bucketed = sum(as_float(bucket.get("spend_avoided_usd")) for bucket in days.values())
    return max(round(lifetime_spend - bucketed, 4), 0.0)


def project_stats_summary(summary: dict) -> dict:
    """Project the measurement rollup onto the fields ``/api/stats`` promises the panel.

    **Why a projection and not a passthrough.** Returning :func:`~tanglebrain.measurement.rollup`'s
    dict verbatim made the endpoint's payload whatever the rollup happened to contain, so a field
    added for the CLI's benefit shipped to the browser by default — three of them did, one capable
    of reaching ~52 KB, without anyone deciding it (#223). Naming the fields inverts that: reaching
    the panel is now a decision, and the next lifetime field cannot leak by accident.

    The two breakdowns are reshaped from maps into ordered lists (see :func:`_day_series` and
    :func:`_model_breakdown`), and the delegates' unbounded ``by_parent`` tree collapses to the one
    number the panel renders from it. That the endpoint's shape is no longer the store's shape is
    the point of having a contract of its own; Surface 4 is localhost-only and internal, and the
    panel that consumes it ships in the same package.

    Args:
        summary: The rollup dict for the whole store — stored totals plus the current row window.

    Returns:
        The panel's ``summary`` payload. Lifetime throughout, with two exceptions it labels:
        ``by_day`` covers only its charted window, and ``delegates.linked_parents`` counts the
        current row window because the tree it is derived from is never folded into the totals.
    """
    delegates = summary.get("delegates") or {}
    by_parent = delegates.get("by_parent") or {}
    by_backend = delegates.get("by_backend") or {}
    days = _parse_day_buckets(summary.get("by_day"))
    spend = as_float(summary.get("spend_avoided_usd"))
    return {
        "tasks": as_int(summary.get("tasks")),
        "spend_avoided_usd": spend,
        "by_tier": {str(k): as_int(v) for k, v in (summary.get("by_tier") or {}).items()},
        "by_origin": {str(k): as_int(v) for k, v in (summary.get("by_origin") or {}).items()},
        "in_tokens_est": as_int(summary.get("in_tokens_est")),
        "out_tokens_est": as_int(summary.get("out_tokens_est")),
        "by_model": _model_breakdown(summary.get("by_model")),
        "by_day": _day_series(days),
        # The day per-day recording began. Carried for the caption's *wording* only — it is older
        # than the oldest surviving bucket once eviction bites, so drawing from it would paint the
        # evicted span as $0. The drawable boundary is the series' own first entry.
        "by_day_since": str(summary.get("by_day_since") or ""),
        "spend_avoided_outside_days_usd": _spend_outside_days(spend, days),
        "delegates": {
            "count": as_int(delegates.get("count")),
            "linkage_lost": as_int(delegates.get("linkage_lost")),
            "in_tokens_est": as_int(delegates.get("in_tokens_est")),
            "out_tokens_est": as_int(delegates.get("out_tokens_est")),
            "cloud_equiv_usd": round(as_float(delegates.get("cloud_equiv_usd")), 4),
            "by_backend": _backend_breakdown(by_backend),
            # How many top-level tasks the sub-calls link back to — the only thing the panel drew
            # from `by_parent`, which holds one key per parent task id and is therefore the largest
            # unbounded structure the old passthrough could ship.
            "linked_parents": len([k for k in by_parent if k != "unlinked"]),
        },
    }


def view_stats() -> dict:
    """Build the spend-avoided rollup view (the local ``--stats`` data).

    Reads both halves of the measurement store, like the CLI: stored lifetime totals plus the
    current row window. Reading rows alone would leave the panel showing a shrinking figure once
    rows are compacted, which is the exact failure the totals file exists to prevent.

    Returns:
        ``{summary, pricing_ref, is_placeholder, health}`` where ``summary`` is
        :func:`project_stats_summary`'s named projection of :func:`rollup`'s dict — lifetime
        throughout, except the charted ``by_day`` window and the delegates' ``linked_parents``
        count, both of which the panel labels — and ``health`` is
        :func:`probe_measurement_health`'s findings, empty when the store is sound.

        The health probe runs on every *request* rather than once per process. That is the property
        it was chosen for: the one-shot stderr notice ``record_task`` emits is printed at most once
        for the life of a long-running ``gui`` process, so a store that breaks at hour six never
        reaches this surface through it, while any later ``/api/stats`` call reports the condition
        that is true when it is asked.

        **The honest limit: the panel does not poll.** It fetches on load, after a run, and after a
        pricing save, so an idle panel keeps showing the health of the store as it was at the last
        of those. The freshness this buys is per-request, not per-second.
    """
    pricing = load_pricing()
    return {
        "summary": project_stats_summary(rollup(read_records(), read_totals())),
        "pricing_ref": pricing.reference_model,
        "is_placeholder": pricing.is_placeholder,
        "health": probe_measurement_health(),
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
        text, served = run_once(
            str(prompt), model=model, local=local, task=task, return_served=True, origin="gui"
        )
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
