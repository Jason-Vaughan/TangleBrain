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
from pathlib import Path
from typing import Callable, Mapping

from tanglebrain.adapters import AdapterError
from tanglebrain.adapters.base import Adapter
from tanglebrain.roster import RosterEntry, Roster
from tanglebrain.selector import build_adapter
from tanglebrain.settings import Settings, load_settings

STATE_DIR_ENV = "TANGLEBRAIN_STATE_DIR"
DEFAULT_STATE_SUBDIR = ".cache/tanglebrain"
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


def default_state_path() -> Path:
    """Return the rotation-state file path.

    Honors ``TANGLEBRAIN_STATE_DIR`` (``~`` expanded); otherwise ``~/.cache/tanglebrain/``.

    Returns:
        The absolute path to the router state JSON file.
    """
    base = os.environ.get(STATE_DIR_ENV)
    root = Path(base).expanduser() if base else Path.home() / DEFAULT_STATE_SUBDIR
    return root / STATE_FILENAME


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
            inject_delegate: Make the local-delegate tool available to each orchestrator, so it
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

        failures: list[tuple[str, str]] = []
        for entry in candidates:
            adapter = self._adapter_factory(entry, inject_delegate=self.inject_delegate)
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
                adapter = self._adapter_factory(entry, inject_delegate=self.inject_delegate)
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
