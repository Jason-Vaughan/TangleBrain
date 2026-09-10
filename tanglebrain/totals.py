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
_DELEGATE_INT_FIELDS = ("count", "linkage_lost", "in_tokens_est", "out_tokens_est")
_DELEGATE_FLOAT_FIELDS = ("cloud_equiv_usd",)
#: Per-backend aggregate fields — the one nested numeric map bounded enough to fold (one key per
#: roster backend, not one per task). Public because `measurement.rollup` builds its own per-backend
#: entries from this tuple: a second literal there would be a third copy of a field list that has to
#: agree, and the drift would only surface as a per-backend figure silently going window-scoped.
BACKEND_INT_FIELDS = ("count", "in_tokens_est", "out_tokens_est")

#: Per-model and per-day aggregate fields. Both maps carry the same shape because they answer the
#: same question sliced two ways — "of the spend this router avoided, how much is attributable to
#: *this* backend / *this* day" — and one shape means one normalizer and one accumulator body.
AGGREGATE_INT_FIELDS = ("count", "in_tokens_est", "out_tokens_est")
AGGREGATE_FLOAT_FIELDS = ("cloud_equiv_usd", "spend_avoided_usd")

#: How many day buckets ``by_day`` retains. **This cap is what makes the field admissible at all.**
#: One key per day grows without bound, which is the exact property that keeps the delegates'
#: ``by_parent`` tree out of this file (see the module docstring) — a map that grows forever cannot
#: live in a file every rollup reads. 400 covers a 90-day chart with headroom and leaves a
#: year-over-year view buildable later.
#:
#: The number is *policy*, not format: changing it breaks nothing on disk. But it is only usefully
#: changed in one direction. Narrowing later works; **widening later recovers nothing, because the
#: evicted days are gone** — which is why it was set wide rather than at the 90 the first consumer
#: needs.
BY_DAY_RETENTION = 400


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
    # Two slices of the headline, bounded by different things and to different degrees. `by_model`
    # takes its key from each record, so it is bounded by the set of roster ids that have ever
    # *served* — which grows when an id is renamed and never shrinks, making it slow-growing rather
    # than strictly bounded. `by_day` has no such ceiling at all and is bounded only because
    # `BY_DAY_RETENTION` evicts it at fold time.
    totals["by_model"] = {}
    totals["by_day"] = {}
    # The day per-day recording began — stamped once and never moved, eviction included.
    #
    # **This is not the boundary a renderer may draw from, and the distinction is load-bearing.**
    # Once eviction bites, this date is OLDER than the oldest surviving bucket, so drawing from it
    # would render the evicted span as $0 — asserting no activity over days that simply are not
    # kept, and doing it only on the long-lived stores where it is least visible. The drawable
    # boundary is `min(by_day)` and needs no field: before the earliest key, absence means
    # *unknown*; between keys, absence means a genuine zero-activity day.
    #
    # What this answers instead is the question `by_day` cannot: **do these per-day figures cover
    # the whole life of the store?** Compared against the lifetime `spend_avoided_usd`, it is what
    # lets a reader be told the chart starts later than the headline does. Nothing else survives
    # the first eviction to say so.
    totals["by_day_since"] = ""
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


def _aggregate_map(raw: object) -> dict:
    """Normalize a ``{key: {count, tokens, dollars}}`` map — the shape ``by_model`` and ``by_day`` share.

    Tolerant in the same direction as every other reader here: a non-object yields ``{}``, and a
    key whose aggregate is not an object normalizes to zeros rather than vanishing. Dropping it
    would understate a split that the store demonstrably recorded, which is the one direction a
    measurement figure must not fail in.

    Args:
        raw: The value stored under ``by_model`` or ``by_day``.

    Returns:
        ``{key: {count, in_tokens_est, out_tokens_est, cloud_equiv_usd, spend_avoided_usd}}``.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for key, info in raw.items():
        info = info if isinstance(info, dict) else {}
        entry: dict = {field: as_int(info.get(field)) for field in AGGREGATE_INT_FIELDS}
        entry.update({field: as_float(info.get(field)) for field in AGGREGATE_FLOAT_FIELDS})
        out[str(key)] = entry
    return out


def is_day_key(value: object) -> bool:
    """Report whether ``value`` is a ``YYYY-MM-DD`` day key.

    **The one validator for this format.** ``by_day``'s keys, ``by_day_since`` and the day derived
    from a record's timestamp are the same shape, and they are compared and ordered against each
    other — so a second, laxer implementation would let a string into one that the others reject,
    and the disagreement would surface as a caption or a chart boundary rather than as an error.

    Checked positionally rather than by splitting: ``"2026-9-100"`` splits into three runs of digits
    and is not a day.

    Args:
        value: The candidate key or stamp, of whatever type it turned out to be.

    Returns:
        ``True`` only for exactly ten characters of ``YYYY-MM-DD``.
    """
    if not isinstance(value, str) or len(value) != 10:
        return False
    if value[4] != "-" or value[7] != "-":
        return False
    return value[:4].isdigit() and value[5:7].isdigit() and value[8:10].isdigit()


def _day_stamp(raw: object) -> str:
    """Normalize a stored ``by_day_since``, yielding ``""`` for anything that is not a day.

    Args:
        raw: The value stored under ``by_day_since``.

    Returns:
        The stamp when :func:`is_day_key` accepts it; ``""`` otherwise.
    """
    return raw if is_day_key(raw) else ""  # type: ignore[return-value]  # guarded by is_day_key


def evict_old_days(totals: dict, keep: int = BY_DAY_RETENTION) -> dict:
    """Trim ``by_day`` to the newest ``keep`` buckets, dropping the oldest — mutates and returns.

    **Called only where the totals are persisted**, never on the read path. That mirrors the
    delegates' ``by_parent`` tree, which is likewise dropped in
    :func:`~tanglebrain.measurement.fold_records_into_totals` rather than in the shared summation:
    the cap exists to bound the *file*, and applying it to a rollup would shrink a figure the
    reader can already see rows for.

    Day keys sort lexicographically because they are zero-padded ``YYYY-MM-DD``, so "newest" needs
    no date parsing and a malformed key sorts to one end rather than raising.

    ``by_day_since`` is deliberately untouched. After the first eviction it is older than the
    oldest surviving bucket, and that gap *is* the signal it exists to carry (see
    :func:`empty_totals`).

    Args:
        totals: The totals being written. Mutated in place.
        keep: How many day buckets to retain. ``0`` empties the map.

    Returns:
        The same dict, for use as an expression.

    Raises:
        ValueError: If ``keep`` is negative.
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    by_day = totals.get("by_day")
    if not isinstance(by_day, dict) or len(by_day) <= keep:
        return totals
    newest = sorted(by_day)[len(by_day) - keep :] if keep else []
    totals["by_day"] = {day: by_day[day] for day in newest}
    return totals


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
    totals["by_model"] = _aggregate_map(raw.get("by_model"))
    totals["by_day"] = _aggregate_map(raw.get("by_day"))
    totals["by_day_since"] = _day_stamp(raw.get("by_day_since"))
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


#: Keys this version *knows about* and deliberately does not persist, by the path of the object
#: holding them. Carry-through consults this so a deliberate omission is never mistaken for a field
#: from a newer version: ``delegates.by_parent`` carries one entry per parent task id, so preserving
#: a stored copy would grow this file without bound — the exact outcome the totals/window split
#: exists to prevent, in the one file whose size the argument rests on.
#:
#: **A field ever RETIRED from the shape above belongs here too.** Removal is a boundary crossing
#: (`boundaries.md`), and without an entry here the writer would resurrect the retired field from
#: every install's existing file, at its stale value, forever — a tombstone with no eviction path.
NOT_PERSISTED: dict[tuple[str, ...], frozenset[str]] = {
    ("delegates",): frozenset({"by_parent"}),
}


#: Maps whose key set the *writer* owns, by the path of the map itself. Carry-through preserves a
#: stored key this version did not compute — which is right for a field from the future and wrong
#: for a key this version deliberately removed. ``by_day`` is trimmed to
#: :data:`BY_DAY_RETENTION` before it is written, so without this entry the merge below would read
#: every evicted day back out of the file it is replacing and put it straight back: the cap would
#: hold in memory, never on disk, and the file would grow without bound while every test that
#: checked the *returned* totals still passed.
#:
#: Narrower than dropping the merge entirely: a key present in **both** still merges recursively, so
#: a field a newer TangleBrain added *inside a retained day* survives. Only keys the writer removed
#: are treated as removed.
TRIMMED_MAPS: frozenset[tuple[str, ...]] = frozenset({("by_day",)})


def carry_unknown_fields(raw: object, totals: dict, _path: tuple[str, ...] = ()) -> dict:
    """Overlay ``totals`` onto ``raw``, keeping any field this version does not define.

    The inverse of :func:`normalize_totals`, and the reason a round-trip through an older
    TangleBrain is not destructive: every key this version computes takes its freshly-computed
    value, and every key foreign to it survives untouched at whatever depth it was found. Nested
    maps are merged the same way, so a field invented inside ``delegates`` — or inside one backend's
    entry — is preserved as readily as a top-level one.

    Two exceptions, both because "foreign" is not the same question as "absent from what this run
    computed": :data:`NOT_PERSISTED` names keys this version knows and declines to store, and
    :data:`TRIMMED_MAPS` names maps whose key set the writer owns, where an absent key means
    *evicted* rather than *unknown*.

    Conflating them would make :data:`NOT_PERSISTED` unenforceable — a key deliberately left out would read as one
    from the future and be carried forward anyway. So a key named there is dropped rather than
    preserved, whatever the stored file holds.

    A key present in both wins for ``totals`` even when the stored value was garbage, because
    :func:`normalize_totals` has already turned that garbage into a usable zero; carrying it back
    would undo the coercion this format relies on.

    Args:
        raw: The parsed prior file (see :func:`read_raw_totals`). A non-object is ignored.
        totals: The computed totals to write.
        _path: The path of the object being merged, used to look up :data:`NOT_PERSISTED`.
            Internal — callers start at the root.

    Returns:
        A new dict — ``totals``, plus whatever only ``raw`` had. Neither argument is mutated.
    """
    if not isinstance(raw, dict):
        return dict(totals)
    omitted = NOT_PERSISTED.get(_path, frozenset())
    merged = dict(totals)
    for key, value in raw.items():
        key = str(key)
        if key in omitted:  # known and deliberately unstored — dropping it is the point
            continue
        if key not in merged:
            if _path in TRIMMED_MAPS:
                continue  # the writer owns this key set; absent means evicted, not unknown
            merged[key] = value
        elif isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = carry_unknown_fields(value, merged[key], (*_path, key))
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
