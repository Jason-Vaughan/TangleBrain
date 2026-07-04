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
