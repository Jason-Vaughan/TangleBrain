"""Detect a usage log that the state-root migration copied only part of.

The v0.21.0 move from ``~/.cache/tanglebrain`` to the data tier staged every entry under one
shared name, so two console scripts starting together could act on each other's staging file. One
of those interleavings is silent: the losing process unlinks the leader's staging file while the
leader is still writing it, the leader writes on into an unnamed inode, and the loser's
still-partial copy is renamed into place. The migration reports success, the destination is short,
and because the re-run guard is "does the destination exist" no later run ever retries it. Staging
under a unique name (:func:`~tanglebrain.atomic.staging_path`) stops it happening again; it does
nothing for a store where it already happened.

**This module only reads.** It says what it found; it never repairs. A repair has to merge the
legacy records back in *underneath* everything written since the migration, and a blind copy would
destroy that history — so it is an explicit, separately-invoked operation, not something a startup
path does to an operator's data without being asked. Until that exists, the notice's real job is to
keep the evidence alive: it names the legacy directory and says to keep it, because a deleted one
leaves nothing to repair from.

**Why the check compares records and not file sizes.** After the migration the new log keeps
growing while the legacy one is frozen, so a log truncated at migration and appended to for a week
is *larger* than the file it is missing records from. Size says nothing; it fails on exactly the
long-running stores with the most to lose.

**The invariant, and the one thing that legitimately breaks it.** A complete migration leaves the
legacy log's bytes at the head of the new one. Compaction breaks that: once the log passes
:data:`~tanglebrain.measurement.MAX_LOG_BYTES` the *oldest* rows fold into ``totals.json`` and
leave the file (:func:`~tanglebrain.measurement.compact_log`), and the oldest rows are precisely
the migrated ones. A check that read the head of the log and stopped there would tell the heaviest,
healthiest users their data was gone — the same population the size heuristic fails, accused
instead of missed. Three properties recover an exact answer:

1. **Compaction removes a contiguous prefix.** So if any migrated record survives in the new log,
   the *newest* migrated record survives too. "Some migrated records present, but not the newest"
   is unreachable by compaction, and therefore evidence of a short copy.
2. **A fold is the only thing that writes ``totals.json``**
   (:func:`~tanglebrain.measurement.compact_log` is the only caller of
   :func:`~tanglebrain.totals.write_totals`), and the legacy root is frozen after the migration. So
   the two roots' totals differ **only if** a fold has run since. Not the converse: a
   ``totals.json`` that is deleted or damaged on either side reads back as zeros
   (:func:`~tanglebrain.totals.read_totals` degrades rather than raising), which looks like a fold
   that never ran. That direction over-reports rather than under-reports, which is the direction an
   honesty signal is allowed to fail in, and it is stated here rather than left to be discovered.
3. **A copy is a prefix in bytes.** So the legacy file's own size is the offset at which its last
   byte sits inside a complete copy, and comparing that boundary answers the healthy case without
   reading either log.

**What the check costs, honestly, because the answer changes after a fold.** An intact store that
has not folded past the migration boundary is confirmed by two small reads, at a cost independent
of the log's size — that is the common case and the one worth optimising. Once a fold moves those
bytes the boundary can never match again, and from then on **every** startup pays more: a scan of
the current log for the newest migrated record, and on a store that cannot be settled that way, a
full read of both. That state is permanent for any machine whose log has crossed the compaction cap
while a legacy root is still present, and the notice it produces repeats on every invocation. It is
not suppressed, because suppressing means remembering that it fired and remembering means writing
to the store — the one thing a detector must not do. The repair command is what discharges it.

**What is reported is the evidence, not a diagnosis.** The finding says the legacy log holds
records this one does not. A short copy is the reason that matters, but it is not the only way to
arrive there: an operator who downgraded to a version that still writes ``~/.cache`` and ran it has
appended records the new log never had, and an operator whose new root already held a
``usage.jsonl`` was never migrated into at all — the migration skips an entry whose destination
exists. So the causal sentence is offered as the usual explanation alongside the other, and never
as the finding itself.

**Two limits worth knowing before building the repair on this.** Rows are compared as a set, so a
legacy log holding the same line twice is satisfied by a current log holding it once — reachable
only for byte-identical records written inside one second, and it costs a missed detection rather
than a false one. And a legacy root the operator has already deleted is indistinguishable from one
that never existed, so no notice is possible: telling those apart needs a persisted marker, and
writing one is the thing this module forbids itself. That case is the reason the notice leads with
keeping the directory rather than explaining anything.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from tanglebrain.measurement import LOG_FILENAME, _read_lines
from tanglebrain.router import legacy_state_root, state_root
from tanglebrain.totals import TOTALS_FILENAME, read_totals

#: How many bytes at the end of the legacy log the boundary comparison reads, and the window the
#: newest migrated record is recovered from. Large enough that a match is not a coincidence — the
#: discriminating region is whatever a short copy left out, and a byte-level collision would need
#: the first appended record to reproduce the dropped legacy bytes exactly — and large enough to
#: hold several rows, which measure 240-330 bytes. Small enough that the read costs nothing beside
#: a process start.
BOUNDARY_BYTES = 8192

#: The store was shown to be missing records the legacy log still holds.
SHORT = "short"
#: No migrated record is present, which a deep enough fold explains as well as a short copy does.
INCONCLUSIVE = "inconclusive"
#: The comparison could not be performed. Reported rather than swallowed: silence renders
#: identically to a healthy store, so an unreadable legacy log would otherwise be the one path that
#: tells an operator their records are accounted for without ever having looked at them — and the
#: action it costs them is deleting the only copy.
UNREADABLE = "unreadable"


def _bytes_ending_at(path: Path, end: int, length: int) -> bytes | None:
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


def _newest_row(block: bytes, *, whole_file: bool) -> str | None:
    """Recover the newest complete row from the final bytes of a log.

    **A file that does not end in a newline ends mid-record**, and that fragment is not a row. It
    matters more here than anywhere else in the codebase: a complete migration copies the fragment
    verbatim, the first append after the move writes straight onto it, and the current log then
    holds ``<fragment><next record>`` as a single line that can never equal the legacy file's last
    line. Treating the fragment as a row would make an intact store fail the comparison and be told
    it had lost data — the exact accusation this module exists to avoid.

    Args:
        block: The tail bytes of the log.
        whole_file: Whether ``block`` is the entire file. When it is not, the first row in the
            block may itself have started before it, so a lone row cannot be trusted to be whole.

    Returns:
        The newest complete row, or ``None`` when the block does not certainly contain one.
    """
    text = block.decode("utf-8", errors="ignore")
    rows = [line for line in text.split("\n") if line.strip()]
    if not text.endswith("\n") and rows:
        rows.pop()  # the log stops mid-record; that fragment is not a row
    if not rows:
        return None
    if not whole_file and len(rows) < 2:
        return None  # the only candidate may have begun before the block; do not guess
    return rows[-1]


def _rows(path: Path) -> list[str] | None:
    """Read a log's rows verbatim, in order, or ``None`` when the file cannot be read.

    Defers to :func:`~tanglebrain.measurement._read_lines` for the split, so "what counts as a row"
    has one definition in the package and this comparison cannot drift from the one compaction and
    the rollup use. Raw lines rather than parsed records: the comparison asks whether the same row
    is present, and the row as written is the strongest form of that question — it survives a field
    this version does not know about and a line too torn to parse.

    **The read that looks redundant is the one that makes silence honest.** ``_read_lines`` returns
    ``[]`` for an empty log and for one it could not read, which is right for a rollup — a reader
    wants a smaller number, not an exception. It is wrong here: an empty legacy log means there is
    nothing to account for, and an unreadable one means nothing was checked, and reporting the
    second as the first is the single failure this module cannot afford. So the file is opened once
    to learn whether it can be read at all, which ``_read_lines`` cannot be asked, and the split is
    then delegated. It costs one extra read on a path that is already the rare, expensive one.

    **A trailing fragment is not a row**, and dropping it here rather than at the call sites is
    what keeps the rule in one place: :func:`_newest_row` applies the same test to the tail block,
    and this applies it to a full read — the path taken when a row is longer than
    :data:`BOUNDARY_BYTES`. A complete migration copies a mid-record fragment forward and the next
    append writes onto it, so counting it makes an intact store look short.

    Args:
        path: The usage log to read.

    Returns:
        One entry per complete row, ``[]`` for a log with no complete rows, or ``None`` when the
        file could not be read or decoded.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    rows = [raw for raw, _ in _read_lines(path)]
    if rows and not text.endswith("\n"):
        rows.pop()  # the log stops mid-record; that fragment is not a row
    return rows


def _contains_row(path: Path, row: str) -> bool | None:
    """Return whether ``row`` appears in the log, reading only as far as the first match.

    Streamed rather than collected into a set: the row being looked for is the newest *migrated*
    one, so on a healthy folded store it sits at the head of the surviving window and the scan stops
    almost immediately. Building a set would pay for the whole file every time.

    Args:
        path: The log to search.
        row: The exact row text, without its newline.

    Returns:
        ``True`` or ``False``, or ``None`` when the log cannot be read.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.rstrip("\r\n") == row:
                    return True
    except (OSError, UnicodeDecodeError):
        return None
    return False


def _folded_since(legacy_root: Path, current_root: Path) -> bool:
    """Return whether a compaction has folded rows since the migration.

    ``totals.json`` is written by compaction alone, and the legacy root is never written after the
    migration copies out of it — so a difference between the two roots' totals is a fold on this
    side of the move. Equality is *not* proof of the converse: an absent or damaged totals file
    reads back as zeros, so a deleted one on either side looks like a fold that never ran. That
    resolves toward the certain wording over the inconclusive one, which is the safe direction for
    a signal about missing data, and it is why the module docstring says "only if" and not "if and
    only if".

    Args:
        legacy_root: The pre-move state root.
        current_root: The state root in use now.

    Returns:
        ``True`` when the current root's totals have moved past the legacy root's.
    """
    return read_totals(current_root / TOTALS_FILENAME) != read_totals(legacy_root / TOTALS_FILENAME)


def probe_migration_integrity() -> str | None:
    """Check whether the current usage log accounts for every record in the legacy one.

    The healthy answer is cheap by construction rather than by a gate in front of an expensive
    comparison: the legacy file's own size is the offset at which its last byte should sit inside a
    complete copy, so an intact, un-folded store is settled by seeking to that boundary in each file
    and comparing :data:`BOUNDARY_BYTES`. Nothing there grows with the log.

    Past that boundary the cost rises, and it does so permanently for the store it happens to — see
    the module docstring. A boundary that does not match means either a short copy or a fold that
    moved it, and the totals settle which without reading a log at all; only a store that *has*
    folded needs the record comparison.

    Takes no overrides. Both roots already resolve through ``TANGLEBRAIN_STATE_DIR`` and
    ``XDG_DATA_HOME``, which is the seam every other test in this package drives, so parameters
    here would be a second way to say the same thing — and a second way that nothing exercises.

    Returns:
        A human-readable finding, or ``None`` when there is nothing to report. The silent cases are
        a migration shown to be complete, an override pointing both roots at one directory, a
        legacy log with no rows in it, and **a machine with no legacy root at all** — which covers
        both a clean install and an operator who has already deleted theirs. Those two are not
        separable without a persisted marker, so the deleted-cache case cannot be announced the way
        `#197 <https://github.com/Jason-Vaughan/TangleBrain/issues/197>`_ asks; a marker is a write,
        and this path does not write. Never raises: it runs before a command has parsed its
        arguments, and a check that broke the tool over a store it merely could not inspect would
        cost more than the defect it looks for.
    """
    # Resolution is inside the guard, not before it: both roots fall back to `Path.home()`, which
    # raises when the environment gives it nothing to resolve. Resolving outside would leave the
    # one promise this function makes false for its own first line.
    try:
        legacy = legacy_state_root()
        current = state_root()
    except (OSError, RuntimeError):
        return None
    if legacy == current:
        return None
    legacy_log = legacy / LOG_FILENAME
    current_log = current / LOG_FILENAME
    try:
        legacy_size = legacy_log.stat().st_size
    except OSError:
        return None  # no legacy root, or none readable: see Returns on why this cannot speak
    if legacy_size == 0:
        return None

    boundary = min(legacy_size, BOUNDARY_BYTES)
    legacy_edge = _bytes_ending_at(legacy_log, legacy_size, boundary)
    if legacy_edge is None:
        return _finding(legacy_log, UNREADABLE)
    try:
        current_size = current_log.stat().st_size
    except OSError:
        # The legacy log exists and its counterpart does not. `migrate_state_root` prints its own
        # failure when a copy raises, but that notice left with the process that printed it, and
        # the operator is standing in front of a store with no log at all.
        return _finding(legacy_log, SHORT)

    if current_size >= legacy_size:
        current_edge = _bytes_ending_at(current_log, legacy_size, boundary)
        if current_edge is None:
            return _finding(legacy_log, UNREADABLE, current_log)
        if current_edge == legacy_edge:
            return None

    # A complete copy puts the legacy bytes at the head of the current log, so the boundary can
    # only fail for two reasons — the copy was short, or a fold has since moved those bytes. The
    # totals separate them without reading a log.
    if not _folded_since(legacy, current):
        return _finding(legacy_log, SHORT)

    anchor = _newest_row(legacy_edge, whole_file=boundary == legacy_size)
    if anchor is None:
        # A row longer than the tail block. Rare, and it costs the full read the block exists to
        # avoid — but guessing at the newest record is how an intact store gets accused.
        legacy_rows = _rows(legacy_log)
        if legacy_rows is None:
            return _finding(legacy_log, UNREADABLE)
        if not legacy_rows:
            return None
        anchor = legacy_rows[-1]
    found = _contains_row(current_log, anchor)
    if found is None:
        return _finding(legacy_log, UNREADABLE, current_log)
    if found:
        # The newest migrated record is still here, and a fold takes a contiguous prefix — so
        # every record after it is here too, and nothing was left behind.
        return None

    legacy_rows = _rows(legacy_log)
    if legacy_rows is None:
        return _finding(legacy_log, UNREADABLE)
    current_rows = _rows(current_log)
    if current_rows is None:
        # Reachable only by a race: `_contains_row` above just read this same file end to end.
        # Kept rather than asserted away, and left untested deliberately — a test would have to
        # make the file fail between two reads, which pins the mock rather than the behaviour.
        return _finding(legacy_log, UNREADABLE, current_log)
    if not legacy_rows:
        return None
    present = set(current_rows)
    if any(row in present for row in legacy_rows):
        # Migrated records survive, but not the newest one. No fold can produce that: it would have
        # had to remove a record from the middle of the log.
        return _finding(legacy_log, SHORT)
    return _finding(legacy_log, INCONCLUSIVE)


def _finding(legacy_log: Path, verdict: str, failed_file: Path | None = None) -> str:
    """Compose the one-line finding for a store whose legacy records are unaccounted for.

    Three wordings, because three states are genuinely different and collapsing them would put a
    definite claim over a comparison that did not make one. All end on the same instruction, which
    is the only thing an operator can act on before the repair exists.

    Args:
        legacy_log: The legacy usage log holding the records in question. It is always the legacy
            one, because the closing instruction is always about the legacy directory.
        verdict: :data:`SHORT`, :data:`INCONCLUSIVE`, or :data:`UNREADABLE`.
        failed_file: Under :data:`UNREADABLE`, the file that actually could not be read — which is
            often the *current* log, and sending the operator to check the legacy file's
            permissions instead would waste the one diagnostic the notice offers. Defaults to
            ``legacy_log``.

    Returns:
        The line to print on stderr.
    """
    if verdict == SHORT:
        head = (
            f"tanglebrain: {legacy_log} holds usage records your current log does not, so your "
            "lifetime spend-avoided figure is understating. Usually that is the v0.21.0 state move "
            "having copied it only in part; running an older TangleBrain since the move does it too."
        )
    elif verdict == UNREADABLE:
        head = (
            f"tanglebrain: could not read {failed_file if failed_file is not None else legacy_log}"
            ", so whether your usage records survived the v0.21.0 state move is unknown — check "
            "that file's permissions and encoding."
        )
    else:
        head = (
            f"tanglebrain: could not confirm your current log still accounts for the records in "
            f"{legacy_log}. None of them are in it, which is equally what a routine compaction of a "
            "long log leaves behind, and the two cannot be told apart from here."
        )
    return head + (
        f" Keep {legacy_log.parent}: it holds the only copy of those records, and nothing can be "
        "repaired from it once it is deleted."
    )


def warn_if_migration_incomplete(stream: TextIO | None = None) -> str | None:
    """Report an incompletely migrated usage log on stderr. Never raises.

    Called by every console script after the migration and before it reads state. Reports rather
    than repairs, and reports every time rather than once: the condition persists until an explicit
    repair clears it, so a notice the operator saw on an invocation they have since scrolled past
    would leave a silently short lifetime figure behind it. Suppressing the repeat would mean
    remembering that it had fired, and remembering means writing to the store — which is the one
    thing a detector must not do.

    Args:
        stream: Where the finding goes. Defaults to ``sys.stderr`` — never stdout, which carries
            the routed answer and gets piped, and which on the MCP delegate carries the protocol.

    Returns:
        The finding that was printed, or ``None`` when the store checked out.
    """
    finding = probe_migration_integrity()
    if finding is None:
        return None
    print(finding, file=stream if stream is not None else sys.stderr)
    return finding
