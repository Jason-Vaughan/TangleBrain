"""Paid-API adapter — the last-resort tier.

**The paid-API tier reuses the *same* transport as the free local tier**, on purpose. Paid APIs are
fronted through an OpenAI-compatible gateway (e.g. LiteLLM): TangleBrain never holds a raw provider
key — it references a scoped key (via ``key_ref``) and calls the gateway's OpenAI-compatible
``/chat/completions`` endpoint, exactly as :class:`~tanglebrain.adapters.openai_compat.OpenAICompatAdapter`
does for a local backend. So this adapter is a thin specialization of that one — the transport is
identical; what makes the ``api`` tier different is **policy, not plumbing**:

- it only exists behind the ``api_billing_enabled`` gate + the entry's ``enabled`` flag
  (enforced in :func:`tanglebrain.selector.build_adapter`, not here), and
- it is routed **last resort**, and
- its per-key monthly budget is capped gateway-side on the key.

Subclassing keeps that "same transport, different policy" relationship explicit and avoids
duplicating the httpx/error-handling block. If the paid tier ever needs genuinely different
transport behaviour, override :meth:`run` here.
"""
from __future__ import annotations

from tanglebrain.adapters.openai_compat import OpenAICompatAdapter

__all__ = ["ApiAdapter"]


class ApiAdapter(OpenAICompatAdapter):
    """Adapter for a ``tier: api`` roster entry — a LiteLLM-fronted paid model.

    Identical transport to :class:`OpenAICompatAdapter` (OpenAI-compat ``/chat/completions`` with a
    Bearer credential resolved from ``key_ref``); it exists as its own type so the routing/selection
    layer can reason about "this is the paid tier" and so the gate/last-resort policy has a clear
    home. The billing gate is enforced by the caller (:func:`tanglebrain.selector.build_adapter`),
    never inside the transport.
    """

    #: Same transport as the base, different tier. Setting this is the whole of the difference
    #: `from_entry` needs — the loader has already guaranteed `base_url`, `model` and `key_ref` for
    #: an `api` entry, and the credential resolves lazily on first `run`, so the raw virtual key is
    #: still never read at construction time.
    _EXPECTED_INVOKE_KIND = "api"
