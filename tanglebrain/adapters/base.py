"""The uniform adapter interface (plan §4).

Every tier — free local, subscription CLI, paid API — is invoked through one shape:
``run(prompt, opts) -> text``. Routing logic above the adapters (the selector in C1, the
cost-tiered router in C3) never needs to know *how* a tier is reached, only that it can hand
it a prompt and get text back. That uniformity is what makes adding or removing a model a
local, contained change.
"""
from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable


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
