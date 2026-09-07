"""The uniform adapter interface.

Every tier — free local, authenticated CLI, paid API — is invoked through one shape:
``run(prompt, opts) -> text``. Routing logic above the adapters (the selector and the router) never
needs to know *how* a tier is reached, only that it can hand it a prompt and get text back. That
uniformity is what makes adding or removing a backend a local, contained change.
"""
from __future__ import annotations

from typing import Iterator, Mapping, Protocol, runtime_checkable


class AdapterError(RuntimeError):
    """Raised when an adapter cannot produce text.

    Covers bad config, transport/subprocess failure, and unexpected response shape — every
    way a tier can fail to return usable text. It lives here (not in a single adapter module)
    so all adapters and the routing layer share one error type to catch. ``openai_compat``
    re-exports it for backwards-compatible imports.
    """


#: Longest object key reproduced verbatim by :func:`describe_shape`. Schema field names are far
#: shorter; anything longer is more likely to be content that happened to land in key position.
_MAX_KEY_LEN = 40

#: How many keys :func:`describe_shape` names before it stops and counts the rest. An envelope has
#: a handful; a long list is noise in an error message rather than a diagnostic.
_MAX_KEYS = 8


def describe_shape(value: object) -> str:
    """Describe a value's shape without reproducing its content.

    Adapter errors are **persisted**: the router collects ``str(exc)`` into a task's ``failures``
    and :func:`~tanglebrain.measurement.record_task` writes that into the usage log. So an error
    that quotes the body it could not parse writes backend response text to disk, and
    ``docs/design/data-model.md`` § Invariants guarantees that never happens — grounding it in
    being *structural*: "a redaction filter can be bypassed by the next code path that forgets
    it; there is nothing to redact cannot." Quoting a body makes it procedural. This keeps it
    structural by never putting the content into the string in the first place.

    What survives is what diagnoses a misconfigured backend: the size of what arrived and, for
    an object, its field names. **Keys are named only when they look like schema** — an
    identifier-shaped, short name. A key that is neither is content that happened to land in key
    position, so it is counted rather than shown.

    Args:
        value: Anything an adapter received and could not use — decoded JSON, raw text, or a
            fragment of either.

    Returns:
        A short, content-free description, e.g. ``52 chars of text`` or
        ``object with keys ['result', 'subtype']``.
    """
    if value is None:
        return "null"
    if isinstance(value, str):
        return f"{len(value)} chars of text"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        if not value:
            return "empty object"
        named, hidden = [], 0
        for key in value:
            text = key if isinstance(key, str) else None
            if text is not None and text.isidentifier() and len(text) <= _MAX_KEY_LEN:
                named.append(text)
            else:
                hidden += 1
        shown, extra = named[:_MAX_KEYS], max(0, len(named) - _MAX_KEYS)
        unnamed = hidden + extra
        if not shown:
            return f"object with {len(value)} key(s), none schema-shaped"
        suffix = f" (+{unnamed} more)" if unnamed else ""
        return f"object with keys {shown!r}{suffix}"
    if isinstance(value, (list, tuple)):
        return f"array of {len(value)} item(s)"
    return type(value).__name__


@runtime_checkable
class Adapter(Protocol):
    """A callable tier: turn a prompt into text.

    Implementations call out to a specific transport (an OpenAI-compat HTTP endpoint, a
    subprocess CLI, a paid API) but expose only this uniform method.
    """

    def run(self, prompt: str, opts: Mapping[str, object] | None = None) -> str:
        """Run ``prompt`` against this tier and return the final text.

        Args:
            prompt: The prompt to send.
            opts: Optional per-call options (e.g. ``max_tokens``). Adapters ignore keys they
                do not understand.

        Returns:
            The tier's final response text.

        Raises:
            Exception: Adapters surface transport/protocol failures to the caller rather than
                retrying or falling back silently — the routing layer decides what to do next.
        """
        ...


@runtime_checkable
class StreamingAdapter(Protocol):
    """An OPTIONAL second capability: stream a prompt's response as text deltas.

    Adapters that can deliver tokens incrementally (an OpenAI-compat SSE endpoint) implement
    this **in addition to** :class:`Adapter` — the uniform ``run() -> str`` contract stays
    untouched, and callers that want streaming probe for it
    (``getattr(adapter, "run_stream", None)``) and fall back to ``run`` when absent. Adapters
    that cannot stream honestly (subprocess CLIs that parse one completed payload) simply do
    not implement it.
    """

    def run_stream(self, prompt: str, opts: Mapping[str, object] | None = None) -> Iterator[str]:
        """Run ``prompt`` against this tier, yielding response text deltas in order.

        The connection is opened lazily, on the first iteration — a caller can therefore pull
        the first delta before committing anything to its own client (connect-time failures
        surface as an exception from that first pull, not mid-stream).

        Args:
            prompt: The prompt to send.
            opts: Optional per-call options (e.g. ``max_tokens``). Adapters ignore keys they
                do not understand.

        Yields:
            Non-empty text fragments, in order; joined, they form the final response text.

        Raises:
            Exception: Configuration errors raise eagerly (at call time); transport/protocol
                failures raise from the iteration that hit them — before the first yield for
                connect-time failures, mid-iteration for a stream that dies part-way.
        """
        ...
