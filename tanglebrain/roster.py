"""Roster config loader (plan §5).

The roster is a simple, editable YAML list of routable models — *not* a registry subsystem
(we are explicitly not rebuilding FleetHub). Adding, removing, or reorganizing a model is an
entry edit, never a code change; this modifiability is a first-class requirement.

This module parses that YAML into typed objects. It parses *every* entry regardless of which
adapters are built, so the full roster is always inspectable; whether a given entry is
*invocable* depends on which adapters exist (``openai-compat`` + ``cli`` today; the paid-API
adapter is gated behind ``api_billing_enabled`` and lands later — issue #2).

Each entry (plan §5)::

    - id: gpt-oss-120b
      tier: local
      invoke: { kind: openai-compat, base_url: "http://.../v1", model: "gpt-oss-120b" }
      cost: free
      good_at: [grunt, code, tools]

``invoke.kind`` is one of ``openai-compat`` | ``cli`` | ``api``. ``scrub_env`` enforces the
§7 sub-vs-key safety rule per adapter. ``can_orchestrate`` flags an entry as eligible for the
§6 orchestrator rotation. ``key_ref`` references a credential without embedding it (see the
contract's key-ref convention): ``file:PATH`` | ``env:NAME`` | ``none``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_KINDS = ("openai-compat", "cli", "api")
VALID_TIERS = ("local", "sub", "api")


class RosterError(ValueError):
    """Raised when the roster YAML is missing, malformed, or semantically invalid.

    A subclass of ``ValueError`` so callers can catch it specifically while still treating
    it as the bad-input error it is.
    """


@dataclass(frozen=True)
class Invoke:
    """How to invoke one roster entry — the transport-specific call details (plan §5).

    Attributes:
        kind: One of ``openai-compat`` | ``cli`` | ``api``.
        base_url: OpenAI-compat base URL (e.g. the LiteLLM ``/v1`` endpoint). Required for
            ``openai-compat``; ``None`` otherwise.
        model: Model id/alias to request. Required for ``openai-compat``; ``None`` otherwise.
        cmd: Argv for a subprocess CLI invocation. Required for ``cli``; ``None`` otherwise.
            A literal ``{prompt}`` token in the argv is replaced with the prompt by the CLI
            adapter; if no token is present the prompt is appended as the final argument.
        scrub_env: Env var names to remove from a subprocess's environment before launch
            (e.g. ``ANTHROPIC_API_KEY``, so ``claude -p`` rides the flat sub, not a billed key).
        parse: Name of the output parser the ``cli`` adapter uses to extract the final text
            from the subprocess stdout (e.g. ``claude-json``, ``gemini-json``, ``plain``).
            ``None`` lets the adapter pick its default. Informational for non-``cli`` kinds.
        key_ref: Credential reference — ``file:PATH`` | ``env:NAME`` | ``none`` — never a raw
            secret. ``None`` means no credential is configured for this entry.
    """

    kind: str
    base_url: str | None = None
    model: str | None = None
    cmd: list[str] | None = None
    scrub_env: list[str] = field(default_factory=list)
    parse: str | None = None
    key_ref: str | None = None


@dataclass(frozen=True)
class RosterEntry:
    """One routable model in the roster (plan §5).

    Attributes:
        id: Unique identifier for the entry (e.g. ``gpt-oss-120b``, ``claude``).
        tier: Cost tier — ``local`` | ``sub`` | ``api``.
        invoke: How to call this entry (see :class:`Invoke`).
        cost: Free-form cost annotation (e.g. ``free``, ``flat-rate``); informational.
        good_at: Tags describing what the entry is good at (drives task-fit routing in §6).
        can_orchestrate: Whether this entry joins the §6 orchestrator rotation.
    """

    id: str
    tier: str
    invoke: Invoke
    cost: str | None = None
    good_at: list[str] = field(default_factory=list)
    can_orchestrate: bool = False


class Roster:
    """An ordered collection of :class:`RosterEntry`, with lookup/filter helpers.

    Order is preserved from the YAML file because it is meaningful — e.g. the local-first
    selector (C1) walks entries in declared order.
    """

    def __init__(self, entries: list[RosterEntry]) -> None:
        """Store the entries and build an id index, rejecting duplicate ids.

        Args:
            entries: The parsed roster entries, in file order.

        Raises:
            RosterError: If two entries share the same ``id``.
        """
        self._entries = list(entries)
        self._by_id: dict[str, RosterEntry] = {}
        for entry in self._entries:
            if entry.id in self._by_id:
                raise RosterError(f"duplicate roster entry id: {entry.id!r}")
            self._by_id[entry.id] = entry

    def __iter__(self):
        """Iterate entries in declared (file) order."""
        return iter(self._entries)

    def __len__(self) -> int:
        """Return the number of entries in the roster."""
        return len(self._entries)

    @property
    def entries(self) -> list[RosterEntry]:
        """Return a copy of the entries list, in declared order."""
        return list(self._entries)

    def by_id(self, entry_id: str) -> RosterEntry:
        """Return the entry with the given id.

        Args:
            entry_id: The id to look up.

        Returns:
            The matching :class:`RosterEntry`.

        Raises:
            KeyError: If no entry has that id.
        """
        return self._by_id[entry_id]

    def in_tier(self, tier: str) -> list[RosterEntry]:
        """Return all entries in the given tier, in declared order.

        Args:
            tier: The tier to filter by (e.g. ``local``).

        Returns:
            The matching entries (possibly empty).
        """
        return [e for e in self._entries if e.tier == tier]

    def orchestrators(self) -> list[RosterEntry]:
        """Return the entries flagged ``can_orchestrate`` (the §6 rotation set).

        Returns:
            The orchestrator-capable entries, in declared order.
        """
        return [e for e in self._entries if e.can_orchestrate]


def _parse_invoke(raw: object, entry_id: str) -> Invoke:
    """Validate and build an :class:`Invoke` from one entry's ``invoke`` block.

    Args:
        raw: The raw ``invoke`` value from YAML (expected to be a mapping).
        entry_id: The owning entry's id, for error messages.

    Returns:
        A validated :class:`Invoke`.

    Raises:
        RosterError: If the block is not a mapping, the kind is missing/unknown, or the
            fields required for that kind are absent.
    """
    if not isinstance(raw, dict):
        raise RosterError(f"entry {entry_id!r}: 'invoke' must be a mapping")

    kind = raw.get("kind")
    if kind not in VALID_KINDS:
        raise RosterError(
            f"entry {entry_id!r}: invoke.kind must be one of {VALID_KINDS}, got {kind!r}"
        )

    base_url = raw.get("base_url")
    model = raw.get("model")
    cmd = raw.get("cmd")
    scrub_env = raw.get("scrub_env", []) or []
    parse = raw.get("parse")
    key_ref = raw.get("key_ref")

    if kind == "openai-compat":
        if not base_url or not model:
            raise RosterError(
                f"entry {entry_id!r}: openai-compat invoke requires 'base_url' and 'model'"
            )
    elif kind == "cli":
        if not cmd or not isinstance(cmd, list):
            raise RosterError(f"entry {entry_id!r}: cli invoke requires a non-empty 'cmd' list")

    if not isinstance(scrub_env, list):
        raise RosterError(f"entry {entry_id!r}: invoke.scrub_env must be a list")

    if parse is not None and not isinstance(parse, str):
        raise RosterError(f"entry {entry_id!r}: invoke.parse must be a string")

    return Invoke(
        kind=kind,
        base_url=base_url,
        model=model,
        cmd=list(cmd) if cmd else None,
        scrub_env=list(scrub_env),
        parse=parse,
        key_ref=key_ref,
    )


def _parse_entry(raw: object) -> RosterEntry:
    """Validate and build one :class:`RosterEntry` from a YAML mapping.

    Args:
        raw: The raw entry value from YAML (expected to be a mapping).

    Returns:
        A validated :class:`RosterEntry`.

    Raises:
        RosterError: If the entry is not a mapping, is missing ``id`` / ``tier`` / ``invoke``,
            or ``tier`` is not one of ``VALID_TIERS``.
    """
    if not isinstance(raw, dict):
        raise RosterError(f"each roster entry must be a mapping, got {type(raw).__name__}")

    entry_id = raw.get("id")
    if not entry_id:
        raise RosterError("roster entry is missing required field 'id'")

    tier = raw.get("tier")
    if not tier:
        raise RosterError(f"entry {entry_id!r}: missing required field 'tier'")
    if tier not in VALID_TIERS:
        raise RosterError(
            f"entry {entry_id!r}: tier must be one of {VALID_TIERS}, got {tier!r}"
        )

    if "invoke" not in raw:
        raise RosterError(f"entry {entry_id!r}: missing required field 'invoke'")

    good_at = raw.get("good_at", []) or []
    if not isinstance(good_at, list):
        raise RosterError(f"entry {entry_id!r}: 'good_at' must be a list")

    return RosterEntry(
        id=entry_id,
        tier=tier,
        invoke=_parse_invoke(raw["invoke"], entry_id),
        cost=raw.get("cost"),
        good_at=list(good_at),
        can_orchestrate=bool(raw.get("can_orchestrate", False)),
    )


def default_roster_path() -> Path:
    """Return the path to the roster YAML shipped with the package.

    Returns:
        The absolute path to ``tanglebrain/config/roster.yaml``.
    """
    return Path(__file__).resolve().parent / "config" / "roster.yaml"


def load_roster(path: str | os.PathLike[str] | None = None) -> Roster:
    """Load and validate the roster YAML into a :class:`Roster`.

    Args:
        path: Path to the roster YAML. Defaults to the packaged
            ``tanglebrain/config/roster.yaml`` when ``None``.

    Returns:
        The parsed :class:`Roster`.

    Raises:
        RosterError: If the file is missing, is not a YAML list, or any entry is invalid.
    """
    roster_path = Path(path) if path is not None else default_roster_path()
    if not roster_path.exists():
        raise RosterError(f"roster file not found: {roster_path}")

    try:
        raw = yaml.safe_load(roster_path.read_text())
    except yaml.YAMLError as exc:
        raise RosterError(f"roster file is not valid YAML: {roster_path}: {exc}") from exc

    if not isinstance(raw, list):
        raise RosterError(
            f"roster file must be a YAML list of entries, got {type(raw).__name__}: {roster_path}"
        )

    return Roster([_parse_entry(item) for item in raw])
