"""TangleBrain CLI — route one request and print the response.

Thin wiring over :func:`run_once`; the routing logic lives in the router/selector/adapters. The
path is chosen by flag precedence ``--model`` > ``--local`` > the frontier-first router (the
default): the router selects + rotates an orchestrator, fails over on errors, and gives it the
local-delegate tool so it offloads sub-tasks to the free local backend.

Usage::

    tanglebrain "Refactor this module and add tests."        # default: frontier-first router
    tanglebrain --task code "..."                            # task-fit hint for the router
    tanglebrain --local "Write a haiku about local inference."   # force the free local tier
    tanglebrain --model gemini "Summarize this long document."   # pin a specific roster entry
"""
from __future__ import annotations

import argparse
import sys
import uuid
from typing import Iterator

from tanglebrain import __version__
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
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Print the TangleBrain version and exit.",
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
        help="Force the local classifier gate ON for this run: a cheap local classify sends "
        "trivial requests straight to the free local backend, and only frontier ones to an "
        "orchestrator.",
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


def _served(path: str, entry, task_id: str) -> dict | None:
    """Build the ``{path, tier, model, task_id}`` served-summary for a routed task, or ``None``.

    Args:
        path: The routing path that served the task (``model``/``local``/``gate-local``/``router``).
        entry: The serving :class:`~tanglebrain.roster.RosterEntry`, or ``None`` when unknown.
        task_id: The task id minted for this run (links the caller's view of the task to its
            usage record — e.g. the serve endpoint uses it as the completion id).

    Returns:
        The served-summary dict, or ``None`` when the serving entry is unknown.
    """
    if entry is None:
        return None
    return {"path": path, "tier": entry.tier, "model": entry.id, "task_id": task_id}


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
    - ``local`` true → the free local tier directly, no orchestration.
    - otherwise → the default routing path. With the **classifier gate** off (the default), this
      is **the frontier-first** :class:`~tanglebrain.router.Router`: task-fit orchestrator selection +
      rotation + failover across the orchestrators, each given the local-delegate tool. With the gate
      on, a cheap local classify runs first: a *trivial* request is handled directly on the free local
      backend (path ``gate-local``, skipping the orchestrators), and everything else falls through to
      the router.

    Args:
        prompt: The prompt to route.
        roster_path: Optional roster YAML path (defaults to the packaged roster).
        max_tokens: Optional completion token cap (honoured by the openai-compat adapter; the
            CLI adapter ignores it, as each CLI controls its own limits).
        model: Optional roster entry id to route to explicitly.
        local: Force the free local tier instead of the frontier-first router.
        task: Optional task-fit hint for the router (a ``good_at`` tag).
        return_served: When ``True``, return ``(text, served)`` where ``served`` is
            ``{path, tier, model, task_id}`` for the entry that served the task (or ``None`` if
            unknown). The GUI and the serve endpoint use this so they needn't re-read the usage
            log. Default ``False`` returns the plain text string, so existing callers (``main``)
            are unchanged.
        gate: Override for the classifier gate on the default path. ``None`` (default) uses the
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
    # Mint a task id for this routed task. It is recorded on the task and threaded through opts so
    # the orchestrator-CLI adapter can propagate it to delegated sub-calls (see CliAdapter.run /
    # PARENT_TASK_ID_ENV), linking the delegation tree back to this task. Cheap and side-effect-free
    # to mint on every path; only the router path (orchestrators with the delegate tool) acts on it.
    task_id = uuid.uuid4().hex
    opts: dict = {"task_id": task_id}
    if max_tokens is not None:
        opts["max_tokens"] = max_tokens

    if model is not None:
        path, entry = "model", select_by_id(roster, model)
        text = build_adapter(entry).run(prompt, opts)
    elif local:
        path, entry = "local", select_local(roster)
        text = build_adapter(entry).run(prompt, opts)
    else:
        gate_on = load_settings().classifier_gate_enabled if gate is None else gate
        if gate_on and classify(prompt, roster=roster) == TRIVIAL:
            # classifier gate: a trivial request skips the orchestrators and is handled directly on
            # the free local backend. Frontier (or any classifier failure) falls through to the router.
            path, entry = "gate-local", select_local(roster)
            text = build_adapter(entry).run(prompt, opts)
        else:
            path = "router"
            router = Router(roster)
            text = router.route(prompt, task=task, opts=opts)
            entry = router.last_served

    record_task(path=path, entry=entry, prompt=prompt, response=text, task_id=task_id)
    return (text, _served(path, entry, task_id)) if return_served else text


def _recording_stream(
    deltas: Iterator[str], path: str, entry, prompt: str, task_id: str
) -> Iterator[str]:
    """Wrap a delta stream so the task is metered exactly once, however the stream ends.

    Accumulates every yielded fragment and calls
    :func:`~tanglebrain.measurement.record_task` with the joined text when the stream finishes.
    Three endings are handled:

    - **Normal exhaustion** — record the full text (parity with :func:`run_once`).
    - **Mid-stream adapter failure** — record the partial text *if any was produced* (it was
      real backend spend), then re-raise so the caller can frame the error. A failure before
      the first fragment records nothing, matching ``run_once`` (which never meters a task
      that produced no text).
    - **Abandoned stream** (caller ``close()``/GC) — record the partial text if any.

    Args:
        deltas: The adapter's delta iterator.
        path: The routing path label (``model``/``local``/``gate-local``).
        entry: The serving roster entry.
        prompt: The routed prompt (for the usage estimate).
        task_id: The task id minted for this run.

    Yields:
        The fragments of ``deltas``, unchanged.
    """
    pieces: list[str] = []
    recorded = False

    def _record(require_text: bool) -> None:
        nonlocal recorded
        if recorded or (require_text and not pieces):
            return
        recorded = True
        record_task(
            path=path, entry=entry, prompt=prompt, response="".join(pieces), task_id=task_id
        )

    try:
        for piece in deltas:
            pieces.append(piece)
            yield piece
    except GeneratorExit:
        _record(require_text=True)
        raise
    except Exception:
        # Deliberately Exception, not BaseException: on KeyboardInterrupt/SystemExit we skip
        # metering I/O and just propagate — don't "fix" this to catch interrupts.
        _record(require_text=True)
        raise
    _record(require_text=False)


def run_once_stream(
    prompt: str,
    roster_path: str | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
    local: bool = False,
    task: str | None = None,
    gate: bool | None = None,
) -> tuple[Iterator[str], dict | None]:
    """Route a single prompt like :func:`run_once`, delivering the response as a delta stream.

    Path precedence, task-id minting, gates, and metering are identical to :func:`run_once`;
    what differs is delivery:

    - **model / local / gate-local** paths: when the built adapter implements the optional
      :class:`~tanglebrain.adapters.base.StreamingAdapter` capability, its deltas are passed
      through incrementally (the connection opens on the first pull — see the capability's
      laziness contract). An adapter without ``run_stream`` runs blocking and the full text is
      delivered as a single-item stream (per-backend emulation).
    - **router** path: always blocking ``Router.route()`` framed as a single-item stream —
      the c13 v2 stance (orchestrators are cli-kind; per-CLI streaming is deferred to v3).

    Metering: streamed paths record on stream completion (partial text on mid-stream failure or
    abandonment — see :func:`_recording_stream`); emulated paths record before returning, since
    the spend has already happened by then.

    Args:
        prompt: The prompt to route.
        roster_path: Optional roster YAML path (defaults to the packaged roster).
        max_tokens: Optional completion token cap (honoured by the openai-compat adapter).
        model: Optional roster entry id to route to explicitly.
        local: Force the free local tier instead of the frontier-first router.
        task: Optional task-fit hint for the router (a ``good_at`` tag).
        gate: Classifier-gate override for the default path, as in :func:`run_once`.

    Returns:
        ``(deltas, served)`` — ``deltas`` yields response text fragments in order (joined, they
        form the full response); ``served`` is ``{path, tier, model, task_id}`` for the entry
        that serves the request (or ``None`` when unknown), resolved **before** the first delta
        on every path.

    Raises:
        RosterError: If the roster cannot be loaded.
        SelectionError: If ``model``/``local`` is used and no suitable entry is available.
        RouterError: If the router runs and no orchestrator can serve the request.
        AdapterError: Raised from ``deltas`` — on the first pull for connect-time failures,
            mid-iteration for a stream that dies part-way. Emulated (blocking) paths raise it
            from this call directly, before any stream exists.
    """
    roster = load_roster(roster_path)
    task_id = uuid.uuid4().hex
    opts: dict = {"task_id": task_id}
    if max_tokens is not None:
        opts["max_tokens"] = max_tokens

    if model is not None:
        path, entry = "model", select_by_id(roster, model)
    elif local:
        path, entry = "local", select_local(roster)
    else:
        gate_on = load_settings().classifier_gate_enabled if gate is None else gate
        if gate_on and classify(prompt, roster=roster) == TRIVIAL:
            path, entry = "gate-local", select_local(roster)
        else:
            # Router path: blocking route + single-item stream (v2 emulation; Router untouched).
            router = Router(roster)
            text = router.route(prompt, task=task, opts=opts)
            entry = router.last_served
            record_task(path="router", entry=entry, prompt=prompt, response=text, task_id=task_id)
            return iter([text]), _served("router", entry, task_id)

    adapter = build_adapter(entry)
    run_stream = getattr(adapter, "run_stream", None)
    if run_stream is None:
        # Per-backend emulation: no streaming capability — run blocking, frame as one delta.
        text = adapter.run(prompt, opts)
        record_task(path=path, entry=entry, prompt=prompt, response=text, task_id=task_id)
        return iter([text]), _served(path, entry, task_id)

    deltas = _recording_stream(run_stream(prompt, opts), path, entry, prompt, task_id)
    return deltas, _served(path, entry, task_id)


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
