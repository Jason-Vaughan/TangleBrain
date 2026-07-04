"""Serve views — pure, transport-free OpenAI request/response translation.

These hold all of the serve endpoint's logic so it can be unit-tested without binding a socket
(mirroring :mod:`tanglebrain.gui.views`). The HTTP layer in :mod:`tanglebrain.serve.server` only
routes requests to these and serializes the result.

Translation contract (issue #70):

- ``model: "auto"`` → the full router (the same default path as a bare ``tanglebrain`` run,
  classifier gate honored per settings). A roster entry id → explicit pin (parity with
  ``--model``). Unknown ids → an OpenAI-style ``model_not_found`` error — never a silent fallback.
  An **absent** ``model`` defaults to ``auto`` (pointing at TangleBrain *is* the routing choice);
  a present-but-empty or non-string ``model`` is rejected, so a truncated client config fails
  loudly instead of silently spending router quota.
- OpenAI chat ``messages`` arrays are flattened to the plain prompt the adapters take: each
  message renders as a ``[role]``-tagged block, joined by blank lines, in order. Text content
  parts are concatenated; non-text parts (images, audio) are rejected with a clear error rather
  than silently dropped.
- ``stream: true`` is real where the backend can stream (c13 v2, issue #73): the request runs
  through :func:`~tanglebrain.cli.run_once_stream`, and ``chat.completion.chunk`` deltas flow as
  the backend produces them (pinned ``openai-compat``/``api`` entries, and the gate-local path).
  Backends that cannot stream (``cli`` kinds, the full-router ``auto`` path) still deliver the
  completed text as a single chunk — same client contract, no early tokens. Early failures (before
  the first delta) return plain JSON errors with the right status; a stream that dies mid-way ends
  with one in-stream ``{"error": ...}`` event and **no** ``[DONE]`` (see :func:`sse_stream_events`).
- Sampling knobs (``temperature``, ``top_p``, ``n``, …) and tool definitions are accepted and
  ignored — the serving backend controls its own generation, and orchestrator CLIs bring their
  own tools. ``max_tokens`` is honored (passed through to the adapter).

The paid-API tier stays behind both existing billing gates; serving changes nothing about them.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Iterator

from tanglebrain.adapters import AdapterError
from tanglebrain.cli import run_once, run_once_stream
from tanglebrain.measurement import estimate_tokens
from tanglebrain.roster import RosterError, load_roster
from tanglebrain.router import RouterError
from tanglebrain.selector import SelectionError

# Default serve port (leased permanently in the operator's port registry; see the README).
DEFAULT_PORT = 3251

# The model-param alias that engages the full router.
AUTO_ALIAS = "auto"

_OWNED_BY = "tanglebrain"


class BadRequestError(ValueError):
    """An invalid request payload — maps to HTTP 400 with an OpenAI-style error body."""


def error_envelope(message: str, err_type: str, code: str | None = None) -> dict:
    """Build an OpenAI-style error body.

    Args:
        message: Human-readable error detail.
        err_type: OpenAI error type (e.g. ``invalid_request_error``).
        code: Optional machine-readable code (e.g. ``model_not_found``).

    Returns:
        ``{"error": {"message", "type", "code"}}``.
    """
    return {"error": {"message": message, "type": err_type, "code": code}}


def _part_text(part: object, index: int) -> str:
    """Extract the text from one OpenAI content part, rejecting non-text parts.

    Args:
        part: One element of a message's content-parts array.
        index: The owning message's position, for error messages.

    Returns:
        The part's text.

    Raises:
        BadRequestError: If the part is not a well-formed ``{"type": "text", "text": ...}`` part.
    """
    if not isinstance(part, dict):
        raise BadRequestError(f"messages[{index}]: each content part must be an object")
    kind = part.get("type")
    if kind != "text":
        raise BadRequestError(
            f"messages[{index}]: unsupported content part type {kind!r} (text only)"
        )
    text = part.get("text")
    if not isinstance(text, str):
        raise BadRequestError(f"messages[{index}]: text content part is missing string 'text'")
    return text


def flatten_messages(messages: object) -> str:
    """Flatten an OpenAI chat ``messages`` array into the plain prompt adapters take.

    Each message renders as ``[role]\\n{content}``; blocks are joined by blank lines in order, so
    the transcript is lossless and deterministic. String content passes through; content-parts
    arrays have their ``text`` parts concatenated. Anything else is rejected — a non-text part
    (image, audio) must fail loudly rather than be silently dropped.

    Args:
        messages: The raw ``messages`` value from the request payload.

    Returns:
        The flattened prompt string.

    Raises:
        BadRequestError: If ``messages`` is not a non-empty list of well-formed message objects.
    """
    if not isinstance(messages, list) or not messages:
        raise BadRequestError("'messages' must be a non-empty array of message objects")

    blocks: list[str] = []
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            raise BadRequestError(f"messages[{i}]: each message must be an object")
        role = message.get("role")
        if not isinstance(role, str) or not role:
            raise BadRequestError(f"messages[{i}]: 'role' must be a non-empty string")
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(_part_text(part, i) for part in content)
        else:
            raise BadRequestError(
                f"messages[{i}]: 'content' must be a string or an array of text parts"
            )
        blocks.append(f"[{role}]\n{text}")
    return "\n\n".join(blocks)


def wants_stream(payload: dict) -> bool:
    """Return whether the request asked for a streaming (SSE) response.

    Args:
        payload: The parsed request body.

    Returns:
        ``True`` only when ``stream`` is JSON ``true``. Any other value (including truthy
        strings like ``"false"``) is treated as non-streaming — a sloppy client degrades to a
        plain JSON envelope it can still read, rather than getting SSE it didn't mean to ask for.
    """
    return payload.get("stream") is True


def completion_envelope(text: str, served: dict | None, requested_model: str, prompt: str) -> dict:
    """Build the OpenAI ``chat.completion`` response body for a routed result.

    ``model`` carries the **served roster id** — which backend actually answered is the useful
    fact for an orchestrated endpoint; the requested alias plus routing detail land in the
    ``tanglebrain`` extension field. ``usage`` reuses the measurement heuristic
    (:func:`~tanglebrain.measurement.estimate_tokens`) and is flagged estimated.

    Args:
        text: The routed response text.
        served: ``run_once``'s served summary (``{path, tier, model, task_id}``) or ``None``.
        requested_model: The ``model`` value from the request (after the ``auto`` default).
        prompt: The flattened prompt (for the usage estimate).

    Returns:
        The response body dict.
    """
    served = served or {}
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(text)
    return {
        "id": f"chatcmpl-{served.get('task_id') or uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": served.get("model") or requested_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "tanglebrain": {
            "requested_model": requested_model,
            "path": served.get("path"),
            "tier": served.get("tier"),
            "tokens_estimated": True,
        },
    }


def _sse_frame(event: dict | str) -> bytes:
    """Frame one SSE event (a JSON-serializable chunk, or the literal ``[DONE]``) as bytes."""
    data = event if isinstance(event, str) else json.dumps(event)
    return f"data: {data}\n\n".encode("utf-8")


def sse_stream_events(
    first: str,
    rest: Iterator[str],
    served: dict | None,
    requested_model: str,
    prompt: str,
) -> Iterator[bytes]:
    """Frame a routed delta stream as ``chat.completion.chunk`` SSE events (c13 D5/D6).

    The caller has already pulled ``first`` (prime-the-pump: connect-time failures surface
    before any headers commit), so every event this yields belongs to a stream that genuinely
    started. Event sequence: a role+content chunk carrying ``first``, one content chunk per
    remaining delta, a finish chunk (``finish_reason: stop``) that always carries the estimated
    ``usage`` block and the ``tanglebrain`` extension, then ``[DONE]``.

    A mid-stream backend failure (``AdapterError``/``RouterError`` from ``rest``) ends the
    stream with a single ``{"error": ...}`` event and **no** finish chunk or ``[DONE]`` — the
    absence tells spec-following clients the stream terminated abnormally rather than lying
    with a fake ``stop``.

    Args:
        first: The already-pulled first delta.
        rest: The remaining deltas (a :func:`~tanglebrain.cli.run_once_stream` iterator, so
            metering happens as a side-effect of exhaustion).
        served: ``run_once_stream``'s served summary (``{path, tier, model, task_id}``) or
            ``None``.
        requested_model: The ``model`` value from the request (after the ``auto`` default).
        prompt: The flattened prompt (for the usage estimate).

    Yields:
        Complete ``data: ...\\n\\n`` SSE event byte strings, one per event.
    """
    served = served or {}
    head = {
        "id": f"chatcmpl-{served.get('task_id') or uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": served.get("model") or requested_model,
    }

    def chunk(delta: dict, finish_reason: str | None = None) -> dict:
        return {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

    pieces = [first]
    yield _sse_frame(chunk({"role": "assistant", "content": first}))
    try:
        for piece in rest:
            pieces.append(piece)
            yield _sse_frame(chunk({"content": piece}))
    except (AdapterError, RouterError) as exc:
        yield _sse_frame({"error": {"message": str(exc), "type": "upstream_error", "code": None}})
        return

    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens("".join(pieces))
    finish = chunk({}, finish_reason="stop")
    finish["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    finish["tanglebrain"] = {
        "requested_model": requested_model,
        "path": served.get("path"),
        "tier": served.get("tier"),
        "tokens_estimated": True,
    }
    yield _sse_frame(finish)
    yield _sse_frame("[DONE]")


def list_models() -> dict:
    """Build the ``GET /v1/models`` body: the ``auto`` alias plus every roster entry id.

    All entries are listed regardless of routability (a gated paid entry still shows — whether it
    can serve is a gate question, not a discovery one), mirroring the roster's always-inspectable
    stance.

    Returns:
        The OpenAI model-list body.

    Raises:
        RosterError: If the roster cannot be loaded.
    """
    roster = load_roster()
    data = [{"id": AUTO_ALIAS, "object": "model", "owned_by": _OWNED_BY}]
    data.extend({"id": e.id, "object": "model", "owned_by": _OWNED_BY} for e in roster.entries)
    return {"object": "list", "data": data}


def _parse_chat_request(payload: dict) -> tuple[str, str, int | None]:
    """Validate and extract ``(model, prompt, max_tokens)`` from a chat-completions payload.

    Shared by the plain and streaming handlers so a bad request fails identically on both.

    Args:
        payload: The parsed JSON request body (a dict).

    Returns:
        ``(model, prompt, max_tokens)`` — model after the absent→``auto`` default, prompt
        flattened via :func:`flatten_messages`.

    Raises:
        BadRequestError: On a malformed ``model``, ``messages``, or ``max_tokens``.
    """
    # Only an ABSENT model defaults to auto. A present-but-falsy value ("", null, 0) is a
    # broken client config and must fail loudly, never silently engage the router.
    model = payload.get("model", AUTO_ALIAS)
    if not isinstance(model, str) or not model:
        raise BadRequestError("'model' must be a non-empty string (or omitted for 'auto')")
    prompt = flatten_messages(payload.get("messages"))
    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise BadRequestError("'max_tokens' must be a positive integer")
    return model, prompt, max_tokens


def handle_chat_completion_stream(payload: dict) -> tuple[int, dict | Iterator[bytes]]:
    """Handle one ``stream: true`` ``POST /v1/chat/completions`` request body.

    Same validation and error mapping as :func:`handle_chat_completion`, but the request runs
    through :func:`~tanglebrain.cli.run_once_stream` and the pump is primed here: the first
    delta is pulled **before** anything is returned, so every failure up to and including the
    backend connection comes back as a plain ``(status, error_body)`` pair — the transport
    never commits SSE headers for a request that dies before its first token (c13 D4).

    Args:
        payload: The parsed JSON request body (a dict).

    Returns:
        ``(status, body)`` where a non-200 ``body`` is an OpenAI-style error dict (serialize as
        JSON), and a 200 ``body`` is the :func:`sse_stream_events` byte iterator (write as
        ``text/event-stream``, one event per pull).
    """
    try:
        model, prompt, max_tokens = _parse_chat_request(payload)
    except BadRequestError as exc:
        return 400, error_envelope(str(exc), "invalid_request_error")

    pinned = None if model == AUTO_ALIAS else model
    try:
        deltas, served = run_once_stream(prompt, model=pinned, max_tokens=max_tokens)
        first = next(deltas)
    except StopIteration:
        # Defensive: adapters raise on zero-content streams, and emulated paths always carry
        # one item — but a well-behaved endpoint maps an impossible-empty stream to 502 anyway.
        return 502, error_envelope("stream ended before any content", "upstream_error")
    except SelectionError as exc:
        if pinned is not None:
            # The only selection to fail on the pinned path is the id lookup itself.
            return 404, error_envelope(str(exc), "invalid_request_error", "model_not_found")
        return 502, error_envelope(str(exc), "upstream_error")
    except (RouterError, AdapterError) as exc:
        return 502, error_envelope(str(exc), "upstream_error")
    except RosterError as exc:
        return 500, error_envelope(str(exc), "server_error")

    return 200, sse_stream_events(first, deltas, served, model, prompt)


def handle_chat_completion(payload: dict) -> tuple[int, dict]:
    """Handle one ``POST /v1/chat/completions`` request body.

    Resolves the model directive, flattens the messages, runs the request through
    :func:`~tanglebrain.cli.run_once` (so measurement and every gate behave exactly as the CLI),
    and maps outcomes to OpenAI-style status + body:

    - success → ``(200, completion envelope)``
    - malformed payload → ``(400, invalid_request_error)``
    - unknown pinned model → ``(404, model_not_found)``
    - routing/backend failure → ``(502, upstream_error)`` with the failure detail
    - broken roster config → ``(500, server_error)``

    Args:
        payload: The parsed JSON request body (a dict).

    Returns:
        ``(status_code, body_dict)``. This is the non-streaming handler — a ``stream: true``
        request is routed to :func:`handle_chat_completion_stream` by the transport instead.
    """
    try:
        model, prompt, max_tokens = _parse_chat_request(payload)
    except BadRequestError as exc:
        return 400, error_envelope(str(exc), "invalid_request_error")

    pinned = None if model == AUTO_ALIAS else model
    try:
        text, served = run_once(prompt, model=pinned, max_tokens=max_tokens, return_served=True)
    except SelectionError as exc:
        if pinned is not None:
            # The only selection to fail on the pinned path is the id lookup itself.
            return 404, error_envelope(str(exc), "invalid_request_error", "model_not_found")
        return 502, error_envelope(str(exc), "upstream_error")
    except (RouterError, AdapterError) as exc:
        return 502, error_envelope(str(exc), "upstream_error")
    except RosterError as exc:
        return 500, error_envelope(str(exc), "server_error")

    return 200, completion_envelope(text, served, model, prompt)
