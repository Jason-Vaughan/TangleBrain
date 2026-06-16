"""Measurement / "spend avoided" rollup (plan §8) — C4.

Every routed task is logged as one JSON line in an append-only usage log, and ``tanglebrain
--stats`` rolls those records up into a "spend avoided" figure: what the routed work *would* have
cost on a paid frontier API, had it not gone to the free local tier or a flat-rate sub. This is how
the north star (drive ongoing compute cost *down*) becomes visible, and it is the data the §6
routing-evolution gate depends on later.

Design notes:

- **Tokens are estimated, not measured.** CLI subs (claude/codex/gemini) expose no usable token
  counts, and the local tier's real ``usage`` is inflated by dropped gpt-oss reasoning tokens. So a
  single ``chars/4`` heuristic over the visible prompt + response is applied *uniformly* across all
  tiers — one consistent, if approximate, methodology (see :func:`estimate_tokens`).
- **Pricing is config-driven** (``config/pricing.yaml``), seeded from monad-stats' ``costSaved``
  constants so the two projects stay aligned; C5's knob GUI tunes it.
- **All I/O is fault-tolerant.** A logging failure must never break the user's actual answer, and a
  corrupt log line must never break the rollup. Reads return sensible defaults; the writer swallows
  every exception. This mirrors the router's state-file idiom (:mod:`tanglebrain.router`).
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tanglebrain.router import DEFAULT_STATE_SUBDIR, STATE_DIR_ENV

LOG_FILENAME = "usage.jsonl"

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
        is_placeholder: ``True`` while the rates are not yet the canonical monad-stats constants;
            the rollup renders a PLACEHOLDER caveat so no figure is mistaken for authoritative.
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

    Honors ``TANGLEBRAIN_STATE_DIR`` (``~`` expanded); otherwise ``~/.cache/tanglebrain/``. The log
    lives alongside the router's state file (same dir, same env override).

    Returns:
        The absolute path to the append-only usage JSONL file.
    """
    base = os.environ.get(STATE_DIR_ENV)
    root = Path(base).expanduser() if base else Path.home() / DEFAULT_STATE_SUBDIR
    return root / LOG_FILENAME


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
# Cloud-equivalent reference pricing for the C4 "spend avoided" rollup (plan §8).
#
# Methodology mirrors Monad-1's monad-stats `costSaved` (ratified 2026-06-13): for each routed
# task, estimate what it WOULD have cost on a paid frontier API, valued at a reference model's
# per-token price. The rollup multiplies estimated tokens (a chars/4 heuristic over the visible
# prompt + response) by these rates. Values are US dollars per 1,000,000 tokens.
#
# `placeholder: true` makes `tanglebrain --stats` flag every figure as PLACEHOLDER (use it if you
# fork the anchor before re-ratifying with the PM). Edited via `tanglebrain-gui` or by hand.
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
    """Return the directory for config backups (under the state dir, never the repo config dir)."""
    base = os.environ.get(STATE_DIR_ENV)
    root = Path(base).expanduser() if base else Path.home() / DEFAULT_STATE_SUBDIR
    return root / "backups"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir, then ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
        shutil.copy2(target, backup_dir / f"pricing-{stamp}.yaml")
    _atomic_write(target, _render_pricing(pricing, header))


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
    log_path: str | os.PathLike[str] | None = None,
    pricing: Pricing | None = None,
) -> None:
    """Append one usage record for a routed task. Never raises — a logging failure is dropped.

    Args:
        path: Which execution path served the task — ``router`` | ``local`` | ``model``.
        entry: The served :class:`~tanglebrain.roster.RosterEntry` (read for ``tier``/``id``); may
            be ``None`` (e.g. the router didn't surface one), in which case both are ``"unknown"``.
        prompt: The task prompt (for input-token estimation).
        response: The returned response text (for output-token estimation).
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
        # The paid-API tier (issue #2) is not built yet, so every routed task currently avoids the
        # full cloud-equivalent. When `api` lands, those tasks incur real spend → avoided = 0.
        avoided = 0.0 if tier == "api" else equiv
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "path": str(path),
            "tier": str(tier),
            "model": str(model),
            "in_tokens_est": in_tok,
            "out_tokens_est": out_tok,
            "cloud_equiv_usd": round(equiv, 6),
            "spend_avoided_usd": round(avoided, 6),
            "pricing_ref": pricing.reference_model,
        }
        target = Path(log_path) if log_path is not None else default_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        # Measurement is a side-effect: a failure here must never affect the returned answer.
        return


def _as_int(value: object) -> int:
    """Coerce a stored numeric field to int, defaulting to 0 on any bad value."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return 0


def _as_float(value: object) -> float:
    """Coerce a stored numeric field to float, defaulting to 0.0 on any bad value."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return 0.0


def read_records(log_path: str | os.PathLike[str] | None = None) -> list[dict]:
    """Read all usage records from the log, skipping malformed lines.

    Args:
        log_path: Override the usage-log path. Defaults to :func:`default_log_path`.

    Returns:
        The parsed records in file (chronological) order. An absent log yields ``[]``.
    """
    target = Path(log_path) if log_path is not None else default_log_path()
    records: list[dict] = []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def rollup(records: list[dict]) -> dict:
    """Aggregate usage records into a summary.

    Args:
        records: The records from :func:`read_records`.

    Returns:
        A dict with: ``tasks`` (int), ``by_tier`` (tier → count), ``in_tokens_est`` /
        ``out_tokens_est`` (summed estimates), and ``cloud_equiv_usd`` / ``spend_avoided_usd``
        (summed dollars).
    """
    summary: dict = {
        "tasks": 0,
        "by_tier": {},
        "in_tokens_est": 0,
        "out_tokens_est": 0,
        "cloud_equiv_usd": 0.0,
        "spend_avoided_usd": 0.0,
    }
    for r in records:
        summary["tasks"] += 1
        tier = str(r.get("tier", "unknown"))
        summary["by_tier"][tier] = summary["by_tier"].get(tier, 0) + 1
        summary["in_tokens_est"] += _as_int(r.get("in_tokens_est"))
        summary["out_tokens_est"] += _as_int(r.get("out_tokens_est"))
        summary["cloud_equiv_usd"] += _as_float(r.get("cloud_equiv_usd"))
        summary["spend_avoided_usd"] += _as_float(r.get("spend_avoided_usd"))
    summary["cloud_equiv_usd"] = round(summary["cloud_equiv_usd"], 4)
    summary["spend_avoided_usd"] = round(summary["spend_avoided_usd"], 4)
    return summary


def format_rollup(summary: dict, pricing: Pricing) -> str:
    """Render a rollup summary as a human-readable block for the CLI.

    Args:
        summary: The aggregate from :func:`rollup`.
        pricing: The currently-configured pricing (for the reference-model label + placeholder
            caveat). Per-record costs were computed when each task ran; this only labels the figure.

    Returns:
        A multi-line string suitable for printing.
    """
    lines = [
        "TangleBrain — spend avoided (cloud-equivalent)",
        f"  Tasks routed:   {summary.get('tasks', 0)}",
    ]
    by_tier = summary.get("by_tier") or {}
    if by_tier:
        tiers = ", ".join(f"{k} {v}" for k, v in sorted(by_tier.items()))
        lines.append(f"  By tier:        {tiers}")
    lines.append(
        f"  Est. tokens:    in {summary.get('in_tokens_est', 0):,} / "
        f"out {summary.get('out_tokens_est', 0):,}"
    )
    lines.append(f"  Spend avoided:  ${summary.get('spend_avoided_usd', 0.0):,.2f}")
    lines.append(f"  Pricing ref:    {pricing.reference_model}")
    if pricing.is_placeholder:
        lines.append(
            "  ⚠ pricing: PLACEHOLDER — figures are illustrative until the monad-stats "
            "costSaved constants land in config/pricing.yaml."
        )
    return "\n".join(lines)
