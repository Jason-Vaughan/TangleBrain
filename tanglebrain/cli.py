"""TangleBrain CLI — route one request and print the response.

Thin wiring over :func:`run_once`; the routing logic lives in the router/selector/adapters. The
path is chosen by flag precedence ``--model`` > ``--local`` > the frontier-first router (the
default since C3b): the router selects + rotates an orchestrator sub, fails over on errors, and
gives it the gpt-oss delegate so it offloads grunt to free local.

Usage::

    tanglebrain "Refactor this module and add tests."        # default: frontier-first router
    tanglebrain --task code "..."                            # task-fit hint for the router
    tanglebrain --local "Write a haiku about local inference."   # force the free local tier
    tanglebrain --model gemini "Summarize this long document."   # pin a specific roster entry
"""
from __future__ import annotations

import argparse
import sys

from tanglebrain.adapters import AdapterError
from tanglebrain.classifier import TRIVIAL, classify
from tanglebrain.measurement import (
    format_rollup,
    load_pricing,
    read_records,
    record_task,
    rollup,
)
from tanglebrain.roster import RosterError, load_roster
from tanglebrain.router import Router, RouterError
from tanglebrain.selector import SelectionError, build_adapter, select_by_id, select_local
from tanglebrain.settings import load_settings


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``tanglebrain`` command.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="tanglebrain",
        description=(
            "Route one request to the cheapest capable tier (frontier-first by default), or "
            "print the 'spend avoided' rollup with --stats."
        ),
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="The prompt to route. Optional only when --stats is given.",
    )
    parser.add_argument(
        "--roster",
        default=None,
        help="Path to a roster YAML (defaults to the packaged tanglebrain/config/roster.yaml).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Route to a specific roster entry by id (e.g. 'claude'). Without it, the default "
            "local-first selection is used. This is an explicit override of routing."
        ),
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help=(
            "Force the free local tier (gpt-oss) instead of the default frontier-first router. "
            "Use for a quick, $0, no-orchestration answer."
        ),
    )
    parser.add_argument(
        "--route",
        action="store_true",
        help="Deprecated/no-op: the frontier-first router is now the default. Kept for back-compat.",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Task-fit hint for the router (a good_at tag, e.g. 'code', 'reasoning', 'long-context').",
    )
    gate_group = parser.add_mutually_exclusive_group()
    gate_group.add_argument(
        "--gate",
        dest="gate",
        action="store_true",
        default=None,
        help="Force the §6 local classifier gate ON for this run: a cheap local classify sends "
        "trivial requests straight to free local, only frontier ones to a sub.",
    )
    gate_group.add_argument(
        "--no-gate",
        dest="gate",
        action="store_false",
        help="Force the classifier gate OFF (always frontier-first router), ignoring the setting.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Override the completion token cap (defaults to the adapter's 2048).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help=(
            "Print the 'spend avoided' rollup (cloud-equivalent cost of every routed task so far) "
            "and exit. No prompt needed."
        ),
    )
    return parser


def _served(path: str, entry) -> dict | None:
    """Build the ``{path, tier, model}`` served-summary for a routed task, or ``None``."""
    if entry is None:
        return None
    return {"path": path, "tier": entry.tier, "model": entry.id}


def run_once(
    prompt: str,
    roster_path: str | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
    local: bool = False,
    task: str | None = None,
    return_served: bool = False,
    gate: bool | None = None,
):
    """Route a single prompt to a roster tier and return the response text.

    Paths, in precedence order:

    - ``model`` set → select that named entry explicitly (an override, not a routing decision).
    - ``local`` true → the free local tier directly (gpt-oss), no orchestration (C1 behaviour).
    - otherwise → the default routing path. With the §6 **classifier gate** off (the default), this
      is **the frontier-first** :class:`~tanglebrain.router.Router`: task-fit orchestrator selection +
      rotation + failover across the subs, each given the gpt-oss delegate. With the gate on, a cheap
      local classify runs first: a *trivial* request is handled directly on free local (path
      ``gate-local``, skipping the subs), and everything else falls through to the router.

    Args:
        prompt: The prompt to route.
        roster_path: Optional roster YAML path (defaults to the packaged roster).
        max_tokens: Optional completion token cap (honoured by the openai-compat adapter; the
            CLI adapter ignores it, as each CLI controls its own limits).
        model: Optional roster entry id to route to explicitly.
        local: Force the free local tier instead of the frontier-first router.
        task: Optional task-fit hint for the router (a ``good_at`` tag).
        return_served: When ``True``, return ``(text, served)`` where ``served`` is
            ``{path, tier, model}`` for the entry that served the task (or ``None`` if unknown).
            The GUI uses this so it needn't re-read the usage log. Default ``False`` returns the
            plain text string, so existing callers (``main``) are unchanged.
        gate: Override for the §6 classifier gate on the default path. ``None`` (default) uses the
            ``classifier_gate_enabled`` setting; ``True``/``False`` force the gate on/off for this
            call. Ignored when ``model`` or ``local`` is set.

    Returns:
        The response text (``str``), or ``(text, served)`` when ``return_served`` is ``True``.

    Raises:
        RosterError: If the roster cannot be loaded.
        SelectionError: If ``model``/``local`` is used and no suitable entry is available.
        RouterError: If the router runs and no orchestrator can serve the request.
        AdapterError: If the adapter cannot produce text.
    """
    roster = load_roster(roster_path)
    opts = {"max_tokens": max_tokens} if max_tokens is not None else None

    if model is not None:
        path, entry = "model", select_by_id(roster, model)
        text = build_adapter(entry).run(prompt, opts)
    elif local:
        path, entry = "local", select_local(roster)
        text = build_adapter(entry).run(prompt, opts)
    else:
        gate_on = load_settings().classifier_gate_enabled if gate is None else gate
        if gate_on and classify(prompt, roster=roster) == TRIVIAL:
            # §6 classifier gate: a trivial request skips the rate-limited subs and is handled
            # directly on free local. Frontier (or any classifier failure) falls through to the router.
            path, entry = "gate-local", select_local(roster)
            text = build_adapter(entry).run(prompt, opts)
        else:
            path = "router"
            router = Router(roster)
            text = router.route(prompt, task=task, opts=opts)
            entry = router.last_served

    record_task(path=path, entry=entry, prompt=prompt, response=text)
    return (text, _served(path, entry)) if return_served else text


def main(argv: list[str] | None = None) -> int:
    """Console entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: ``0`` on success, ``1`` on a known TangleBrain error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.stats:
        print(format_rollup(rollup(read_records()), load_pricing()))
        return 0

    if args.prompt is None:
        parser.error("prompt is required (unless --stats is given)")

    try:
        text = run_once(
            args.prompt,
            roster_path=args.roster,
            max_tokens=args.max_tokens,
            model=args.model,
            local=args.local,
            task=args.task,
            gate=args.gate,
        )
    except (RosterError, SelectionError, RouterError, AdapterError) as exc:
        print(f"tanglebrain: {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
