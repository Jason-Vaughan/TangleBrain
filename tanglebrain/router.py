"""Frontier-first router — pluggable orchestrator selection + rotation + failover.

The control plane: route a task to an orchestrator (a ``can_orchestrate`` backend), rotate the
orchestrator role across the eligible set, and fail over to the next when one errors. Rotation and
failover give resilience and even load, independent of local delegation.

This module stays a deterministic control plane. It does not classify tasks for the caller — the
caller passes a ``task`` hint or gets pure rotation; the optional classifier gate lives in
:mod:`tanglebrain.classifier`. Orchestrators offload sub-tasks to the free local backend via the
``delegate_local`` tool, which the router injects into their invocations.

Rotation state persists across processes (each ``tanglebrain`` run is a new process), so successive
requests rotate across the orchestrators.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Mapping, TextIO

from tanglebrain.adapters import AdapterError
from tanglebrain.adapters.base import Adapter
from tanglebrain.atomic import staging_path
from tanglebrain.roster import RosterEntry, Roster
from tanglebrain.selector import build_adapter
from tanglebrain.settings import Settings, load_settings

STATE_DIR_ENV = "TANGLEBRAIN_STATE_DIR"
XDG_DATA_HOME_ENV = "XDG_DATA_HOME"

#: Default state root, relative to ``~``. This is the XDG *data* tier, deliberately: everything
#: under the state root — the rotation cursor, the usage log, config backups — has to survive a
#: disk cleaner. ``~/.cache`` is *defined* as a directory anything may delete at will, so a
#: lifetime spend-avoided figure stored there is one `brew cleanup` from zero.
DEFAULT_STATE_SUBDIR = ".local/share/tanglebrain"

#: Where the state root lived before the move. Read once per process by
#: :func:`migrate_state_root` and never written.
LEGACY_STATE_SUBDIR = ".cache/tanglebrain"

STATE_FILENAME = "router-state.json"

# Substrings that mark an orchestrator failure as a rate-limit/capacity issue rather than a hard
# error. Used only to annotate the failover log — failover happens on *any* AdapterError.
_RATE_LIMIT_RE = re.compile(r"429|rate.?limit|quota|resource_exhausted|overloaded|too many requests", re.IGNORECASE)


class RouterError(RuntimeError):
    """Raised when no orchestrator can serve a request.

    Two cases: the roster has no orchestrator-capable entry, or every orchestrator tried failed.
    The message names each per-orchestrator failure so the caller can see what went wrong.
    """


def _looks_like_rate_limit(message: str) -> bool:
    """Return whether an error message looks like a rate-limit / capacity rejection.

    Args:
        message: The error text (e.g. an ``AdapterError`` string).

    Returns:
        ``True`` if it matches a known rate-limit/capacity pattern.
    """
    return bool(_RATE_LIMIT_RE.search(message or ""))


def state_root() -> Path:
    """Return the directory holding every piece of TangleBrain's persistent state.

    Resolution order, highest precedence first:

    1. ``TANGLEBRAIN_STATE_DIR`` — the operator's explicit override (``~`` expanded). Unchanged
       by the move to the data tier, and it still wins over everything.
    2. ``XDG_DATA_HOME``/``tanglebrain`` — honored when the operator has relocated their data tier.
    3. ``~/.local/share/tanglebrain`` — the default.

    The rotation cursor, the usage log, and config backups all live here. None of them is a
    cache: losing the cursor perturbs rotation, losing the usage log destroys the product's
    lifetime spend-avoided claim outright, and losing a backup destroys the only copy of a
    config the operator hand-edited.

    Returns:
        The absolute path to the state root. Not created — callers that write make it.
    """
    base = os.environ.get(STATE_DIR_ENV)
    if base:
        return Path(base).expanduser()
    xdg = os.environ.get(XDG_DATA_HOME_ENV)
    if xdg:
        return Path(xdg).expanduser() / "tanglebrain"
    return Path.home() / DEFAULT_STATE_SUBDIR


def legacy_state_root() -> Path:
    """Return the pre-move, cache-tier state root that :func:`migrate_state_root` reads from.

    ``TANGLEBRAIN_STATE_DIR`` resolves this the same way it resolves :func:`state_root`, so an
    operator who already set the override has both pointing at one directory and nothing to
    migrate — the override was always the state root, wherever they put it.

    Returns:
        The absolute path to the legacy state root.
    """
    base = os.environ.get(STATE_DIR_ENV)
    if base:
        return Path(base).expanduser()
    return Path.home() / LEGACY_STATE_SUBDIR


def _discard(path: Path) -> None:
    """Remove ``path`` — file or directory tree — tolerating its absence.

    Used to clear a half-written staging entry after a failed copy. Absence is tolerated rather
    than expected: the failure may have arrived before anything was created, and a cleanup that
    raised would replace the real error with its own.

    Args:
        path: The path to remove.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return


def migrate_state_root(stream: TextIO | None = None) -> list[str]:
    """Copy a pre-existing cache-tier state root forward to the data tier. Never raises.

    Copies, rather than moves, and **leaves every original in place**: an operator who downgrades
    to a version that still reads ``~/.cache`` must not find their history gone. The cost is one
    duplicated directory, which is cheap next to silently zeroing someone's spend-avoided figure.

    Every entry in the legacy root is migrated, rather than a named list of the files we expect.
    A hard-coded list is a claim about the directory's contents that decays the moment anything
    new is written there, and the failure mode is silent partial migration — the worst kind.

    Copying is per-entry and skips anything already present at the destination, so an interrupted
    run completes on the next invocation instead of skipping wholesale, and a completed run is a
    no-op. Each entry is staged under a temporary name in the destination directory and moved into
    place with :func:`os.replace`, so a copy killed part-way leaves no partial file at the real
    path — which matters precisely *because* the re-run guard is "does the destination exist": a
    truncated ``usage.jsonl`` would be skipped forever and understate the lifetime figure with
    nothing to signal it. One notice is printed for the move as a whole, not one per file: the
    operator needs to know their state moved, not to read an inventory.

    The staging name is **unique per process** (:func:`~tanglebrain.atomic.staging_path`), not a
    fixed one. Every console script migrates on startup, so two can be here at once, and a shared
    staging name lets each act on the other's file. Either the second one's cleanup deletes the
    first one's *completed* copy, whose :func:`os.replace` then fails on a file that is simply gone
    — both processes report a failed migration on a machine where nothing is wrong, and the next
    run repairs it — or, worse, it unlinks that copy *while the first is still writing it*, so the
    first writes on into a file that no longer has a name and then renames the second one's
    still-partial copy into place. That one is silent, and because the re-run guard is "does the
    destination exist" no later run ever retries the truncated file. A unique name means a losing
    process can only ever discard its own work.

    The staging entry is discarded in a ``finally`` rather than on the error path, because a unique
    name is one no later run can find: whatever is not cleaned up on the way out is never cleaned
    up at all. ``SIGKILL`` and power loss still orphan one. That is accepted rather than solved,
    because the only way to reclaim it is to sweep the destination for the staging suffix at
    startup — and a sweep is the shared-name collision again, one directory wider.

    Such an orphan is **deliberately visible**. The old name was dotfile-hidden; this one cannot be,
    because uniqueness comes from the suffix :func:`~tanglebrain.atomic.staging_path` appends, and
    hiding it again would mean a second naming rule at the one call site that exists to stop having
    one. Visible is also the right answer on its own terms: an orphan is the operator's to delete,
    and they cannot delete what they cannot see.

    Failure is reported, never raised and never silent. A migration that fails leaves the new root
    incomplete, and a rollup over an incomplete log understates savings — which reads as the
    product lying rather than as a broken copy. Saying so on stderr is what distinguishes them;
    the next invocation retries.

    Args:
        stream: Where the notice goes. Defaults to ``sys.stderr`` — never stdout, which carries
            the routed answer and gets piped.

    Returns:
        The names of the entries copied, empty when there was nothing to do.
    """
    out = stream if stream is not None else sys.stderr
    source = legacy_state_root()
    target = state_root()
    if source == target:
        return []
    migrated: list[str] = []
    try:
        if not source.is_dir():
            return []
        for item in sorted(source.iterdir()):
            destination = target / item.name
            if destination.exists():
                continue
            target.mkdir(parents=True, exist_ok=True)
            staged = staging_path(destination)
            try:
                if item.is_dir():
                    shutil.copytree(item, staged)
                else:
                    shutil.copy2(item, staged)
                os.replace(staged, destination)
            except OSError:
                if destination.exists():
                    # Another console script started at the same moment and won the race. Its
                    # copy is the same bytes from the same source, so this is not a failure.
                    continue
                raise
            finally:
                # `finally`, not the `except` arm: Ctrl-C during a first-run migration is not an
                # `OSError`, and an entry that leaves this block uncleaned is unreclaimable (see
                # the note on staging names above). A successful replace makes this a no-op.
                _discard(staged)
            migrated.append(item.name)
    except OSError as exc:
        print(
            f"tanglebrain: could not move state from {source} to {target} ({exc}). "
            "Your history is still at the old path; --stats may read low until this succeeds.",
            file=out,
        )
        return migrated
    if migrated:
        print(
            f"tanglebrain: state moved from {source} to {target} "
            f"({len(migrated)} item(s) copied; originals left in place).",
            file=out,
        )
    return migrated


def default_state_path() -> Path:
    """Return the rotation-state file path.

    Returns:
        The absolute path to the router state JSON file, under :func:`state_root`.
    """
    return state_root() / STATE_FILENAME


def _read_cursor(path: Path) -> int:
    """Read the rotation cursor from the state file, tolerating missing/corrupt state.

    Args:
        path: The state file path.

    Returns:
        The stored cursor (>= 0), or ``0`` if the file is absent, unreadable, or malformed —
        bad state must never crash routing.
    """
    try:
        data = json.loads(path.read_text())
        cursor = int(data["cursor"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return 0
    return cursor if cursor >= 0 else 0


def _write_cursor(path: Path, cursor: int) -> None:
    """Persist the rotation cursor, creating the parent directory as needed.

    The write is not locked or atomic: two concurrent ``--route`` processes can read the same
    cursor and both write (last-writer-wins), so a rotation slot may be skipped or repeated. That
    is intentionally accepted — the cursor is a load-spread *hint*, not a correctness invariant, so
    a lost update only mildly perturbs the spread, never breaks routing.

    Args:
        path: The state file path.
        cursor: The cursor value to store.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cursor": cursor}))


class Router:
    """Frontier-first orchestrator router: task-fit selection + rotation + failover."""

    def __init__(
        self,
        roster: Roster,
        state_path: str | os.PathLike[str] | None = None,
        adapter_factory: Callable[..., Adapter] = build_adapter,
        inject_delegate: bool = True,
        settings: Settings | None = None,
    ) -> None:
        """Configure the router.

        Args:
            roster: The loaded roster; its ``can_orchestrate`` entries form the rotation set.
            state_path: Path to the rotation-state file. Defaults to :func:`default_state_path`.
                Inject a temp path in tests so they never touch the real cache.
            adapter_factory: Builds an adapter for an entry. Defaults to the selector's
                ``build_adapter``; injectable for tests. Called as
                ``adapter_factory(entry, inject_delegate=...)``.
            inject_delegate: Master switch for delegation. When true (the default) each entry's
                own ``can_orchestrate`` decides; when false, delegation is off for every entry
                this router builds. Make the local-delegate tool available to each orchestrator, so it
                offloads sub-tasks to the free local backend. On by default; set false to route to
                bare orchestrators (e.g. for debugging).
            settings: Global settings carrying the paid-API billing gate. Loaded from the
                packaged ``config/settings.yaml`` when ``None``. The router consults
                ``api_billing_enabled`` to decide whether the last-resort paid-API fallback is live;
                with the gate off (the default) the router never reaches a paid tier.
        """
        self.roster = roster
        self.state_path = Path(state_path) if state_path is not None else default_state_path()
        self._adapter_factory = adapter_factory
        self.inject_delegate = inject_delegate
        self.settings = settings if settings is not None else load_settings()
        # The entry that served the most recent successful route(), or None before any success.
        # Surfaced so the CLI's measurement seam can record which tier/model handled a task
        # without changing route()'s str return type.
        self.last_served: RosterEntry | None = None
        # The (entry_id, error) attempts the most recent route() lost before serving (empty on a
        # first-try success). Surfaced like last_served so the measurement seam can record lost
        # failover attempts and fully-failed tasks (#100).
        self.last_failures: list[tuple[str, str]] = []

    def route(
        self,
        prompt: str,
        task: str | None = None,
        opts: Mapping[str, object] | None = None,
    ) -> str:
        """Route ``prompt`` to an orchestrator sub, with task-fit, rotation, and failover.

        Selection: among the ``can_orchestrate`` entries, walk in round-robin order starting from
        the persisted cursor; if ``task`` is given, prefer entries whose ``good_at`` contains it
        (falling back to all orchestrators when none match — task-fit is a preference, not a gate).
        Try each in order; on an :class:`~tanglebrain.adapters.base.AdapterError` from one, fail
        over to the next. On success, advance the persisted cursor past the served orchestrator so
        the next request starts elsewhere (load-spread).

        Last resort: if **every** orchestrator fails and the paid-API billing gate is on
        (``settings.api_billing_enabled``), fall through to the enabled ``tier: api`` entries in
        roster order — the genuine last resort. With the gate off (the default) the router never
        reaches a paid tier. A paid success does not advance the orchestrator rotation cursor.

        Args:
            prompt: The task prompt.
            task: Optional task-fit hint — a ``good_at`` tag (e.g. ``code``, ``reasoning``,
                ``long-context``).
            opts: Optional per-call adapter options (passed straight through to ``adapter.run``).

        Returns:
            The serving entry's response text (an orchestrator, or a paid-API fallback when the gate
            is on and all orchestrators failed). The served entry is exposed on ``last_served``.

        Raises:
            RouterError: If the roster has no orchestrator-capable entry, or every candidate tried
                (all orchestrators, plus any enabled paid-API fallback) failed.
        """
        orchestrators = self.roster.orchestrators()
        if not orchestrators:
            raise RouterError(
                "no orchestrator-capable entries in roster (need at least one can_orchestrate: true)"
            )

        n = len(orchestrators)
        cursor = _read_cursor(self.state_path) % n
        rotated = [orchestrators[(cursor + i) % n] for i in range(n)]

        if task:
            candidates = [e for e in rotated if task in e.good_at] or rotated
        else:
            candidates = rotated

        # Aliased onto the instance up front so it reflects the attempts however route() exits —
        # success after failover, total failure, or the paid fallback (#100).
        failures: list[tuple[str, str]] = []
        self.last_failures = failures
        for entry in candidates:
            # None lets build_adapter derive from the entry: a paid last-resort entry that is
            # not an orchestrator must not receive the delegate tool merely by reaching this loop.
            adapter = self._adapter_factory(
                entry, inject_delegate=None if self.inject_delegate else False
            )
            try:
                text = adapter.run(prompt, opts)
            except AdapterError as exc:
                failures.append((entry.id, str(exc)))
                continue
            served_pos = next(i for i, e in enumerate(orchestrators) if e.id == entry.id)
            _write_cursor(self.state_path, (served_pos + 1) % n)
            self.last_served = entry
            return text

        # Last-resort paid-API fallback. Only after EVERY orchestrator has failed, and only when
        # billing is explicitly enabled, fall through to a paid `api` entry (the genuine last
        # resort). With the gate off — the default — this block is skipped entirely, so the router
        # can never reach a paid tier. Enabled paid entries are tried in roster order; a paid success
        # does NOT advance the orchestrator rotation cursor (api is not part of the rotation). This
        # requires at least one orchestrator above — the router never paid-routes a roster that has no
        # orchestrators to exhaust (use ``--model`` for an explicit paid call).
        if self.settings.api_billing_enabled:
            attempted = {eid for eid, _ in failures}
            for entry in self.roster.in_tier("api"):
                # Skip disabled entries, and any already tried above (a degenerate roster could flag
                # a paid entry ``can_orchestrate: true``, putting it in the rotation — don't re-run it).
                if not entry.enabled or entry.id in attempted:
                    continue
                # None lets build_adapter derive from the entry: a paid last-resort entry that
                # is not an orchestrator must not receive the delegate tool merely by reaching
                # this loop.
                adapter = self._adapter_factory(
                    entry, inject_delegate=None if self.inject_delegate else False
                )
                try:
                    text = adapter.run(prompt, opts)
                except AdapterError as exc:
                    failures.append((entry.id, str(exc)))
                    continue
                self.last_served = entry
                return text

        detail = "; ".join(
            f"{eid}{' [rate-limit]' if _looks_like_rate_limit(msg) else ''}: {msg}"
            for eid, msg in failures
        )
        raise RouterError(f"all {len(failures)} candidate(s) failed: {detail}")
