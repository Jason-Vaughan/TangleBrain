"""Global settings loader — the paid-API billing gate.

The roster (``roster.yaml``) is a *list* of routable backends; per-entry policy lives there. But a
few knobs are **global**, not per-entry — chief among them the paid-API billing gate. Those live
here, in a separate ``config/settings.yaml`` mapping, so the roster stays a bare list (folding a
global flag into it would force a breaking list→mapping parse change across every roster reader).

``api_billing_enabled`` is the durable safety contract: **paid billing is OFF unless this flag is
explicitly true.** When false, ``tier: api`` roster entries still
parse and are inspectable, but :func:`tanglebrain.selector.build_adapter` refuses to build their
adapter — they are inert. This is the single switch that makes accidental spend structurally hard.

Loading is fault-tolerant in the *missing-file* direction only: an absent settings file yields the
safe defaults (gate **off**). A present-but-malformed file raises :class:`SettingsError` rather than
silently falling back, so a typo in the gate can never be mistaken for "billing enabled".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


class SettingsError(ValueError):
    """Raised when the settings YAML is present but malformed or semantically invalid.

    A subclass of ``ValueError`` so callers can catch it specifically while still treating it as
    the bad-input error it is. A *missing* file is not an error (it yields defaults); only a
    present-but-broken file raises.
    """


@dataclass(frozen=True)
class Settings:
    """Global, non-per-entry TangleBrain settings.

    Attributes:
        api_billing_enabled: The paid-API billing gate. ``False`` (the default) means every
            ``tier: api`` roster entry parses but is **never routable** — the adapter factory
            refuses to build it. Must be flipped to ``True`` explicitly to permit any paid spend.
        classifier_gate_enabled: The local classifier gate. ``False`` (the default) keeps the
            normal frontier-first routing. When ``True``, a cheap local classify runs in front of the
            router: trivial requests go straight to the free local backend, only frontier requests
            reach an orchestrator. Off until an operator turns it on.
        delegate_max_concurrency: Operator cap on how many sub-tasks ``delegate_many`` runs at once.
            ``None`` (the default) means TangleBrain derives the cap from the system
            (``os.cpu_count()``). Set a positive int to pin it to the backend's real parallelism
            (e.g. match a local model server's ``OLLAMA_NUM_PARALLEL``) — the true limit TB can't
            portably introspect. A per-call ``max_concurrency`` may lower this but never exceed it.
    """

    api_billing_enabled: bool = False
    classifier_gate_enabled: bool = False
    delegate_max_concurrency: int | None = None


def default_settings_path() -> Path:
    """Return the path to the settings YAML shipped with the package.

    Returns:
        The absolute path to ``tanglebrain/config/settings.yaml``.
    """
    return Path(__file__).resolve().parent / "config" / "settings.yaml"


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load global settings, defaulting safely when the file is absent.

    Args:
        path: Path to the settings YAML. Defaults to the packaged
            ``tanglebrain/config/settings.yaml`` when ``None``.

    Returns:
        The parsed :class:`Settings`. A missing file yields :class:`Settings` defaults
        (billing gate **off**).

    Raises:
        SettingsError: If the file exists but is not a YAML mapping, or ``api_billing_enabled``
            is present and not a boolean. Never falls back silently on a malformed gate.
    """
    settings_path = Path(path) if path is not None else default_settings_path()
    if not settings_path.exists():
        return Settings()

    try:
        raw = yaml.safe_load(settings_path.read_text())
    except yaml.YAMLError as exc:
        raise SettingsError(f"settings file is not valid YAML: {settings_path}: {exc}") from exc

    # An empty file (``raw is None``) is a valid "use all defaults" statement.
    if raw is None:
        return Settings()
    if not isinstance(raw, dict):
        raise SettingsError(
            f"settings file must be a YAML mapping, got {type(raw).__name__}: {settings_path}"
        )

    def _bool(key: str) -> bool:
        value = raw.get(key, False)
        # Reject non-bool explicitly — `bool` is a subclass of `int`, but a stray `1`/`"true"` in a
        # gate must be a hard error, never a coincidental truthy enable.
        if not isinstance(value, bool):
            raise SettingsError(
                f"settings {key!r} must be a boolean (true/false), got "
                f"{value!r} ({type(value).__name__}): {settings_path}"
            )
        return value

    def _opt_positive_int(key: str) -> int | None:
        value = raw.get(key)
        if value is None:
            return None
        # Reject bool explicitly (bool is an int subclass) so a stray `true` can't pass as a count;
        # reject floats/strings; require a positive count.
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(
                f"settings {key!r} must be a positive integer, got "
                f"{value!r} ({type(value).__name__}): {settings_path}"
            )
        if value < 1:
            raise SettingsError(
                f"settings {key!r} must be >= 1, got {value!r}: {settings_path}"
            )
        return value

    return Settings(
        api_billing_enabled=_bool("api_billing_enabled"),
        classifier_gate_enabled=_bool("classifier_gate_enabled"),
        delegate_max_concurrency=_opt_positive_int("delegate_max_concurrency"),
    )
