"""Measurement / "spend avoided" rollup.

Every routed task is logged as one JSON line in an append-only usage log, and ``tanglebrain
--stats`` rolls those records up into a "spend avoided" figure: what the routed work *would* have
cost on a paid frontier API, had it not gone to the free local tier or a subscription CLI. This
makes the cloud-equivalent cost avoided by routing visible.

Design notes:

- **Tokens are estimated, not measured.** Authenticated CLIs expose no usable token counts, and a
  local reasoning model's real ``usage`` is inflated by dropped reasoning tokens. So a
  single ``chars/4`` heuristic over the visible prompt + response is applied *uniformly* across all
  tiers — one consistent, if approximate, methodology (see :func:`estimate_tokens`).
- **Pricing is config-driven** (``config/pricing.yaml``): a reference frontier price the operator
  tunes (the knob GUI edits it).
- **State lives in the XDG data tier**, not ``~/.cache`` (see
  :func:`~tanglebrain.router.state_root`). The usage log is not reconstructible, so a
  cache-tier home would have made every historical figure deletable by any cleanup tool.
- **The store has two halves**: permanent lifetime aggregates in ``totals.json``
  (:mod:`tanglebrain.totals`) and a window of per-task rows in the log. :func:`rollup` sums the
  two, so the rows behind the headline can be bounded without the headline moving. Only the
  delegates' ``by_parent`` tree is window-scoped — it has one key per parent task id, which is
  unbounded and so cannot be folded — and the renderers label it as such.
- **Compaction moves rows across that seam** (:func:`compact_log`), and the *order* of its two
  writes is its whole guarantee: totals first, rows dropped only after. A torn compaction then
  **over-counts rather than losing rows** — the figure reads too large. Nothing identifies *which*
  rows were double-counted, so that state is not self-correcting; what the ordering buys is that no
  row is ever destroyed before something records it, which is the one failure this product's
  central claim cannot survive. That residue is an accepted limit, argued at
  :func:`_compact_if_oversized`.
- **The window is capped by size** (:data:`MAX_LOG_BYTES`). Every append checks the log's size and
  folds the oldest rows away once it crosses the cap, so the file is bounded without the lifetime
  figure moving. Size, not age: an age cap takes a light user's whole history and a heavy user's
  nothing, and disk footprint is the cost worth bounding.
- **All I/O is fault-tolerant.** A logging failure must never break the user's actual answer, and a
  corrupt log line must never break the rollup. Reads return sensible defaults; the writer swallows
  every exception. This mirrors the router's state-file idiom (:mod:`tanglebrain.router`).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tanglebrain.atomic import atomic_copy, atomic_write
from tanglebrain.router import state_root
from tanglebrain.totals import (
    BACKEND_INT_FIELDS,
    TOTALS_FILENAME,
    as_float,
    as_int,
    default_totals_path,
    normalize_totals,
    read_raw_totals,
    read_totals,
    write_totals,
)

LOG_FILENAME = "usage.jsonl"

#: Env var carrying the top-level task id from the orchestrator down to a delegated sub-call. The
#: CLI mints a task id per routed task and the orchestrator-CLI adapter injects it into the
#: orchestrator subprocess env (only when the delegate tool is injected); the orchestrator forwards
#: its env to the MCP delegate child it spawns, where :func:`tanglebrain.delegate.run_delegate` reads
#: it back and stamps each delegate record's ``parent_task_id``. This is what links a delegated
#: sub-call to the specific top-level task that spawned it, across the process boundary.
PARENT_TASK_ID_ENV = "TANGLEBRAIN_TASK_ID"

# Serializes appends to the usage log so concurrent writers in one process (delegate_many fans
# sub-tasks out across threads) can't interleave bytes mid-line. Per-process only; cross-process
# appends rely on the OS's O_APPEND atomicity for short lines, as before.
_LOG_LOCK = threading.Lock()

# Held non-blocking around an automatic compaction, so a thread fan-out that all crosses the cap at
# once folds the log once rather than N times. Not a correctness lock — `compact_log` takes
# `_LOG_LOCK` for its whole body — and never waited on: a thread that finds a fold already running
# has nothing to add by queueing behind it.
_COMPACT_LOCK = threading.Lock()

#: Byte ceiling on the row window. Once an append pushes the log past this, the oldest rows fold
#: into the lifetime totals and leave the file (:func:`_compact_if_oversized`), so the log is
#: bounded while the spend-avoided figure stays exactly where it was.
#:
#: **Size, not age.** An age cap is regressive — a light user loses a whole history to the calendar
#: while a heavy user loses nothing — and disk footprint is the cost a cap exists to bound.
#:
#: 5 MiB is roughly 15,000 records: rows measure 240-330 bytes on a real log, and the widest shape
#: the current record can take (``task_id`` and ``origin`` present, a long ``pricing_ref``) sits at
#: the top of that range. Large enough that a heavy user keeps months of per-task detail, small
#: enough that the file is never a surprise in a home directory. A stated default rather than a
#: magic number: the behaviour is read from here, so changing it here changes the behaviour.
MAX_LOG_BYTES = 5 * 1024 * 1024

#: What a compaction leaves behind, as a byte budget over the newest rows — ~1 MiB, roughly 3,000
#: records, enough recent history that the window-scoped halves of the rollup (the per-parent
#: delegate tree, the backend split) still answer a question.
#:
#: Expressed in the same unit as :data:`MAX_LOG_BYTES` and strictly smaller on purpose: a
#: compaction then cannot leave the log still over the cap, and the gap between the two is the
#: hysteresis that stops every following append from triggering another whole-file rewrite.
KEEP_RECENT_BYTES = 1024 * 1024


class CompactionRefusedError(RuntimeError):
    """Raised when compaction declines to run because folding would destroy recoverable state.

    The condition is a lifetime totals file that is present but cannot be read back as an object —
    its bytes are unparseable, or the file cannot be read at all. Distinct from an ``OSError``:
    nothing failed and nothing was written. The store is in a state where the *safe* action is to
    leave both halves on disk, so the caller is told rather than quietly given a smaller number.
    Callers that must not break a user's answer should treat it the way :func:`record_task` treats
    a logging failure — the rows are intact, and a later run can still fold them.
    """


# Chars per token for the uniform estimation heuristic. ~4 chars/token is the standard rough
# approximation for English-ish text across modern BPE tokenizers; good enough for an *estimate*.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Pricing:
    """Cloud-equivalent reference pricing for the rollup (loaded from ``config/pricing.yaml``).

    Attributes:
        reference_model: Human-readable label for the frontier model these rates represent.
        input_per_mtok: US dollars per 1,000,000 input (prompt) tokens.
        output_per_mtok: US dollars per 1,000,000 output (completion) tokens.
        is_placeholder: ``True`` while the rates are rough/illustrative; the rollup renders a
            PLACEHOLDER caveat so no figure is mistaken for a precise cost.
    """

    reference_model: str
    input_per_mtok: float
    output_per_mtok: float
    is_placeholder: bool


# Fallback used when ``config/pricing.yaml`` is missing or unreadable — always flagged placeholder.
PLACEHOLDER_PRICING = Pricing(
    reference_model="unconfigured (PLACEHOLDER — pricing.yaml unreadable)",
    input_per_mtok=3.00,
    output_per_mtok=15.00,
    is_placeholder=True,
)


def default_log_path() -> Path:
    """Return the usage-log file path.

    Resolves under :func:`~tanglebrain.router.state_root` — the data tier, not the cache tier.
    This log is the only record of the lifetime spend-avoided figure and is not reconstructible
    (prompt and response text is never persisted, by design), so it cannot live anywhere a
    cleaner is entitled to delete.

    Returns:
        The absolute path to the append-only usage JSONL file.
    """
    return state_root() / LOG_FILENAME


def default_pricing_path() -> Path:
    """Return the path to the pricing YAML shipped with the package.

    Returns:
        The absolute path to ``tanglebrain/config/pricing.yaml``.
    """
    return Path(__file__).resolve().parent / "config" / "pricing.yaml"


def load_pricing(path: str | os.PathLike[str] | None = None) -> Pricing:
    """Load cloud-equivalent reference pricing, tolerating a missing/corrupt file.

    Args:
        path: Path to a pricing YAML. Defaults to the packaged ``config/pricing.yaml``.

    Returns:
        The parsed :class:`Pricing`, or :data:`PLACEHOLDER_PRICING` if the file is absent,
        unreadable, or malformed — bad config must never crash the rollup.
    """
    pricing_path = Path(path) if path is not None else default_pricing_path()
    try:
        raw = yaml.safe_load(pricing_path.read_text())
        return Pricing(
            reference_model=str(raw.get("reference_model", "unknown")),
            input_per_mtok=float(raw["input_per_mtok"]),
            output_per_mtok=float(raw["output_per_mtok"]),
            is_placeholder=bool(raw.get("placeholder", False)),
        )
    except (OSError, yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError):
        return PLACEHOLDER_PRICING


# Fallback header, used ONLY when the target file is absent (e.g. a fresh write to a new path).
# A normal save preserves the existing file's own leading comment block verbatim (see
# :func:`_leading_comment_block`), so the curated methodology note is never replaced or drifted.
PRICING_HEADER = """\
# Cloud-equivalent reference pricing for the "spend avoided" rollup.
#
# Methodology: for each routed task, estimate what it WOULD have cost on a paid frontier API, valued
# at a reference model's per-million-token price. The rollup multiplies estimated tokens (a chars/4
# heuristic over the visible prompt + response) by these rates. Values are US dollars per 1,000,000
# tokens.
#
# `placeholder: true` makes `tanglebrain --stats` flag every figure as PLACEHOLDER (use it if you
# fork these reference rates before re-checking them). Edited via `tanglebrain-gui` or by hand.
"""


def _leading_comment_block(text: str) -> str:
    """Return the file's leading run of comment/blank lines (its header), or ``""`` if none.

    Used to preserve a pricing file's curated header verbatim across a save, so no documentation
    is lost or replaced. Stops at the first non-comment, non-blank line (the first YAML key).
    """
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            out.append(line)
        else:
            break
    while out and not out[-1].strip():  # drop trailing blank lines before the keys
        out.pop()
    return "\n".join(out) + "\n" if out else ""


def validate_pricing(data: dict) -> Pricing:
    """Strictly validate raw pricing fields and build a :class:`Pricing`.

    Unlike :func:`load_pricing` (lenient — bad reads fall back to a placeholder), this rejects
    invalid input so the panel never persists garbage.

    Args:
        data: ``{reference_model, input_per_mtok, output_per_mtok, placeholder}``.

    Returns:
        A validated :class:`Pricing`.

    Raises:
        ValueError: If a field is missing, the wrong type, a non-finite/negative rate, or an
            empty ``reference_model``.
    """
    if not isinstance(data, dict):
        raise ValueError("pricing must be an object")

    model = data.get("reference_model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("reference_model must be a non-empty string")

    rates = {}
    for key in ("input_per_mtok", "output_per_mtok"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf guard
            raise ValueError(f"{key} must be finite")
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
        rates[key] = value

    placeholder = data.get("placeholder", False)
    if not isinstance(placeholder, bool):
        raise ValueError("placeholder must be a boolean")

    return Pricing(
        reference_model=model.strip(),
        input_per_mtok=rates["input_per_mtok"],
        output_per_mtok=rates["output_per_mtok"],
        is_placeholder=placeholder,
    )


def _backup_dir() -> Path:
    """Return the directory for config backups (under the state root, never the repo config dir).

    Data tier, like the rest of the state root: a backup is the only copy of a config the operator
    hand-edited, so a cleaner deleting it defeats the entire point of taking one.
    """
    return state_root() / "backups"


def _render_pricing(pricing: Pricing, header: str) -> str:
    """Render a :class:`Pricing` to YAML text beneath ``header``.

    ``reference_model`` is emitted via ``json.dumps`` — a JSON string is valid YAML and safely
    quotes/escapes any colons, quotes, unicode, or backslashes. Float rates use Python's ``repr``
    (valid YAML), which round-trips exactly through :func:`load_pricing`.
    """
    return (
        header
        + f"placeholder: {str(pricing.is_placeholder).lower()}\n"
        + f"reference_model: {json.dumps(pricing.reference_model)}\n"
        + f"input_per_mtok: {pricing.input_per_mtok}\n"
        + f"output_per_mtok: {pricing.output_per_mtok}\n"
    )


def save_pricing(pricing: Pricing, path: str | os.PathLike[str] | None = None) -> None:
    """Persist pricing to the config YAML — header-preserving, with a backup, written atomically.

    Preserves the target's existing leading comment block verbatim (falling back to
    :data:`PRICING_HEADER` only when the file is absent), backs up any existing file to
    ``<state_dir>/backups/pricing-<ts>.yaml``, then atomically replaces the target.

    Args:
        pricing: The validated pricing to write (see :func:`validate_pricing`).
        path: Target YAML path. Defaults to the packaged ``config/pricing.yaml``.
    """
    target = Path(path) if path is not None else default_pricing_path()
    header = PRICING_HEADER
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        block = _leading_comment_block(existing)
        if block.strip():
            header = block  # keep the curated header verbatim — no drift, no doc loss
        backup_dir = _backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")  # sub-second: no same-second collision
        # Staged and renamed, not copied straight to the final name: an interrupted copy would
        # otherwise leave a truncated file wearing a backup's name, and a backup is read exactly
        # when the original is already gone.
        atomic_copy(target, backup_dir / f"pricing-{stamp}.yaml")
    atomic_write(target, _render_pricing(pricing, header))


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` via the uniform ``chars/4`` heuristic.

    This is an approximation, applied identically to every tier (CLI subs expose no real counts).
    Empty/falsy text counts as 0; any non-empty text is at least 1 token.

    Args:
        text: The prompt or response text.

    Returns:
        The estimated token count (``>= 0``).
    """
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def cloud_equiv_usd(in_tokens: int, out_tokens: int, pricing: Pricing) -> float:
    """Compute the cloud-equivalent cost of a task at the reference frontier price.

    Args:
        in_tokens: Estimated input (prompt) tokens.
        out_tokens: Estimated output (completion) tokens.
        pricing: The reference pricing to apply.

    Returns:
        The estimated US-dollar cost on the reference frontier API.
    """
    return (
        in_tokens / 1_000_000 * pricing.input_per_mtok
        + out_tokens / 1_000_000 * pricing.output_per_mtok
    )


def record_task(
    *,
    path: str,
    entry: object,
    prompt: str,
    response: str,
    kind: str = "task",
    task_id: str | None = None,
    parent_task_id: str | None = None,
    origin: str | None = None,
    failures: list[tuple[str, str]] | None = None,
    log_path: str | os.PathLike[str] | None = None,
    pricing: Pricing | None = None,
) -> None:
    """Append one usage record for a routed task or a delegated sub-call. Never raises.

    A logging failure is dropped — measurement is a side-effect that must never affect the returned
    answer.

    Args:
        path: Which execution path served the work — ``router`` | ``local`` | ``model`` |
            ``delegate``.
        entry: The served :class:`~tanglebrain.roster.RosterEntry` (read for ``tier``/``id``); may
            be ``None`` (e.g. the router didn't surface one), in which case both are ``"unknown"``.
        prompt: The prompt (for input-token estimation).
        response: The returned response text (for output-token estimation).
        kind: ``"task"`` for a top-level routed task (the default; what the spend-avoided headline
            counts), ``"delegate"`` for a delegated sub-call, or ``"failure"`` for a task no
            backend served (#100). Delegate and failure records are rolled up **separately** so a
            sub-call's saving is never double-counted and a failed task never inflates the headline.
        task_id: For a top-level task, the id minted for this routed task (so its delegated sub-calls
            can be linked back to it). Omitted from the record when ``None``.
        parent_task_id: For a delegated sub-call, the id of the top-level task that spawned it (read
            from :data:`PARENT_TASK_ID_ENV`). Omitted from the record when ``None`` — e.g. a delegate
            invoked outside a propagated task, which rolls up as ``unlinked``. For a top-level task,
            an external caller's own task/session identity (#74: the serve endpoint's
            ``X-TangleBrain-Parent-Task`` header) — pure attribution metadata; the delegate tree's
            ``by_parent`` rollup reads it only off ``delegate`` records.
        origin: Which surface the work entered through — ``"cli"`` | ``"gui"`` | ``"serve"``
            (#74). Omitted from the record when ``None``; records without it roll up as
            ``untagged`` (pre-#74 history is never guessed at).
        failures: The ``(entry_id, error)`` attempts that failed before this outcome (#100): the
            lost failover attempts on a served task, or every attempt on a ``"failure"`` record.
            Omitted from the record when empty/``None``, so first-try history keeps its shape.
        log_path: Override the usage-log path (tests inject a temp path). Defaults to
            :func:`default_log_path`.
        pricing: Override the pricing. Defaults to :func:`load_pricing`.
    """
    try:
        if pricing is None:
            pricing = load_pricing()
        tier = getattr(entry, "tier", None) or "unknown"
        model = getattr(entry, "id", None) or "unknown"
        in_tok = estimate_tokens(prompt)
        out_tok = estimate_tokens(response)
        equiv = cloud_equiv_usd(in_tok, out_tok, pricing)
        # A paid `api` task incurs real spend, so it avoids nothing (avoided = 0); every other tier
        # routes work off a paid frontier API, so it avoids the full cloud-equivalent. A failed
        # task produced no answer anywhere, so it likewise avoids nothing.
        avoided = 0.0 if tier == "api" or kind == "failure" else equiv
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": str(kind),
            "path": str(path),
            "tier": str(tier),
            "model": str(model),
            "in_tokens_est": in_tok,
            "out_tokens_est": out_tok,
            "cloud_equiv_usd": round(equiv, 6),
            "spend_avoided_usd": round(avoided, 6),
            "pricing_ref": pricing.reference_model,
        }
        # Optional linkage fields — only written when present, so existing records/readers that
        # never set them are unaffected (a missing field reads as "no linkage").
        if task_id is not None:
            record["task_id"] = str(task_id)
        if parent_task_id is not None:
            record["parent_task_id"] = str(parent_task_id)
        if origin is not None:
            record["origin"] = str(origin)
        # Written only when attempts were actually lost (never as an empty list), and only from a
        # real sequence — a reader predating the field stays correct (#100).
        if isinstance(failures, (list, tuple)) and failures:
            record["failures"] = [{"entry": str(eid), "error": str(err)} for eid, err in failures]
        target = Path(log_path) if log_path is not None else default_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        # Outside the lock deliberately — `_LOG_LOCK` is not reentrant and compaction holds it for
        # its whole body, so triggering from inside would deadlock rather than raise.
        _compact_if_oversized(target)
    except Exception:  # noqa: BLE001
        # Measurement is a side-effect: a failure here must never affect the returned answer.
        return


def _read_lines(log_path: str | os.PathLike[str] | None = None) -> list[tuple[str, dict | None]]:
    """Read the log as ``(raw line, parsed record or None)`` pairs, in chronological order.

    :func:`read_records` wants the records and :func:`compact_log` wants the lines — compaction
    copies the rows it keeps through **unparsed**, so an unknown field and an unparseable line
    alike survive in the retained window. A line before the cut is folded away with the rest of its
    span whether or not it parsed. Both callers go through this so there is a single definition of
    what counts as a row.

    Args:
        log_path: Override the usage-log path. Defaults to :func:`default_log_path`.

    Returns:
        One pair per non-blank line, the second element ``None`` when the line is not a JSON
        object. An absent or unreadable log yields ``[]``.
    """
    target = Path(log_path) if log_path is not None else default_log_path()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines: list[tuple[str, dict | None]] = []
    for raw in text.splitlines():
        if not raw.strip():  # blank lines are not rows; nothing carries them forward
            continue
        try:
            # `json.loads` tolerates surrounding whitespace itself, so the line is parsed as
            # found — stripping here would make the "verbatim" the rewrite promises a near-miss.
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None
        lines.append((raw, obj if isinstance(obj, dict) else None))
    return lines


def read_records(log_path: str | os.PathLike[str] | None = None) -> list[dict]:
    """Read all usage records from the log, skipping malformed lines.

    Args:
        log_path: Override the usage-log path. Defaults to :func:`default_log_path`.

    Returns:
        The parsed records in file (chronological) order. An absent log yields ``[]``.
    """
    return [record for _, record in _read_lines(log_path) if record is not None]


def _accumulate(records: list[dict], totals: dict | None) -> dict:
    """Sum stored lifetime totals and a batch of records — the summation shared by both callers.

    :func:`rollup` renders this for a reader and :func:`fold_records_into_totals` persists it, and
    they must agree exactly or compaction would move the headline. One body is what makes that
    true by construction rather than by a test that has to keep noticing.

    Money is deliberately **not** rounded here. Rounding is presentation, and applying it to a
    value that is about to be added to again turns each fold into a fresh 5e-5 of drift. Because
    the fold takes the oldest rows first, the additions happen in the same order either way, so a
    figure read straight from the rows and the same figure read after any number of folds are
    bit-identical rather than merely close.

    Args:
        records: The records to add — the current row window, or the batch being folded away.
        totals: Stored lifetime aggregates, normalized here rather than trusted (see
            :func:`rollup`). ``None`` reads as all-zeros.

    Returns:
        The unrounded summary, including the window-scoped ``delegates.by_parent`` tree.
    """
    # Normalizing here rather than trusting the argument makes this function total for *any*
    # caller: `None`, a partial dict, a file written by a newer version. It is the function whose
    # failure blanks the product's headline, so it does not get to depend on being handed a
    # well-formed dict. `normalize_totals` also builds a fresh structure, which is what keeps the
    # window's rows from accumulating into a caller's own totals dict below.
    stored = normalize_totals(totals)
    # Popped rather than listed in an exclusion tuple: a hand-maintained list of "keys handled
    # separately" is a third place the field list has to agree, and adding a name to it would drop
    # a lifetime figure out of the summary in silence.
    summary: dict = dict(stored)
    pricing_refs: set[str] = set(summary.pop("pricing_refs"))
    delegates: dict = dict(summary.pop("delegates"))
    # Window-scoped, and the only figure here that is: one key per parent task id is unbounded, so
    # it is never folded into the stored totals and always starts empty.
    delegates["by_parent"] = {}
    for r in records:
        in_tok = as_int(r.get("in_tokens_est"))
        out_tok = as_int(r.get("out_tokens_est"))
        lost = r.get("failures")
        summary["lost_attempts"] += len(lost) if isinstance(lost, list) else 0
        if str(r.get("kind", "task")) == "failure":
            # Held out of the headline like delegates: a failed task avoided no spend (#100).
            summary["failures"] += 1
            continue
        # Collected from here down, so a record widens the span exactly when it reaches a figure
        # in this summary — every kind but a failure record, which is discarded above. A delegate's
        # cloud-equiv is rendered in the block. An `api` task avoided nothing and its cloud-equiv is
        # summed into `cloud_equiv_usd`, which no renderer prints today, but the task still lands in
        # the task count, the tier split and the token estimates, all of which are rendered. A
        # failure record enters none of them, so letting it contribute would caveat a figure it
        # never touched. Over-inclusion is the safe direction for a caveat: one that stays silent
        # over a mixed figure is the defect it exists to prevent.
        ref = r.get("pricing_ref")
        if ref not in (None, ""):
            pricing_refs.add(str(ref))
        if str(r.get("kind", "task")) == "delegate":
            delegates["count"] += 1
            model = str(r.get("model", "unknown"))
            backend = delegates["by_backend"].setdefault(
                model, {field: 0 for field in BACKEND_INT_FIELDS}
            )
            backend["count"] += 1
            backend["in_tokens_est"] += in_tok
            backend["out_tokens_est"] += out_tok
            # Per-parent tree: link this sub-call to the top-level task that spawned it. A delegate
            # with no parent_task_id (run outside a propagated task) groups under "unlinked".
            parent_id = r.get("parent_task_id")
            parent_key = str(parent_id) if parent_id not in (None, "") else "unlinked"
            parent = delegates["by_parent"].setdefault(parent_key, {"count": 0, "by_backend": {}})
            parent["count"] += 1
            parent["by_backend"][model] = parent["by_backend"].get(model, 0) + 1
            delegates["in_tokens_est"] += in_tok
            delegates["out_tokens_est"] += out_tok
            delegates["cloud_equiv_usd"] += as_float(r.get("cloud_equiv_usd"))
            continue
        summary["tasks"] += 1
        tier = str(r.get("tier", "unknown"))
        summary["by_tier"][tier] = summary["by_tier"].get(tier, 0) + 1
        origin = str(r.get("origin") or "untagged")
        summary["by_origin"][origin] = summary["by_origin"].get(origin, 0) + 1
        summary["in_tokens_est"] += in_tok
        summary["out_tokens_est"] += out_tok
        summary["cloud_equiv_usd"] += as_float(r.get("cloud_equiv_usd"))
        summary["spend_avoided_usd"] += as_float(r.get("spend_avoided_usd"))
    summary["pricing_refs"] = sorted(pricing_refs)
    summary["delegates"] = delegates
    return summary


def rollup(records: list[dict], totals: dict | None = None) -> dict:
    """Aggregate stored lifetime totals plus the current row window into one summary.

    Every figure below is a lifetime figure — stored totals plus the rows still on disk — with the
    single exception of the delegates' ``by_parent`` tree, which is unbounded and therefore
    describes the window alone (see :mod:`tanglebrain.totals`). Rendering a window-scoped split
    beside a lifetime headline without saying which is which is the contradiction this split
    exists to avoid, so the renderers label it.

    Args:
        records: The records from :func:`read_records` — the current row window.
        totals: Stored lifetime aggregates from :func:`~tanglebrain.totals.read_totals`. Defaults
            to all-zeros, which makes the result identical to a window-only rollup: a log with no
            ``totals.json`` has had nothing folded away, so its rows *are* its lifetime.

    Returns:
        A dict with: ``tasks`` (int), ``by_tier`` (tier → count), ``by_origin`` (origin → count,
        where a record without an ``origin`` field counts as ``untagged`` — pre-#74 history is
        never guessed at), ``in_tokens_est`` / ``out_tokens_est`` (summed estimates), and
        ``cloud_equiv_usd`` / ``spend_avoided_usd``
        (summed dollars) — all over **top-level tasks only** — plus ``delegates``, a separate
        sub-rollup of delegated sub-calls ``{count, by_backend: {model: {count, in_tokens_est,
        out_tokens_est}}, by_parent: {parent_task_id: {count, by_backend: {model: count}}},
        in_tokens_est, out_tokens_est, cloud_equiv_usd}``. ``by_parent`` groups each delegate under
        the top-level task that spawned it (via ``parent_task_id``); delegates with no
        ``parent_task_id`` are grouped under the sentinel ``"unlinked"``. Delegate records are kept
        out of the headline so a sub-call's saving is never double-counted against its parent task;
        their cloud-equiv is informational. A record without a ``kind`` field counts as a task.

        Also ``failures`` (count of ``kind: "failure"`` records — tasks no backend served) and
        ``lost_attempts`` (total failed attempts across all records: every attempt on a failure
        record plus the lost failovers behind eventual successes). Failure records are held out
        of the headline like delegates — a failed task avoided no spend (#100).

        Also ``pricing_refs``: the sorted, de-duplicated reference-pricing revisions this figure
        spans, merged from the stored totals and from the rows that contributed money to it. It is
        collected here rather than derived later because compaction destroys the per-row evidence.
    """
    summary = _accumulate(records, totals)
    # Rounded once, at the edge: these figures are read by `format_rollup` and by the GUI
    # panel's JSON, and neither wants a float's full tail. The fold reads `_accumulate`
    # directly so no stored value is ever rounded before it is added to again.
    summary["cloud_equiv_usd"] = round(summary["cloud_equiv_usd"], 4)
    summary["spend_avoided_usd"] = round(summary["spend_avoided_usd"], 4)
    summary["delegates"]["cloud_equiv_usd"] = round(summary["delegates"]["cloud_equiv_usd"], 4)
    return summary


def fold_records_into_totals(records: list[dict], totals: dict | None = None) -> dict:
    """Add a batch of rows into the lifetime totals, returning the new totals.

    The fold performs the *same* summation :func:`rollup` performs, through the same body, so a
    figure cannot change simply because rows moved from the window into the store. The one
    difference is the delegates' ``by_parent`` tree, which is dropped: it holds one key per parent
    task id, so folding it would grow the totals file without bound. That tree is window-scoped by
    definition, and every renderer says so.

    Pure — no file is read or written. :func:`compact_log` is what persists the result.

    Args:
        records: The rows being folded away.
        totals: The stored totals to add them to. ``None`` reads as all-zeros.

    Returns:
        A new totals dict in the shape :mod:`tanglebrain.totals` defines.
    """
    folded = _accumulate(records, totals)
    folded["delegates"].pop("by_parent", None)
    return folded


def _rewrite_log(log_path: Path, lines: list[str]) -> None:
    """Replace the usage log with exactly ``lines`` — the second, destructive half of a compaction.

    Its own function because the *ordering* around it is the guarantee compaction makes, and a
    seam that can be made to fail is what lets a test prove rows are never dropped before the
    totals that replace them have landed.

    Args:
        log_path: The usage log to replace.
        lines: The rows to keep, verbatim, without trailing newlines.
    """
    atomic_write(log_path, "".join(line + "\n" for line in lines))


def compact_log(
    *,
    keep_recent: int,
    log_path: str | os.PathLike[str] | None = None,
    totals_path: str | os.PathLike[str] | None = None,
) -> int:
    """Fold every row but the most recent ``keep_recent`` into the lifetime totals, then drop them.

    **The order of the two writes is the whole point.** The totals file is written first and the
    rows are removed only once that write has landed. An interrupted compaction therefore leaves
    rows counted in *both* halves, so the figure reads **too large**. The opposite order would drop
    rows before anything recorded them: a smaller figure, no evidence, nothing left to recompute
    from. Over-counting is a bug; under-counting is the loss of the only claim this product makes
    about itself, so the recoverable direction is the only one reachable.

    **What that does not buy:** a folded row is byte-identical to an unfolded one, and nothing here
    records a watermark, a fold count or a timestamp — so an inflated figure is not *attributable*
    and does not correct itself. That is an accepted limit, argued where the automatic trigger
    lives (:func:`_compact_if_oversized`). A *failed* fold puts the totals back, so it is bounded
    at one batch whenever that rollback lands; the two states where it is not are a crash, which
    runs no code, and a rollback that fails in its turn — both leave the log over its cap, so the
    next recorded task re-folds.

    Nothing is written at all when there is nothing to fold, so a short log leaves both files
    exactly as they were rather than materializing a zeroed totals file beside it.

    Rows that are kept are written back **verbatim**, so neither a field added by a newer
    TangleBrain nor a line left torn by an interrupted append is lost to the operation that
    rewrites the file.

    **Concurrency.** ``_LOG_LOCK`` is held across the whole read-fold-truncate, so an append from
    another thread of this process (``delegate_many`` fans out) cannot land between the read and
    the rewrite. It cannot cover *another process*, in two ways: a second TangleBrain appending
    during those milliseconds writes to the file being replaced and that row is lost, and two
    compactions overlapping read the same totals and the later write discards the earlier fold
    wholesale. Accepted for a single-operator local tool, and stated rather than papered over — an
    advisory lock would buy a guarantee on POSIX only, and a guarantee that silently does not hold
    on one supported platform is worse than a limitation written down. What the size cap changed is
    **frequency, not window width**: compaction went from one deliberate call to a check on every
    recorded task, in every process, and ``_COMPACT_LOCK`` serializes only the threads of one of
    them. Two TangleBrain processes crossing the cap together is therefore reachable where it
    previously took an operator running two maintenance calls at once. It is still a
    single-operator tool and the window is still the milliseconds of one fold, so the trade stands
    — but it stands on a frequency this chunk raised, not on the one it was first weighed against.

    Args:
        keep_recent: How many of the newest rows to leave in the log. ``0`` folds everything.
        log_path: Override the usage-log path. Defaults to :func:`default_log_path`.
        totals_path: Override the totals path. Defaults to
            :func:`~tanglebrain.totals.default_totals_path`.

    Returns:
        The number of rows removed from the log — ``0`` when the window was already short enough.
        A line too malformed to parse is removed with the rest of its span and counted here; it
        contributed nothing to any figure, so nothing is folded in its place.

    Raises:
        ValueError: If ``keep_recent`` is negative.
        CompactionRefusedError: If the totals file exists but cannot be read back as a totals
            object — unparseable, or unreadable at all. Nothing is written; the damaged file and
            every row are left where they are.
        OSError: If either write fails. Both files are left as they were: a failed totals write
            never reaches the log, and a failed log rewrite puts the totals back. Never a lossy
            state in either direction.
    """
    if keep_recent < 0:
        raise ValueError("keep_recent must be >= 0")
    log = Path(log_path) if log_path is not None else default_log_path()
    totals_file = Path(totals_path) if totals_path is not None else default_totals_path()
    with _LOG_LOCK:
        lines = _read_lines(log)
        if keep_recent >= len(lines):
            return 0
        # A totals file that is present but unreadable reads as zeros, and folding onto zeros
        # would overwrite the damaged bytes and *then* delete the rows that could have reconciled
        # them — turning a bad-but-recoverable store into a permanent under-count without a crash.
        # Reading as zeros is right for a rollup, which only renders; it is wrong for a writer,
        # which destroys. Refusing keeps both halves on disk. The log grows meanwhile, which is a
        # smaller problem than a wrong headline.
        # `read_raw_totals` returns `None` for unreadable bytes as well as unparseable ones, so
        # this covers both — the file is present and cannot be trusted either way.
        if totals_file.exists() and not isinstance(read_raw_totals(totals_file), dict):
            raise CompactionRefusedError(
                f"{totals_file} exists but cannot be read as a totals object; refusing to fold "
                f"{len(lines) - keep_recent} row(s) onto it. Move or repair the file first — the "
                f"rows are still in {log} and no figure has been lost."
            )
        cut = len(lines) - keep_recent
        folding = [record for _, record in lines[:cut] if record is not None]
        keeping = [raw for raw, _ in lines[cut:]]
        snapshot = _totals_snapshot(totals_file)
        write_totals(fold_records_into_totals(folding, read_totals(totals_file)), totals_file)
        try:
            _rewrite_log(log, keeping)
        except OSError:
            # The totals now hold rows the log also still holds. With one manual caller that was a
            # one-shot over-count; under an automatic trigger the log is *still over its cap*, so
            # the next recorded task folds the same rows onto the already-inflated total, and the
            # one after that again — a figure growing by a whole batch per task, from a failure
            # that repeats (a full disk fails the megabyte-scale log rewrite while the few-hundred
            # -byte totals write and append still succeed). Putting the totals back makes a failed
            # fold a no-op instead: nothing was destroyed, because the rows are exactly where they
            # were.
            _restore_totals(totals_file, snapshot)
            raise
    return cut


def _totals_snapshot(totals_file: Path) -> tuple[bool, str | None]:
    """Capture the totals file verbatim, so a fold that cannot finish can be undone exactly.

    Text rather than a parsed value: restoring a re-serialization would silently rewrite a field
    this version does not define, and the whole point of the snapshot is that the store ends where
    it started.

    **"Absent" and "could not be read" are returned as different things**, because the rollback
    does opposite work for them — delete, versus leave alone. Collapsing both into "no content"
    would make a file this function merely failed to *read* a file the rollback *deletes*, which is
    the under-count direction the store cannot survive. Unreachable today (the refusal guard runs
    first, under the same lock), so this is the cheap half of a rule rather than a fix for a live
    bug — the expensive half is discovering later that it stopped being unreachable.

    Args:
        totals_file: The lifetime totals path.

    Returns:
        ``(the file existed, its text or None)``. The text is ``None`` only when the file could not
        be read — absent, or present and unreadable, which the flag tells apart.
    """
    try:
        return True, totals_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError: bytes that are not UTF-8 are unreadable here in
        # exactly the sense that matters, and must not be mistaken for an absent file.
        return True, None


def _restore_totals(totals_file: Path, snapshot: tuple[bool, str | None]) -> None:
    """Undo a totals write whose paired log rewrite failed. Never raises.

    Best-effort by design. It runs while an ``OSError`` is already propagating, and that error is
    the one the caller needs to see — a second exception raised from here would replace the
    diagnosis with the symptom. If the restore itself fails, the store is left over-counting and
    the log stays over its cap, so the *next* recorded task re-folds: the unbounded case the
    rollback exists to prevent, back again. Accepted because the restore asks far less of a failing
    disk than the write that failed — a few hundred bytes, or a delete — and because the
    alternative, deleting rows to match, is the direction that loses the figure entirely.

    Args:
        totals_file: The lifetime totals path.
        snapshot: The pair from :func:`_totals_snapshot`.
    """
    existed, previous = snapshot
    try:
        if previous is not None:
            atomic_write(totals_file, previous)
        elif not existed:
            totals_file.unlink(missing_ok=True)   # the failed fold created it; take it back out
        # existed and unreadable: leave it. Over-counting is recoverable; deleting a totals file
        # this function could not read is not.
    except OSError:
        return


def _keep_recent_for_budget(log: Path, budget: int) -> int:
    """Count the newest rows whose bytes fit inside ``budget``.

    Turns the byte budget the cap is stated in into the row count :func:`compact_log` takes. Doing
    that arithmetic here rather than teaching compaction a second unit leaves its contract — fold
    everything but the newest N rows — exactly as it was, and keeps the retention policy in one
    place with the cap it is derived from.

    Args:
        log: The usage log to measure.
        budget: How many bytes of the newest rows to keep.

    Returns:
        The number of trailing rows that fit; ``0`` when not even the newest row does, which folds
        the log empty rather than leaving a row the budget does not cover.
    """
    kept = 0
    used = 0
    for raw, _ in reversed(_read_lines(log)):
        used += len(raw.encode("utf-8")) + 1  # +1 for the newline the rewrite puts back
        if used > budget:
            break
        kept += 1
    return kept


def _compact_if_oversized(log: Path) -> None:
    """Fold the oldest rows away once the log has grown past :data:`MAX_LOG_BYTES`. Never raises.

    The automatic half of compaction: :func:`compact_log` does the work and this decides when. The
    check is one ``stat`` per recorded task, which costs nothing beside the append it follows; the
    fold runs only on the append that crosses the cap, and leaves the log inside
    :data:`KEEP_RECENT_BYTES` so the next one is a whole window away.

    **Every failure is swallowed here rather than by the caller's blanket ``except``.** Compaction
    raises by design — ``OSError`` on a failed write, :exc:`CompactionRefusedError` when the totals
    file is present but unusable — and measurement is a side-effect that must never break the
    answer (`nonfunctional-requirements.md` § Direction). Catching them at the trigger keeps the
    caller's own swallow meaning only "the append failed". A refused fold leaves both halves on
    disk and the log keeps growing until the unusable ``totals.json`` behind it is repaired.

    **The over-count a torn fold leaves is an accepted limit, and this is what makes it reachable
    with no operator present.** Compaction orders its two writes so an interruption counts rows in
    both halves and the figure reads too large, but nothing records *which* rows, so the state is
    neither attributable nor self-correcting. A persisted watermark was considered and rejected:
    every form of it has to answer "are the rows in front of me already counted", and each way of
    answering fails toward *under*-counting — a ``ts`` is second-resolution and shared by rows on
    both sides of a cut, ``task_id`` is optional and absent from most rows, and a digest of the
    folded prefix races the appends it would be compared against. That trades a vanishing event
    (a power loss inside the microseconds between two fsynced writes) for a permanent hazard on
    every read, in the one direction this product's central claim cannot survive.

    **What keeps it to one batch is the rollback, not the odds.** A *failed* fold puts the totals
    back (:func:`compact_log`), which matters far more here than it did when compaction was a
    manual call: the log stays over its cap either way, so without the rollback a failure that
    repeats — a full disk fails the megabyte-scale log rewrite while the small totals write and the
    append still succeed — would re-fold the same rows on every recorded task and grow the figure
    without limit. With it, two states still leave rows counted twice, and they are not the same
    size. A **crash** runs no code, so nothing rolls back — but the next run's fold completes and
    truncates, which caps the damage at one batch. A **rollback that fails in its turn**
    (:func:`_restore_totals` swallows its own ``OSError``, so the caller sees the real diagnosis)
    leaves the totals inflated and the log over its cap, and that is the unbounded case again:
    every following task re-folds. It is far less likely than the write it follows — putting back a
    few hundred bytes, or deleting them, asks much less of a failing disk than rewriting a megabyte
    of log — and it is the honest limit, written down rather than implied.

    Args:
        log: The usage log just appended to. The lifetime totals are taken from beside it: the two
            files are one store and resolve together under a single path override.
    """
    try:
        if log.stat().st_size <= MAX_LOG_BYTES:
            return
    except OSError:
        return  # the log went away between the append and the check; nothing to fold
    if not _COMPACT_LOCK.acquire(blocking=False):
        return  # another thread of this process is already folding this log
    try:
        compact_log(
            keep_recent=_keep_recent_for_budget(log, KEEP_RECENT_BYTES),
            log_path=log,
            totals_path=log.parent / TOTALS_FILENAME,
        )
    except (OSError, CompactionRefusedError):
        return
    finally:
        _COMPACT_LOCK.release()


def format_rollup(summary: dict, pricing: Pricing) -> str:
    """Render a rollup summary as a human-readable block for the CLI.

    **The reference-pricing label describes the figure, not the configuration.** Each record was
    priced when it ran and a ``config/pricing.yaml`` edit never restates history, so a figure summed
    across an edit genuinely has no single revision behind it. The line reports the revision the
    summary actually spans, or how many it spans when that is more than one, with a note saying why
    a span is expected rather than wrong. Current pricing labels the line only when the summary
    carries no revision evidence at all — an empty log, or rows written before ``pricing_ref``
    existed — where there is nothing truer to print.

    The revisions are counted, never listed. A reader wants one number with an honest caveat on it;
    a headline partitioned by pricing revision is correct and unreadable.

    **What a span is evidence of, exactly.** ``pricing_ref`` records the reference-model *label*,
    which is the only part of a pricing revision a record carries, so a span proves the pricing
    config was edited — not that the rates moved. Editing the label alone raises the caveat over a
    figure nothing changed underneath, and editing the rates while leaving the label alone moves a
    figure this line cannot see. Hence the wording below: it reports an edit, not a rate change,
    and the absence of a span is not a claim that the rates held. Widening the record to carry the
    rates would catch both and is an accepted limit rather than open work — this line exists to stop
    one label being asserted over a mixed history, which it does.

    Args:
        summary: The aggregate from :func:`rollup`.
        pricing: The currently-configured pricing — the placeholder caveat, and the fallback
            reference-model label described above.

    Returns:
        A multi-line string suitable for printing.
    """
    # "lifetime" is in the heading because it is the claim the whole block makes: the figures are
    # stored totals plus the rows still on disk, not a report on whatever rows survived compaction.
    # It also gives the one window-scoped line below something to be the exception to.
    lines = [
        "TangleBrain — spend avoided (cloud-equivalent, lifetime)",
        f"  Tasks routed:   {summary.get('tasks', 0)}",
    ]
    failed = summary.get("failures", 0)
    lost = summary.get("lost_attempts", 0)
    # Show the failure line only once there is something to say — an all-green log stays as-is.
    if failed or lost:
        lines.append(f"  Tasks failed:   {failed} (lost failover attempts: {lost})")
    by_tier = summary.get("by_tier") or {}
    if by_tier:
        tiers = ", ".join(f"{k} {v}" for k, v in sorted(by_tier.items()))
        lines.append(f"  By tier:        {tiers}")
    by_origin = summary.get("by_origin") or {}
    # Show the origin split only once it says something — all-untagged history adds no signal.
    if any(k != "untagged" for k in by_origin):
        origins = ", ".join(f"{k} {v}" for k, v in sorted(by_origin.items()))
        lines.append(f"  By origin:      {origins}")
    lines.append(
        f"  Est. tokens:    in {summary.get('in_tokens_est', 0):,} / "
        f"out {summary.get('out_tokens_est', 0):,}"
    )
    lines.append(f"  Spend avoided:  ${summary.get('spend_avoided_usd', 0.0):,.2f}")
    # `pricing_refs` is collected by `rollup` from the rows and merged with the set the stored
    # totals carry, so a span survives compaction destroying the per-row evidence behind it.
    refs = summary.get("pricing_refs") or []
    if len(refs) > 1:
        lines.append(f"  Pricing ref:    {len(refs)} revisions")
        # Deliberately not a warning: spanning revisions is what any long-lived log does the first
        # time its operator tunes the reference price, and flagging normal history as a fault
        # teaches the reader to discount the caveats that do mean something.
        lines.append(
            "  ℹ pricing: this history spans an edit to the reference pricing — each task "
            "keeps the figure it was priced at."
        )
    else:
        lines.append(f"  Pricing ref:    {refs[0] if refs else pricing.reference_model}")
    if pricing.is_placeholder:
        lines.append(
            "  ⚠ pricing: PLACEHOLDER — figures are illustrative; set real rates in "
            "config/pricing.yaml and flip placeholder to false."
        )

    delegates = summary.get("delegates") or {}
    if delegates.get("count"):
        lines.append("")
        lines.append("  Delegated sub-tasks (offloaded by orchestrators)")
        lines.append(f"    Count:        {delegates.get('count', 0)}")
        by_backend = delegates.get("by_backend") or {}
        if by_backend:
            backends = ", ".join(
                f"{model} {info.get('count', 0)}" for model, info in sorted(by_backend.items())
            )
            lines.append(f"    By backend:   {backends}")
        by_parent = delegates.get("by_parent") or {}
        if by_parent:
            linked = [k for k in by_parent if k != "unlinked"]
            unlinked = (by_parent.get("unlinked") or {}).get("count", 0)
            if linked:
                tree = f"{len(linked)} parent task(s)"
                if unlinked:
                    tree += f", {unlinked} unlinked"
            else:
                tree = f"{unlinked} unlinked"  # all sub-calls ran outside a propagated task
            # The one window-scoped line in a lifetime block, and it says so. The parent tree has
            # one key per parent task id, so it cannot be folded into the permanent totals; an
            # unlabelled window split sitting under a lifetime headline reads as a lifetime count.
            lines.append(f"    Linked to:    {tree} (within the current row window)")
        lines.append(
            f"    Est. tokens:  in {delegates.get('in_tokens_est', 0):,} / "
            f"out {delegates.get('out_tokens_est', 0):,}"
        )
        lines.append(
            f"    Cloud-equiv:  ${delegates.get('cloud_equiv_usd', 0.0):,.2f} "
            "(informational — already credited within parent tasks)"
        )
    return "\n".join(lines)
