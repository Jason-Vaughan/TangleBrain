"""Surgical, comment-preserving roster editor (the knob-panel roster slice).

The knob panel lets the operator tune config from the browser. Roster editing needs care because
``roster.yaml`` carries dense inline comments (per-CLI invocation notes, the paid-tier example) that
a naive YAML dump would destroy. This module edits a **focused** set of simple per-entry fields *in
place* — touching only the specific value on the specific line — so every comment, blank line, and
the nested ``invoke`` block survive byte-for-byte. No new dependency (the GUI's standing
zero-new-runtime-dep stance).

It deliberately does **not** add, remove, or reorder entries, nor edit the ``invoke`` block — those
still need a hand-edit (they'd require a full comment-preserving YAML round-trip). Editable fields
are entry-level scalars only:

- ``enabled`` / ``can_orchestrate`` — booleans,
- ``budget_usd_month`` — a positive number, or cleared (``None``) → the line is removed,
- ``good_at`` — a flow-style list of simple tags.

**Safety**: every save re-parses the candidate text with the real :func:`tanglebrain.roster.load_roster`
*before* it is written (a surgical mistake can never land a malformed roster), then backs up the
existing file and writes atomically — mirroring the pricing save.
"""
from __future__ import annotations

import math
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from tanglebrain.measurement import _atomic_write, _backup_dir
from tanglebrain.roster import RosterError, default_roster_path, load_roster

# The only fields this editor will touch (entry-level scalars). Everything else — id, tier, the
# invoke block, cost — stays hand-edited.
EDITABLE_FIELDS = ("enabled", "can_orchestrate", "budget_usd_month", "good_at")

_GOOD_AT_TAG_RE = re.compile(r"^[\w.\-]+$")  # flow-safe tags: no spaces/commas/brackets/quotes


class RosterEditError(ValueError):
    """Raised when a requested roster edit is invalid or cannot be applied safely."""


def render_field(field: str, value: object) -> str | None:
    """Validate ``value`` for ``field`` and render it as the YAML scalar to write.

    Args:
        field: One of :data:`EDITABLE_FIELDS`.
        value: The new value (from JSON: ``bool`` / number / ``None`` / list of str).

    Returns:
        The rendered YAML value (e.g. ``"true"``, ``"25"``, ``"[a, b]"``), or ``None`` to signal the
        field's line should be **removed** (only ``budget_usd_month`` cleared to ``None``).

    Raises:
        RosterEditError: If ``field`` is not editable or ``value`` is the wrong type/shape.
    """
    if field not in EDITABLE_FIELDS:
        raise RosterEditError(f"field {field!r} is not editable (allowed: {', '.join(EDITABLE_FIELDS)})")

    if field in ("enabled", "can_orchestrate"):
        if not isinstance(value, bool):
            raise RosterEditError(f"{field} must be a boolean, got {value!r}")
        return "true" if value else "false"

    if field == "budget_usd_month":
        if value is None or value == "":
            return None  # cleared → remove the line (absent == no budget)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RosterEditError(f"budget_usd_month must be a finite number, got {value!r}")
        if value <= 0:
            raise RosterEditError(f"budget_usd_month must be > 0, got {value!r}")
        # Render an integer-valued number without a trailing .0, else the float repr.
        return str(int(value)) if float(value).is_integer() else repr(float(value))

    # good_at — a list of simple, flow-safe tags.
    if not isinstance(value, list) or any(not isinstance(t, str) for t in value):
        raise RosterEditError(f"good_at must be a list of strings, got {value!r}")
    tags = [t.strip() for t in value]
    for t in tags:
        if not t or not _GOOD_AT_TAG_RE.match(t):
            raise RosterEditError(
                f"good_at tag {t!r} is not a simple tag (allowed: letters, digits, _ . -)"
            )
    return "[" + ", ".join(tags) + "]"


def _entry_id_of(line: str) -> str | None:
    """Return the entry id declared by a ``- id: <id>`` list-item line, or ``None``."""
    m = re.match(r"^-\s+id:\s*(.*)$", line)
    if not m:
        return None
    raw = re.split(r"\s+#", m.group(1), maxsplit=1)[0].strip()
    return raw.strip("\"'")


def _find_entry_block(lines: list[str], entry_id: str) -> tuple[int, int]:
    """Return the ``[start, end)`` line range of ``entry_id``'s block (its indented field lines).

    The block is the ``- id:`` line plus every following *indented* line, stopping at the first
    column-0 line (a blank line, the next ``- `` item, a ``#`` comment, or EOF).

    Raises:
        RosterEditError: If no entry with that id is found.
    """
    start = next((i for i, ln in enumerate(lines) if _entry_id_of(ln) == entry_id), None)
    if start is None:
        raise RosterEditError(f"no roster entry with id {entry_id!r}")
    end = start + 1
    while end < len(lines) and lines[end][:1] in (" ", "\t"):
        end += 1
    return start, end


def _apply_field(lines: list[str], entry_id: str, field: str, rendered: str | None) -> list[str]:
    """Return a new ``lines`` list with ``field`` set to ``rendered`` on ``entry_id``'s block.

    Updates the value in place (preserving any trailing inline comment); when the field is absent it
    is appended at the **end of the entry's block** (a fresh 2-space-indent line after all the entry's
    existing lines), which can never splice between a block-style key and its continuation, nor land
    inside the nested ``invoke`` map. Removes the line when ``rendered`` is ``None``.
    """
    start, end = _find_entry_block(lines, entry_id)
    # The field line is matched only at exactly 2-space indent, so a same-named key nested deeper in
    # the invoke block (4-space) is never mistaken for the top-level field.
    field_re = re.compile(r"^( {2}" + re.escape(field) + r":\s*)(.*?)(\s+#.*)?$")

    for i in range(start, end):
        m = field_re.match(lines[i])
        if not m:
            continue
        if rendered is None:
            return lines[:i] + lines[i + 1:]  # remove the line
        return lines[:i] + [m.group(1) + rendered + (m.group(3) or "")] + lines[i + 1:]

    # Field absent.
    if rendered is None:
        return lines  # nothing to remove
    return lines[:end] + [f"  {field}: {rendered}"] + lines[end:]


def save_roster_edits(
    entry_id: str,
    fields: dict,
    path: str | os.PathLike[str] | None = None,
) -> None:
    """Apply edits to one entry's editable fields, validate, back up, and write atomically.

    Args:
        entry_id: The id of the (existing) entry to edit.
        fields: ``{field: value}`` for one or more :data:`EDITABLE_FIELDS`.
        path: Target roster YAML. Defaults to the packaged ``config/roster.yaml``.

    Raises:
        RosterEditError: If the entry is unknown, a field/value is invalid, or the resulting YAML
            fails to re-parse as a valid roster (in which case nothing is written).
    """
    if not fields:
        raise RosterEditError("no fields to edit")

    target = Path(path) if path is not None else default_roster_path()
    if not target.exists():
        raise RosterEditError(f"roster file not found: {target}")

    text = target.read_text(encoding="utf-8")
    lines = text.split("\n")

    _find_entry_block(lines, entry_id)  # fail fast on unknown id before rendering anything
    for field, value in fields.items():
        lines = _apply_field(lines, entry_id, field, render_field(field, value))
    candidate = "\n".join(lines)

    # Validate by re-parsing with the real loader before any write — a surgical slip can never land
    # a malformed roster. Use a sibling temp file so the parse sees exactly what we'd write.
    check = target.with_name(target.name + ".check.tmp")
    try:
        check.write_text(candidate, encoding="utf-8")
        try:
            roster = load_roster(check)
        except RosterError as exc:
            raise RosterEditError(f"edit would produce an invalid roster: {exc}") from exc
    finally:
        check.unlink(missing_ok=True)

    # Defensive: confirm each edit actually took on the re-parsed entry.
    entry = roster.by_id(entry_id)
    for field, value in fields.items():
        got = getattr(entry, field)
        want = (None if (value is None or value == "") and field == "budget_usd_month"
                else float(value) if field == "budget_usd_month"
                else [t.strip() for t in value] if field == "good_at"
                else value)
        if got != want:
            raise RosterEditError(f"edit to {field!r} did not apply as expected (got {got!r})")

    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    shutil.copy2(target, backup_dir / f"roster-{stamp}.yaml")
    _atomic_write(target, candidate)
