"""OpenAI-compat adapter — the free local tier (plan §4, C1).

Calls an OpenAI-compatible ``/chat/completions`` endpoint (the local LiteLLM gateway in
front of gpt-oss-120b) and returns the final text. It calls LiteLLM **directly** — it does
not depend on coordinator's MCP server (that was only the C0 spike vehicle).

Behaviour mirrors the C0 spike's ``_call_litellm`` (coordinator ``openclaw-local-mcp``):

- Returns only ``choices[0].message.content`` — gpt-oss's chain-of-thought lands in a
  separate ``reasoning_content`` field and is intentionally dropped.
- Defaults ``max_tokens`` to 2048: gpt-oss spends part of its budget on internal reasoning
  before emitting the final answer, so a stingy cap truncates real output (the C0 lesson).
- Raises on any non-2xx status, transport failure, or unexpected response shape. This layer
  does NOT retry or fall back — failures surface to the routing layer, which decides.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

import httpx

from tanglebrain.roster import RosterEntry

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_TOKENS = 2048


class AdapterError(RuntimeError):
    """Raised when an adapter cannot produce text (bad config, transport error, bad shape)."""


def resolve_key_ref(key_ref: str | None) -> str | None:
    """Resolve a roster ``key_ref`` to a credential string, without embedding secrets.

    Supported forms (see the contract's key-ref convention):

    - ``file:PATH`` — read the key from a file (``~`` is expanded); the file is the source of
      truth, never the config.
    - ``env:NAME`` — read the key from environment variable ``NAME``.
    - ``none`` (or ``None``) — no credential; the endpoint is open.

    Args:
        key_ref: The reference string from the roster entry, or ``None``.

    Returns:
        The resolved key, or ``None`` for an open endpoint.

    Raises:
        AdapterError: If the form is unrecognized, or the referenced file/env var is missing
            or empty.
    """
    if key_ref is None or key_ref == "none":
        return None

    if key_ref.startswith("file:"):
        raw_path = key_ref[len("file:"):]
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise AdapterError(f"key_ref file not found: {path}")
        key = path.read_text().strip()
        if not key:
            raise AdapterError(f"key_ref file is empty: {path}")
        return key

    if key_ref.startswith("env:"):
        name = key_ref[len("env:"):]
        key = os.environ.get(name)
        if not key:
            raise AdapterError(f"key_ref env var not set or empty: {name}")
        return key

    raise AdapterError(
        f"unrecognized key_ref {key_ref!r}; expected 'file:PATH', 'env:NAME', or 'none'"
    )


class OpenAICompatAdapter:
    """Adapter that runs prompts against an OpenAI-compat chat-completions endpoint.

    Implements the uniform :class:`~tanglebrain.adapters.base.Adapter` interface
    (``run(prompt, opts) -> text``).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        key_ref: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        """Configure the adapter.

        The credential is resolved lazily (on first :meth:`run`), so constructing an adapter
        for an entry whose key file is absent does not fail until it is actually invoked.

        Args:
            base_url: OpenAI-compat base URL (e.g. ``http://litellm...:4000/v1``).
            model: Model id/alias to request (e.g. ``gpt-oss-120b``).
            key_ref: Credential reference (``file:PATH`` | ``env:NAME`` | ``none``), or ``None``.
            timeout: Per-request timeout in seconds.
            default_max_tokens: ``max_tokens`` used when a call does not override it.
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.key_ref = key_ref
        self.timeout = timeout
        self.default_max_tokens = default_max_tokens

    @classmethod
    def from_entry(cls, entry: RosterEntry, **overrides: object) -> "OpenAICompatAdapter":
        """Build an adapter from an ``openai-compat`` roster entry.

        Args:
            entry: A roster entry whose ``invoke.kind`` is ``openai-compat``.
            **overrides: Optional constructor overrides (``timeout``, ``default_max_tokens``).

        Returns:
            A configured :class:`OpenAICompatAdapter`.

        Raises:
            AdapterError: If the entry's invoke kind is not ``openai-compat``.
        """
        if entry.invoke.kind != "openai-compat":
            raise AdapterError(
                f"entry {entry.id!r} has invoke.kind {entry.invoke.kind!r}, "
                "not 'openai-compat'"
            )
        return cls(
            base_url=entry.invoke.base_url,  # validated non-None by the roster loader
            model=entry.invoke.model,
            key_ref=entry.invoke.key_ref,
            **overrides,  # type: ignore[arg-type]
        )

    def run(self, prompt: str, opts: Mapping[str, object] | None = None) -> str:
        """Send a single-message chat completion and return the final text.

        Args:
            prompt: The prompt to send as the sole user message.
            opts: Optional per-call options. Recognized keys: ``max_tokens`` (int).

        Returns:
            The model's final ``content`` (``reasoning_content`` is dropped).

        Raises:
            AdapterError: If ``max_tokens`` < 1, or on non-2xx status, transport failure, or
                unexpected response shape.
        """
        opts = opts or {}
        max_tokens = int(opts.get("max_tokens", self.default_max_tokens))
        if max_tokens < 1:
            raise AdapterError(
                f"max_tokens must be >= 1, got {max_tokens} "
                "(gpt-oss needs generous headroom — the C0 budget lesson)"
            )

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        key = resolve_key_ref(self.key_ref)
        if key:
            headers["Authorization"] = f"Bearer {key}"

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            raise AdapterError(
                f"LiteLLM returned {exc.response.status_code} for model {self.model!r}: {body}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"transport error calling {url} for model {self.model!r}: {exc}"
            ) from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError(f"unexpected response shape from LiteLLM: {data!r}") from exc

        if content is None:
            raise AdapterError(
                f"LiteLLM returned null content for model {self.model!r} "
                f"(often a truncated response — try a larger max_tokens): {data!r}"
            )
        return content
