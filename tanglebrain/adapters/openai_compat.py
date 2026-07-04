"""OpenAI-compat adapter — the free local tier.

Calls an OpenAI-compatible ``/chat/completions`` endpoint (e.g. Ollama, or any local/self-hosted
gateway) and returns the final text. It calls the endpoint **directly** — no MCP server in between.

Behaviour:

- Returns only ``choices[0].message.content`` — some local reasoning models put chain-of-thought in
  a separate ``reasoning_content`` field, which is intentionally dropped.
- Defaults ``max_tokens`` to 2048: reasoning models spend part of their budget on internal reasoning
  before emitting the final answer, so a stingy cap can truncate real output.
- Raises on any non-2xx status, transport failure, or unexpected response shape. This layer
  does NOT retry or fall back — failures surface to the routing layer, which decides.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Mapping

import httpx

from tanglebrain.adapters.base import AdapterError
from tanglebrain.roster import RosterEntry

# Re-exported for backwards-compatible imports; the canonical definition lives in
# ``tanglebrain.adapters.base`` so the CLI adapter and the routing layer share one error type.
__all__ = ["AdapterError", "OpenAICompatAdapter", "resolve_key_ref"]

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_TOKENS = 2048


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
            base_url: OpenAI-compat base URL (e.g. ``http://localhost:11434/v1``).
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
        url, headers, max_tokens = self._prepare_request(opts)

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

    def _prepare_request(self, opts: Mapping[str, object]) -> tuple[str, dict, int]:
        """Resolve the URL, headers (credential included), and token cap for one call.

        Shared by :meth:`run` and :meth:`run_stream` so config/credential failures behave
        identically on both paths.

        Args:
            opts: Per-call options (``max_tokens`` recognized).

        Returns:
            ``(url, headers, max_tokens)``.

        Raises:
            AdapterError: If ``max_tokens`` < 1 or the credential reference cannot resolve.
        """
        max_tokens = int(opts.get("max_tokens", self.default_max_tokens))
        if max_tokens < 1:
            raise AdapterError(
                f"max_tokens must be >= 1, got {max_tokens} "
                "(a local reasoning model needs generous headroom)"
            )
        headers = {"Content-Type": "application/json"}
        key = resolve_key_ref(self.key_ref)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return f"{self.base_url}/chat/completions", headers, max_tokens

    def run_stream(self, prompt: str, opts: Mapping[str, object] | None = None) -> Iterator[str]:
        """Stream a single-message chat completion, yielding content deltas as they arrive.

        Implements the optional :class:`~tanglebrain.adapters.base.StreamingAdapter` capability:
        the same request as :meth:`run` with ``"stream": true``, decoded as SSE pass-through.
        Config/credential errors raise **eagerly** (at call time); the HTTP connection opens
        lazily on the first iteration, so a caller can pull the first delta before committing
        its own response headers (connect-time failures surface from that pull, pre-stream).

        Decoding stance (mirrors :meth:`run` where they overlap):

        - Yields ``choices[0].delta.content`` fragments; empty/role-only deltas and chunks with
          no choices (e.g. a trailing usage chunk) are skipped, never yielded.
        - ``reasoning_content`` deltas are dropped, matching ``run``.
        - Each ``data:`` line is decoded as one standalone JSON event. Spec-legal multi-line
          ``data:`` events are NOT reassembled — every real OpenAI-compat backend emits
          one-line events, and an exotic one fails loudly (``AdapterError``), never corrupts.
        - ``data: [DONE]`` ends the stream; a clean close **without** ``[DONE]`` also ends it
          (some local gateways omit the terminator — treat honest EOF as done, not an error).
        - A stream that ends cleanly having produced **no content at all** raises
          :class:`AdapterError`, mirroring ``run``'s null-content stance — a dead backend that
          200s with an empty stream must be a loud error, not a silent empty success.
        - An in-stream ``{"error": ...}`` event, a malformed ``data:`` line, a shape-broken
          event, a non-2xx status, or a transport failure raises :class:`AdapterError`.

        Args:
            prompt: The prompt to send as the sole user message.
            opts: Optional per-call options. Recognized keys: ``max_tokens`` (int).

        Yields:
            Non-empty content fragments, in generation order.

        Raises:
            AdapterError: Eagerly for bad config/credentials; from the first pull for
                connect-time failures; mid-iteration for a stream that dies part-way.
        """
        opts = opts or {}
        url, headers, max_tokens = self._prepare_request(opts)
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": True,
        }
        return self._stream_deltas(url, headers, payload)

    def _stream_deltas(self, url: str, headers: dict, payload: dict) -> Iterator[str]:
        """Open the SSE request and yield content deltas (the lazy half of :meth:`run_stream`).

        Args:
            url: The chat-completions URL.
            headers: Request headers (credential already resolved).
            payload: The JSON request body (``stream: true`` already set).

        Yields:
            Non-empty content fragments.

        Raises:
            AdapterError: On non-2xx status, transport failure, a malformed SSE data line, a
                shape-broken event, an in-stream error event, or a clean end with no content.
        """
        produced = False
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        # Read the body before .text — on a stream it is not buffered yet.
                        body = response.read().decode("utf-8", errors="replace")
                        raise AdapterError(
                            f"LiteLLM returned {response.status_code} for model "
                            f"{self.model!r}: {body}"
                        )
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue  # SSE comments / event: lines / keep-alive blanks
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except ValueError as exc:
                            raise AdapterError(
                                f"malformed SSE data line from model {self.model!r}: {data!r}"
                            ) from exc
                        if not isinstance(event, dict):
                            raise AdapterError(
                                f"unexpected SSE event shape from model {self.model!r}: {event!r}"
                            )
                        if "error" in event:
                            raise AdapterError(
                                f"in-stream error from model {self.model!r}: {event['error']!r}"
                            )
                        try:
                            choices = event.get("choices") or []
                            if not choices:
                                continue  # e.g. a trailing usage-only chunk
                            delta = choices[0].get("delta") or {}
                            content = delta.get("content")
                        except (AttributeError, TypeError, KeyError, IndexError) as exc:
                            # e.g. {"choices": [null]} — spec-valid JSON, broken shape. Must map
                            # to AdapterError like every other decode failure (S2's mid-stream
                            # error framing catches AdapterError, not raw AttributeError).
                            raise AdapterError(
                                f"unexpected SSE event shape from model {self.model!r}: {event!r}"
                            ) from exc
                        if content:
                            produced = True
                            yield content
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"transport error streaming {url} for model {self.model!r}: {exc}"
            ) from exc
        if not produced:
            raise AdapterError(
                f"stream from model {self.model!r} ended with no content "
                "(often a truncated response — try a larger max_tokens)"
            )
