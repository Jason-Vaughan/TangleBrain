"""Lifetime aggregates — the permanent half of the measurement store (``totals.json``).

The usage log answers two different questions out of one file. *"How much spend has TangleBrain
avoided since it was installed"* is a **lifetime** claim that must never shrink. *"Which backends
served the recent tasks"* is a **window** onto the rows still on disk. Only the window can be
bounded, so the lifetime claim needs somewhere the rows can be folded *into* — that place is
``totals.json``, written beside the usage log under :func:`~tanglebrain.router.state_root`.

Splitting the two is what makes a wrong headline attributable: the figure is a sum of a stored
lifetime total and the rows currently on disk, so a discrepancy is either in this file or in the
window, never diffused across both.

Reading is deliberately total. An absent, unreadable, or corrupt file reads as all-zeros, so a log
that has never been compacted rolls up exactly as it did before this file existed, and a damaged
one degrades to a smaller number rather than to an error (`observability-strategy.md` § Direction:
observability degrades to less information, never to an error).

**Forward compatibility is a property of the format, not an event.** An unknown key is ignored and
a missing key reads as zero — the same contract the usage record honours (`data-model.md` §
Direction), and for the same reason: files written by other versions of TangleBrain are already on
disk and an append-only store has no migration story for them. There is deliberately **no
schema-version field**. A version number advertises that a breaking revision is possible; the
additive-only contract exists precisely so that one never has to be, and a field nothing reads is a
value waiting to rot.

Ignoring an unknown key on *read* would destroy it on *write*, so :func:`write_totals` carries
every field it does not recognise straight through from the file it replaces. Without that, the
first fold performed by an older TangleBrain would permanently delete fields a newer one wrote —
and "every version of TangleBrain that shares the file" is a named consumer of this format
(`boundaries.md`). **Carrying a field is not maintaining it:** a version that does not know a field
cannot add the folded rows' contribution to it, so the value goes stale rather than being lost.
Stale-and-recoverable beats absent, which is why the round-trip preserves rather than drops.

**The delegates' ``by_parent`` tree is absent on purpose.** It carries one key per parent task id,
so its cardinality grows without bound and it cannot be folded into a file that has to stay small.
It is inherently window-scoped, and every renderer of the rollup says so.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from tanglebrain.atomic import atomic_write
from tanglebrain.router import state_root

TOTALS_FILENAME = "totals.json"

# The lifetime shape, split by how each field is coerced on read. Declared as data rather than
# written out in a normalizer body so the field list has exactly one home: `empty_totals` builds
# the zeroed value from it and `normalize_totals` projects a parsed file onto it, and neither can
# drift from the other.
_INT_FIELDS = ("tasks", "failures", "lost_attempts", "in_tokens_est", "out_tokens_est")
_FLOAT_FIELDS = ("cloud_equiv_usd", "spend_avoided_usd")
_COUNT_MAPS = ("by_tier", "by_origin")
_DELEGATE_INT_FIELDS = ("count", "in_tokens_est", "out_tokens_est")
_DELEGATE_FLOAT_FIELDS = ("cloud_equiv_usd",)
#: Per-backend aggregate fields — the one nested numeric map bounded enough to fold (one key per
#: roster backend, not one per task). Public because `measurement.rollup` builds its own per-backend
#: entries from this tuple: a second literal there would be a third copy of a field list that has to
#: agree, and the drift would only surface as a per-backend figure silently going window-scoped.
BACKEND_INT_FIELDS = ("count", "in_tokens_est", "out_tokens_est")


def as_int(value: object) -> int:
    """Coerce a stored numeric field to ``int``, defaulting to 0 on any bad value.

    Shared by both halves of the measurement store. Anything read back off disk was written by
    another process, possibly by another version, possibly interrupted mid-write — so a string, a
    ``null``, or an object where a number belonged has to yield a number rather than abort a
    rollup that the rest of the file could still answer.

    Args:
        value: The raw value parsed out of the stored JSON.

    Returns:
        The value as an ``int``, or ``0`` if it cannot be one.
    """
    try:
        return int(value)  # type: ignore[call-overload]  # guarded by the except below
    except (ValueError, TypeError):
        return 0


def as_float(value: object) -> float:
    """Coerce a stored numeric field to ``float``, defaulting to 0.0 on any bad value.

    The float twin of :func:`as_int`, and tolerant for the same reason.

    Args:
        value: The raw value parsed out of the stored JSON.

    Returns:
        The value as a ``float``, or ``0.0`` if it cannot be one.
    """
    try:
        return float(value)  # type: ignore[arg-type]  # guarded by the except below
    except (ValueError, TypeError):
        return 0.0


def default_totals_path() -> Path:
    """Return the lifetime-totals file path — beside the usage log, in the data tier.

    Colocated with the log deliberately: the two files are one store, and a totals file that
    outlived its rows (or vice versa) would describe a log that no longer exists. Both resolve
    under :func:`~tanglebrain.router.state_root`, so a single override relocates the pair.

    Returns:
        The absolute path to ``totals.json``.
    """
    return state_root() / TOTALS_FILENAME


def empty_totals() -> dict:
    """Return a zeroed totals structure — the value an absent or corrupt file reads as.

    Every field the rollup treats as lifetime appears here, and the correspondence is mostly
    *constructed* rather than asserted: :func:`~tanglebrain.measurement.rollup` seeds its summary
    from this structure and builds its per-backend entries from :data:`BACKEND_INT_FIELDS`, so a
    field declared here flows into the rollup with nothing to keep in step. What construction does
    not cover is a key the rollup adds on its own, and
    ``test_every_lifetime_rollup_field_has_a_home_in_totals`` pins exactly that residual — with the
    delegates' ``by_parent`` tree as the single named exception, unbounded and therefore
    window-scoped (see the module docstring). A second exception fails the suite.

    Returns:
        A fresh, fully-zeroed totals dict. Never shared — callers may mutate it.
    """
    totals: dict = {field: 0 for field in _INT_FIELDS}
    totals.update({field: 0.0 for field in _FLOAT_FIELDS})
    totals.update({field: {} for field in _COUNT_MAPS})
    # The set of reference-pricing revisions these totals were computed under. Stored because
    # compaction destroys the per-row `pricing_ref` evidence, and a lifetime figure summed across
    # two different reference prices is a figure whose caveat has to survive its rows.
    totals["pricing_refs"] = []
    delegates: dict = {field: 0 for field in _DELEGATE_INT_FIELDS}
    delegates.update({field: 0.0 for field in _DELEGATE_FLOAT_FIELDS})
    delegates["by_backend"] = {}
    totals["delegates"] = delegates
    return totals


def _count_map(raw: object) -> dict:
    """Normalize a stored ``{key: count}`` map, coercing each count and dropping non-maps.

    Args:
        raw: The value stored under a count-map field.

    Returns:
        ``{str: int}``; ``{}`` when the stored value was not an object.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(k): as_int(v) for k, v in raw.items()}


def _backend_map(raw: object) -> dict:
    """Normalize the delegates' ``by_backend`` map of per-model aggregate dicts.

    Args:
        raw: The value stored under ``delegates.by_backend``.

    Returns:
        ``{model: {count, in_tokens_est, out_tokens_est}}``; ``{}`` when the stored value was not
        an object. A model whose aggregate is not an object normalizes to zeros rather than
        vanishing — the model *was* recorded, and dropping it would understate the backend split.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for model, info in raw.items():
        info = info if isinstance(info, dict) else {}
        out[str(model)] = {field: as_int(info.get(field)) for field in BACKEND_INT_FIELDS}
    return out


def _pricing_refs(raw: object) -> list[str]:
    """Normalize the stored set of pricing revisions to a sorted, de-duplicated list of strings.

    JSON has no set type, so the field is stored as an array and canonicalized on read: sorting
    and de-duplicating here means two readers of the same file always agree on the value, and a
    caller can compare or merge without re-normalizing.

    Args:
        raw: The value stored under ``pricing_refs``.

    Returns:
        The revisions, sorted and unique; ``[]`` when the stored value was not an array.
    """
    if not isinstance(raw, list):
        return []
    return sorted({str(ref) for ref in raw})


def normalize_totals(raw: object) -> dict:
    """Project arbitrary parsed JSON onto the totals shape — unknown keys out, missing keys zero.

    This function *is* the format's forward-compatibility contract, executed. A file written by a
    newer TangleBrain carrying fields this version has never heard of loses those fields and keeps
    every one it shares; a file written by an older one is filled out with zeros. Neither is an
    error, because both are ordinary states of a store that several versions write to over its
    lifetime.

    Args:
        raw: Whatever ``json.loads`` produced — not necessarily an object.

    Returns:
        A totals dict in the canonical shape. A non-object input yields :func:`empty_totals`.
    """
    if not isinstance(raw, dict):
        return empty_totals()
    totals = empty_totals()
    for field in _INT_FIELDS:
        totals[field] = as_int(raw.get(field))
    for field in _FLOAT_FIELDS:
        totals[field] = as_float(raw.get(field))
    for field in _COUNT_MAPS:
        totals[field] = _count_map(raw.get(field))
    totals["pricing_refs"] = _pricing_refs(raw.get("pricing_refs"))
    stored_delegates = raw.get("delegates")
    stored_delegates = stored_delegates if isinstance(stored_delegates, dict) else {}
    delegates = totals["delegates"]
    for field in _DELEGATE_INT_FIELDS:
        delegates[field] = as_int(stored_delegates.get(field))
    for field in _DELEGATE_FLOAT_FIELDS:
        delegates[field] = as_float(stored_delegates.get(field))
    delegates["by_backend"] = _backend_map(stored_delegates.get("by_backend"))
    return totals


def read_totals(path: str | os.PathLike[str] | None = None) -> dict:
    """Read the lifetime totals, tolerating an absent, unreadable, or corrupt file.

    Absence is the *normal* case for a log that has never been compacted, not a failure — such a
    log rolls up from its rows alone, byte-identically to how it did before this file existed.
    Corruption degrades the same way: a partially-written totals file understates the lifetime
    figure, which is a smaller number rather than a broken command.

    Args:
        path: Override the totals path (tests inject a temp path). Defaults to
            :func:`default_totals_path`.

    Returns:
        The normalized totals (see :func:`normalize_totals`), or :func:`empty_totals` when the
        file is absent or unusable.
    """
    return normalize_totals(read_raw_totals(path))


def read_raw_totals(path: str | os.PathLike[str] | None = None) -> object:
    """Read the totals file as parsed JSON, *unprojected* — every key, including unrecognised ones.

    :func:`read_totals` is what the rollup wants: the format's own shape, with anything foreign
    projected away. This is what the *writer* wants: the fields a newer TangleBrain may have added,
    so they can ride through a round-trip instead of being deleted by a version that never knew
    them (see :func:`write_totals`).

    Args:
        path: Override the totals path. Defaults to :func:`default_totals_path`.

    Returns:
        Whatever ``json.loads`` produced — not necessarily an object — or ``None`` when the file is
        absent or unusable, which the callers treat the same way an empty file would be treated.
    """
    target = Path(path) if path is not None else default_totals_path()
    try:
        # `json.JSONDecodeError` and `UnicodeDecodeError` are both `ValueError` subclasses, so the
        # unreadable-bytes and unparseable-text cases are covered by the one entry.
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def carry_unknown_fields(raw: object, totals: dict) -> dict:
    """Overlay ``totals`` onto ``raw``, keeping any field ``totals`` has no opinion about.

    The inverse of :func:`normalize_totals`, and the reason a round-trip through an older
    TangleBrain is not destructive: every key this version knows takes its freshly-computed value,
    and every key it does not know survives untouched at whatever depth it was found. Nested maps
    are merged the same way, so a field invented inside ``delegates`` — or inside one backend's
    entry — is preserved as readily as a top-level one.

    A key present in both wins for ``totals`` even when the stored value was garbage, because
    :func:`normalize_totals` has already turned that garbage into a usable zero; carrying it back
    would undo the coercion this format relies on.

    Args:
        raw: The parsed prior file (see :func:`read_raw_totals`). A non-object is ignored.
        totals: The computed totals to write.

    Returns:
        A new dict — ``totals``, plus whatever only ``raw`` had. Neither argument is mutated.
    """
    if not isinstance(raw, dict):
        return dict(totals)
    merged = dict(totals)
    for key, value in raw.items():
        key = str(key)
        if key not in merged:
            merged[key] = value
        elif isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = carry_unknown_fields(value, merged[key])
    return merged


def write_totals(totals: dict, path: str | os.PathLike[str] | None = None) -> None:
    """Replace the lifetime totals file atomically, preserving fields this version does not know.

    Two properties, both load-bearing and both unconditional — a knob to disable either would be a
    knob for corrupting the store:

    - **Atomic.** The file is staged beside itself and renamed over (:func:`~tanglebrain.atomic.
      atomic_write`), so a crash mid-write leaves the previous totals whole. A reader never sees a
      half-written object, and the compaction that calls this can therefore treat a raised
      exception as "nothing happened".
    - **Non-destructive.** Unknown keys are carried through from the file being replaced
      (:func:`carry_unknown_fields`), read at the last moment rather than taken from the caller, so
      no writer can forget to preserve them.

    Args:
        totals: The totals to persist. Only the fields this version computes need be present.
        path: Override the totals path. Defaults to :func:`default_totals_path`.

    Raises:
        OSError: If the directory cannot be created, or the staging write or rename fails. The
            existing file is untouched in every failing case.
    """
    target = Path(path) if path is not None else default_totals_path()
    merged = carry_unknown_fields(read_raw_totals(target), totals)
    atomic_write(target, json.dumps(merged, indent=2, sort_keys=True) + "\n")
