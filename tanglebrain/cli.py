"""TangleBrain CLI — route one request to the free local tier end-to-end (C1).

This is the C1 end-to-end wiring: load the roster, pick the local entry, build its adapter,
run the prompt, print the text. It is intentionally thin — the cost-tiered routing it will
eventually front lives in later chunks (see ``.claude/plans/tanglebrain.md``).

Usage::

    tanglebrain "Write a haiku about local inference."
    tanglebrain --roster path/to/roster.yaml --max-tokens 1024 "..."
"""
from __future__ import annotations

import argparse
import sys

from tanglebrain.adapters import AdapterError
from tanglebrain.roster import RosterError, load_roster
from tanglebrain.selector import SelectionError, build_adapter, select_by_id, select_local


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``tanglebrain`` command.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="tanglebrain",
        description="Route one request to the free local tier (C1).",
    )
    parser.add_argument("prompt", help="The prompt to route.")
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
            "local-first selection is used. This is an explicit override, not the C3 router."
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Override the completion token cap (defaults to the adapter's 2048).",
    )
    return parser


def run_once(
    prompt: str,
    roster_path: str | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
) -> str:
    """Route a single prompt to a roster tier and return the response text.

    With ``model`` unset, the default local-first selection is used (C1 behaviour). With
    ``model`` set, the named roster entry is selected explicitly (not a routing decision — the
    cost-tiered router is C3). The adapter for the entry's kind is built and run either way.

    Args:
        prompt: The prompt to route.
        roster_path: Optional roster YAML path (defaults to the packaged roster).
        max_tokens: Optional completion token cap (honoured by the openai-compat adapter; the
            CLI adapter ignores it, as each CLI controls its own limits).
        model: Optional roster entry id to route to explicitly.

    Returns:
        The selected tier's response text.

    Raises:
        RosterError: If the roster cannot be loaded.
        SelectionError: If no suitable entry is available.
        AdapterError: If the adapter cannot produce text.
    """
    roster = load_roster(roster_path)
    entry = select_by_id(roster, model) if model is not None else select_local(roster)
    adapter = build_adapter(entry)
    opts = {"max_tokens": max_tokens} if max_tokens is not None else None
    return adapter.run(prompt, opts)


def main(argv: list[str] | None = None) -> int:
    """Console entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: ``0`` on success, ``1`` on a known TangleBrain error.
    """
    args = build_parser().parse_args(argv)
    try:
        text = run_once(
            args.prompt,
            roster_path=args.roster,
            max_tokens=args.max_tokens,
            model=args.model,
        )
    except (RosterError, SelectionError, AdapterError) as exc:
        print(f"tanglebrain: {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
