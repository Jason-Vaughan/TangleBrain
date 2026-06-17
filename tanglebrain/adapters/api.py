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

from tanglebrain.adapters.base import AdapterError
from tanglebrain.adapters.openai_compat import OpenAICompatAdapter
from tanglebrain.roster import RosterEntry

__all__ = ["ApiAdapter"]


class ApiAdapter(OpenAICompatAdapter):
    """Adapter for a ``tier: api`` roster entry — a LiteLLM-fronted paid model.

    Identical transport to :class:`OpenAICompatAdapter` (OpenAI-compat ``/chat/completions`` with a
    Bearer credential resolved from ``key_ref``); it exists as its own type so the routing/selection
    layer can reason about "this is the paid tier" and so the gate/last-resort policy has a clear
    home. The billing gate is enforced by the caller (:func:`tanglebrain.selector.build_adapter`),
    never inside the transport.
    """

    @classmethod
    def from_entry(cls, entry: RosterEntry, **overrides: object) -> "ApiAdapter":
        """Build an adapter from an ``api`` roster entry.

        The roster loader has already guaranteed ``base_url``, ``model`` and ``key_ref`` are present
        for an ``api`` entry, and the credential is resolved lazily (on first :meth:`run`), so the
        raw virtual key is never read at construction time.

        Args:
            entry: A roster entry whose ``invoke.kind`` is ``api``.
            **overrides: Optional constructor overrides (``timeout``, ``default_max_tokens``).

        Returns:
            A configured :class:`ApiAdapter`.

        Raises:
            AdapterError: If the entry's invoke kind is not ``api``.
        """
        if entry.invoke.kind != "api":
            raise AdapterError(
                f"entry {entry.id!r} has invoke.kind {entry.invoke.kind!r}, not 'api'"
            )
        return cls(
            base_url=entry.invoke.base_url,  # validated non-None by the roster loader
            model=entry.invoke.model,
            key_ref=entry.invoke.key_ref,
            **overrides,  # type: ignore[arg-type]
        )
