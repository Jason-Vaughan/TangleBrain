"""Detect a usage log that the state-root migration copied only part of.

The v0.21.0 move from ``~/.cache/tanglebrain`` to the data tier staged every entry under one
shared name, so two console scripts starting together could act on each other's staging file. One
of those interleavings is silent: the losing process unlinks the leader's staging file while the
leader is still writing it, the leader writes on into an unnamed inode, and the loser's
still-partial copy is renamed into place. The migration reports success, the destination is short,
and because the re-run guard is "does the destination exist" no later run ever retries it. The
staging name is unique from v0.23.0 (:func:`~tanglebrain.atomic.staging_path`), which stops it
happening again — it does nothing for a store where it already happened.

**This module only reads.** It says what it found; it never repairs. A repair has to merge the
legacy records back in *underneath* everything written since the migration, and a blind copy would
destroy that history — so it is an explicit, separately-invoked operation, not something a startup
path does to an operator's data without being asked.

**Why the check is a comparison of records and not of file sizes.** After the migration the new log
keeps growing while the legacy one is frozen, so a log truncated at migration and appended to for a
week is *larger* than the legacy file it is missing records from. Size says nothing; it fails on
exactly the long-running stores with the most to lose.

**The invariant, and the one thing that legitimately breaks it.** A complete migration leaves the
legacy log's bytes at the head of the new one, so the legacy file's records are a prefix of the new
file's. Compaction breaks that: once the log passes :data:`~tanglebrain.measurement.MAX_LOG_BYTES`
the *oldest* rows fold into ``totals.json`` and leave the file
(:func:`~tanglebrain.measurement.compact_log`), and the oldest rows are precisely the migrated
ones. A check that read the head of the log and stopped there would tell the heaviest, healthiest
users their data was gone — the same population the size heuristic fails, accused instead of
missed. Two properties recover an exact answer:

1. **Compaction removes a contiguous prefix.** So if any migrated record survives in the new log,
   the *last* migrated record survives too. "Some legacy records present, but not the last one" is
   unreachable by compaction, and therefore evidence of a short copy.
2. **``totals.json`` is written by compaction and by nothing else**
   (:func:`~tanglebrain.measurement.compact_log` is the only caller of
   :func:`~tanglebrain.totals.write_totals`), and the legacy root is frozen after the migration. So
   the two roots' totals differ if and only if a fold has run since. That settles the one case
   record comparison cannot: no migrated record survives at all.

**What is reported is the evidence, not a diagnosis.** The finding says the legacy log holds
records this one does not. A short copy is the reason that matters, but it is not the only way to
arrive there — an operator who downgraded to a version that still writes ``~/.cache`` and ran it
has appended records the new log genuinely never had. Naming the cause would be wrong for them;
naming what was compared is true for everyone, and is what the operator needs in order to act.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from tanglebrain.measurement import LOG_FILENAME
from tanglebrain.router import legacy_state_root, state_root
from tanglebrain.totals import TOTALS_FILENAME, read_totals

#: How many bytes at the end of the legacy log the fast path compares. Large enough that a match is
#: not a coincidence — the discriminating region is whatever a short copy left out, and a byte-level
#: collision would need the first appended record to reproduce the dropped legacy bytes exactly —
#: and small enough that the read costs nothing beside a process start. Whole-file comparison was
#: the alternative and buys no accuracy the boundary already gives.
BOUNDARY_BYTES = 8192


def _tail(path: Path, end: int, length: int) -> bytes | None:
    """Read the ``length`` bytes ending at offset ``end``, or ``None`` if they cannot be read.

    Args:
        path: File to read.
        end: Absolute offset one past the last byte wanted.
        length: How many bytes to read back from ``end``.

    Returns:
        Exactly ``length`` bytes, or ``None`` when the file is unreadable or ends early — a short
        read means the region being compared is not there, which is never a match.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(end - length)
            chunk = handle.read(length)
    except OSError:
        return None
    return chunk if len(chunk) == length else None


def _records(path: Path) -> list[str]:
    """Read a usage log as its non-blank lines, verbatim and in order.

    Lines rather than parsed records: the comparison asks whether the same row is present, and the
    row as written is the strongest form of that question — it survives a field this version does
    not know about and a line too torn to parse, both of which a parse would collapse or drop.

    Args:
        path: The usage log to read.

    Returns:
        One entry per non-blank line. An absent or unreadable log yields ``[]``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [line for line in text.splitlines() if line.strip()]


def _folded_since(legacy_root: Path, current_root: Path) -> bool:
    """Return whether a compaction has folded rows since the migration.

    ``totals.json`` is written by compaction alone, and the legacy root is never written after the
    migration copies out of it — so a difference between the two roots' folded counters is a fold
    that happened on this side of the move, and equality is the absence of one.

    Args:
        legacy_root: The pre-move state root.
        current_root: The state root in use now.

    Returns:
        ``True`` when the current root's totals have moved past the legacy root's.
    """
    legacy = read_totals(legacy_root / TOTALS_FILENAME)
    current = read_totals(current_root / TOTALS_FILENAME)
    return current != legacy


def probe_migration_integrity(
    legacy_root: Path | None = None,
    current_root: Path | None = None,
) -> str | None:
    """Check whether the current usage log accounts for every record in the legacy one.

    Cheap by construction rather than by a gate in front of an expensive comparison. The legacy
    file's own size is the offset at which its last byte should sit inside a complete copy, so the
    healthy case is answered by seeking to that boundary in each file and comparing
    :data:`BOUNDARY_BYTES` — two seeks and two small reads, at a cost that does not grow with the
    log. Every startup of every console script pays that and nothing more.

    The record-level comparison runs only when the boundary does not match, which means either a
    short copy or a compaction that has since moved the boundary. Reading both logs is affordable
    there because it is rare and because that is what an exact answer costs; a store in this state
    has a real question to answer.

    Args:
        legacy_root: Override the pre-move state root. Defaults to
            :func:`~tanglebrain.router.legacy_state_root`.
        current_root: Override the state root in use. Defaults to
            :func:`~tanglebrain.router.state_root`.

    Returns:
        A human-readable finding, or ``None`` when there is nothing to report — which covers a
        verified-complete migration, a machine that never had a legacy root, and an override that
        points both roots at one directory. Never raises: this runs before a command has parsed its
        arguments, and a check that broke the tool over a store it merely could not inspect would
        cost more than the defect it looks for.
    """
    # Resolution is inside the guard, not before it: both roots fall back to `Path.home()`, which
    # raises when the environment gives it nothing to resolve. Resolving outside would leave the
    # one promise this function makes false for its own first line.
    try:
        legacy = legacy_root if legacy_root is not None else legacy_state_root()
        current = current_root if current_root is not None else state_root()
    except (OSError, RuntimeError):
        return None
    if legacy == current:
        return None
    legacy_log = legacy / LOG_FILENAME
    current_log = current / LOG_FILENAME
    try:
        legacy_size = legacy_log.stat().st_size
    except OSError:
        return None  # no legacy log: nothing was migrated, so there is nothing to account for
    if legacy_size == 0:
        return None
    try:
        current_size = current_log.stat().st_size
    except OSError:
        # The legacy log exists and its counterpart does not. The migration prints its own failure
        # when a copy raises, so this is that failure's aftermath rather than a second finding —
        # except that the notice is gone with the process that printed it, and the operator is
        # standing in front of a store with no log at all.
        return _finding(legacy_log, verified=True)

    boundary = min(legacy_size, BOUNDARY_BYTES)
    if current_size >= legacy_size:
        head = _tail(current_log, legacy_size, boundary)
        if head is not None and head == _tail(legacy_log, legacy_size, boundary):
            return None

    legacy_records = _records(legacy_log)
    if not legacy_records:
        return None  # bytes but no rows — nothing a record comparison can be missing
    current_records = set(_records(current_log))
    if legacy_records[-1] in current_records:
        # Compaction has folded past the migration boundary, but the newest migrated record is
        # still here — and since a fold takes a contiguous prefix, everything after it is too.
        return None
    if any(record in current_records for record in legacy_records):
        # Migrated records survive, but not the newest one. No fold can produce that: it would have
        # had to remove a record from the middle of the log.
        return _finding(legacy_log, verified=True)
    # Nothing at all in common. A fold deep enough to have taken every migrated record explains it
    # as well as a short copy does, and only one of the two leaves the totals moved.
    return _finding(legacy_log, verified=not _folded_since(legacy, current))


def _finding(legacy_log: Path, *, verified: bool) -> str:
    """Compose the finding text for a store whose legacy records are unaccounted for.

    Two wordings, because two states are genuinely different and collapsing them would put a
    definite claim over an inconclusive comparison. Both end on the same instruction: the legacy
    directory is the only copy of those records, and a repair that no longer has it has nothing to
    work from.

    Args:
        legacy_log: The legacy usage log holding the records in question.
        verified: Whether the records were shown to be missing, as opposed to not shown to be
            present.

    Returns:
        The line to print on stderr.
    """
    if verified:
        head = (
            f"tanglebrain: {legacy_log} holds usage records your current log does not. "
            "The v0.21.0 state move copied it only in part, so your lifetime spend-avoided "
            "figure is understating."
        )
    else:
        head = (
            f"tanglebrain: could not confirm the v0.21.0 state move copied all of {legacy_log}. "
            "Its records are no longer in your current log, which is also what a routine "
            "compaction of a long log looks like, and the two cannot be told apart from here."
        )
    return head + (
        " Keep that directory: it holds the only copy of those records, and nothing can be "
        "repaired from it once it is deleted."
    )


def warn_if_migration_incomplete(stream: TextIO | None = None) -> str | None:
    """Report an incompletely migrated usage log on stderr. Never raises.

    Called by every console script before it reads state, beside the migration itself. Reports
    rather than repairs, and reports every time rather than once: the condition persists until an
    explicit repair clears it, so a notice the operator saw on an invocation they have since
    scrolled past would leave a silently short lifetime figure behind it. Suppressing the repeat
    would mean remembering that it had fired, and remembering means writing to the store — which is
    the one thing a detector must not do.

    Args:
        stream: Where the finding goes. Defaults to ``sys.stderr`` — never stdout, which carries
            the routed answer and gets piped.

    Returns:
        The finding that was printed, or ``None`` when the store checked out.
    """
    finding = probe_migration_integrity()
    if finding is None:
        return None
    print(finding, file=stream if stream is not None else sys.stderr)
    return finding
