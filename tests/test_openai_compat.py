"""Tests for the openai-compat adapter (tanglebrain/adapters/openai_compat.py).

All HTTP is mocked — these tests never touch the network. The adapter is exercised with real
``httpx.Response`` objects so status handling and JSON parsing match production behaviour.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from tanglebrain.adapters.openai_compat import (
    DEFAULT_MAX_TOKENS,
    AdapterError,
    OpenAICompatAdapter,
    resolve_key_ref,
)
from tanglebrain.roster import Invoke, RosterEntry

URL = "http://litellm.example:4000/v1"


def fake_client_returning(response: httpx.Response) -> MagicMock:
    """Build a MagicMock that mimics ``httpx.Client`` used as a context manager.

    The returned mock's ``post`` yields ``response`` (or raises if ``response`` is an
    exception set as ``side_effect`` by the caller afterwards).
    """
    fake = MagicMock()
    fake.__enter__.return_value = fake
    fake.__exit__.return_value = False
    fake.post.return_value = response
    return fake


def make_response(status: int, *, json_body=None, text="") -> httpx.Response:
    """Construct a real httpx.Response bound to a dummy request."""
    request = httpx.Request("POST", f"{URL}/chat/completions")
    if json_body is not None:
        return httpx.Response(status, request=request, json=json_body)
    return httpx.Response(status, request=request, text=text)


class ResolveKeyRefTest(unittest.TestCase):
    """key_ref resolution covers file / env / none / unknown forms."""

    def test_none_literal_and_python_none(self):
        self.assertIsNone(resolve_key_ref(None))
        self.assertIsNone(resolve_key_ref("none"))

    def test_file_ref_reads_and_strips(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".key", delete=False)
        handle.write("  sk-scoped-123\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(resolve_key_ref(f"file:{handle.name}"), "sk-scoped-123")

    def test_file_ref_expands_user(self):
        # ~ must be expanded, not treated literally.
        with patch.object(Path, "expanduser", return_value=Path("/no/such.key")):
            with self.assertRaises(AdapterError):
                resolve_key_ref("file:~/x.key")

    def test_file_ref_missing(self):
        with self.assertRaises(AdapterError):
            resolve_key_ref("file:/no/such/scoped.key")

    def test_file_ref_empty(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".key", delete=False)
        handle.write("   \n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(AdapterError):
            resolve_key_ref(f"file:{handle.name}")

    def test_env_ref(self):
        with patch.dict(os.environ, {"TB_KEY": "sk-env-9"}, clear=True):
            self.assertEqual(resolve_key_ref("env:TB_KEY"), "sk-env-9")

    def test_env_ref_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AdapterError):
                resolve_key_ref("env:TB_KEY")

    def test_unknown_form(self):
        with self.assertRaises(AdapterError):
            resolve_key_ref("vault:secret/x")


class RunTest(unittest.TestCase):
    """run() builds the right request and returns only the final content."""

    def _adapter(self, key_ref=None):
        return OpenAICompatAdapter(base_url=URL, model="gpt-oss-120b", key_ref=key_ref)

    def test_returns_content(self):
        resp = make_response(200, json_body={"choices": [{"message": {"content": "hi there"}}]})
        fake = fake_client_returning(resp)
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            self.assertEqual(self._adapter().run("hello"), "hi there")

    def test_drops_reasoning_content(self):
        # gpt-oss returns chain-of-thought in a separate field; we return only content.
        body = {"choices": [{"message": {"content": "final", "reasoning_content": "lots of CoT"}}]}
        fake = fake_client_returning(make_response(200, json_body=body))
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            self.assertEqual(self._adapter().run("q"), "final")

    def test_default_max_tokens_is_2048(self):
        fake = fake_client_returning(make_response(200, json_body={"choices": [{"message": {"content": "x"}}]}))
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            self._adapter().run("q")
        payload = fake.post.call_args.kwargs["json"]
        self.assertEqual(payload["max_tokens"], DEFAULT_MAX_TOKENS)
        self.assertEqual(DEFAULT_MAX_TOKENS, 2048)

    def test_max_tokens_override(self):
        fake = fake_client_returning(make_response(200, json_body={"choices": [{"message": {"content": "x"}}]}))
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            self._adapter().run("q", {"max_tokens": 512})
        self.assertEqual(fake.post.call_args.kwargs["json"]["max_tokens"], 512)

    def test_authorization_header_present_with_key(self):
        with patch("tanglebrain.adapters.openai_compat.resolve_key_ref", return_value="sk-abc"):
            fake = fake_client_returning(make_response(200, json_body={"choices": [{"message": {"content": "x"}}]}))
            with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
                self._adapter("file:whatever").run("q")
            self.assertEqual(fake.post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-abc")

    def test_authorization_header_absent_when_open(self):
        fake = fake_client_returning(make_response(200, json_body={"choices": [{"message": {"content": "x"}}]}))
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            self._adapter("none").run("q")
        self.assertNotIn("Authorization", fake.post.call_args.kwargs["headers"])

    def test_http_error_raises_adapter_error(self):
        fake = fake_client_returning(make_response(500, text="upstream boom"))
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            with self.assertRaises(AdapterError) as ctx:
                self._adapter("none").run("q")
        self.assertIn("500", str(ctx.exception))

    def test_transport_error_raises_adapter_error(self):
        fake = fake_client_returning(make_response(200, json_body={}))
        fake.post.side_effect = httpx.ConnectError("no route to host")
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            with self.assertRaises(AdapterError):
                self._adapter("none").run("q")

    def test_unexpected_shape_raises(self):
        fake = fake_client_returning(make_response(200, json_body={"unexpected": True}))
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            with self.assertRaises(AdapterError):
                self._adapter("none").run("q")

    def test_max_tokens_below_one_rejected(self):
        # The CLI passes --max-tokens straight through; 0/negative would truncate silently.
        for bad in (0, -1):
            with self.assertRaises(AdapterError):
                self._adapter("none").run("q", {"max_tokens": bad})

    def test_null_content_raises_with_budget_hint(self):
        fake = fake_client_returning(make_response(200, json_body={"choices": [{"message": {"content": None}}]}))
        with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
            with self.assertRaises(AdapterError) as ctx:
                self._adapter("none").run("q")
        self.assertIn("max_tokens", str(ctx.exception))


# Captured before any test patches httpx.Client, so stream-test factories can build a REAL
# client around a MockTransport without recursing into their own patch.
_RealClient = httpx.Client


def sse_bytes(*events: str) -> bytes:
    """Frame ``events`` as an SSE body (one ``data:`` line each, blank-line separated)."""
    return "".join(f"data: {event}\n\n" for event in events).encode("utf-8")


def delta_event(content: str | None = None, **delta_extra) -> str:
    """Build one ``chat.completion.chunk`` SSE event JSON with the given delta content."""
    delta: dict = dict(delta_extra)
    if content is not None:
        delta["content"] = content
    return json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})


class RunStreamTest(unittest.TestCase):
    """run_stream — SSE pass-through decoding, laziness, and error mapping (c13-S1)."""

    def _adapter(self) -> OpenAICompatAdapter:
        return OpenAICompatAdapter(base_url=URL, model="llama3.2", key_ref="none")

    def _patched_client(self, handler):
        """Patch ``httpx.Client`` so the adapter talks to ``handler`` via a real MockTransport."""

        def factory(**kwargs):
            return _RealClient(
                transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout")
            )

        return patch("tanglebrain.adapters.openai_compat.httpx.Client", new=factory)

    def test_streams_content_deltas_in_order(self):
        body = sse_bytes(
            delta_event(role="assistant"),  # role-only preamble — no content, skipped
            delta_event("Hel"),
            delta_event("lo"),
            json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
            "[DONE]",
        )
        with self._patched_client(lambda req: httpx.Response(200, content=body)):
            self.assertEqual(list(self._adapter().run_stream("q")), ["Hel", "lo"])

    def test_payload_carries_stream_true_and_max_tokens(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, content=sse_bytes(delta_event("x"), "[DONE]"))

        with self._patched_client(handler):
            list(self._adapter().run_stream("the prompt", {"max_tokens": 99}))
        self.assertIs(seen["stream"], True)
        self.assertEqual(seen["max_tokens"], 99)
        self.assertEqual(seen["messages"], [{"role": "user", "content": "the prompt"}])

    def test_clean_close_without_done_still_delivers(self):
        # Some local gateways omit [DONE]; honest EOF ends the stream without error.
        body = sse_bytes(delta_event("all"), delta_event(" of it"))
        with self._patched_client(lambda req: httpx.Response(200, content=body)):
            self.assertEqual(list(self._adapter().run_stream("q")), ["all", " of it"])

    def test_connection_opens_lazily_and_config_raises_eagerly(self):
        # Config errors raise at CALL time, with no HTTP client ever constructed…
        with patch("tanglebrain.adapters.openai_compat.httpx.Client") as client_cls:
            with self.assertRaises(AdapterError):
                self._adapter().run_stream("q", {"max_tokens": 0})
        client_cls.assert_not_called()
        # …and a valid call constructs no client until the first pull.
        with patch("tanglebrain.adapters.openai_compat.httpx.Client") as client_cls:
            self._adapter().run_stream("q")
        client_cls.assert_not_called()

    def test_non_2xx_raises_adapter_error_with_body_before_any_yield(self):
        handler = lambda req: httpx.Response(500, text="backend melted")  # noqa: E731
        with self._patched_client(handler):
            stream = self._adapter().run_stream("q")
            with self.assertRaises(AdapterError) as ctx:
                next(stream)
        self.assertIn("500", str(ctx.exception))
        self.assertIn("backend melted", str(ctx.exception))

    def test_malformed_data_line_raises(self):
        body = b"data: {not json}\n\n"
        with self._patched_client(lambda req: httpx.Response(200, content=body)):
            with self.assertRaises(AdapterError) as ctx:
                list(self._adapter().run_stream("q"))
        self.assertIn("malformed SSE", str(ctx.exception))

    def test_in_stream_error_event_raises(self):
        body = sse_bytes(delta_event("par"), json.dumps({"error": {"message": "quota exceeded"}}))
        with self._patched_client(lambda req: httpx.Response(200, content=body)):
            stream = self._adapter().run_stream("q")
            self.assertEqual(next(stream), "par")
            with self.assertRaises(AdapterError) as ctx:
                next(stream)
        self.assertIn("quota exceeded", str(ctx.exception))

    def test_usage_only_chunk_and_comment_lines_skipped(self):
        body = (
            b": keep-alive comment\n\n"
            + sse_bytes(
                delta_event("hi"),
                json.dumps({"choices": [], "usage": {"total_tokens": 5}}),
                "[DONE]",
            )
        )
        with self._patched_client(lambda req: httpx.Response(200, content=body)):
            self.assertEqual(list(self._adapter().run_stream("q")), ["hi"])

    def test_mid_stream_transport_error_maps_to_adapter_error(self):
        class ExplodingStream(httpx.SyncByteStream):
            def __iter__(self):
                yield sse_bytes(delta_event("par"))
                raise httpx.ReadError("connection reset")

        handler = lambda req: httpx.Response(200, stream=ExplodingStream())  # noqa: E731
        with self._patched_client(handler):
            stream = self._adapter().run_stream("q")
            self.assertEqual(next(stream), "par")
            with self.assertRaises(AdapterError) as ctx:
                next(stream)
        self.assertIn("transport error", str(ctx.exception))

    def test_events_after_done_are_ignored(self):
        body = sse_bytes(delta_event("a"), "[DONE]", delta_event("ghost"))
        with self._patched_client(lambda req: httpx.Response(200, content=body)):
            self.assertEqual(list(self._adapter().run_stream("q")), ["a"])


class FromEntryTest(unittest.TestCase):
    """from_entry() wires a roster entry into an adapter, rejecting the wrong kind."""

    def test_builds_from_openai_compat_entry(self):
        entry = RosterEntry(
            id="gpt-oss-120b",
            tier="local",
            invoke=Invoke(kind="openai-compat", base_url=URL, model="gpt-oss-120b", key_ref="none"),
        )
        adapter = OpenAICompatAdapter.from_entry(entry)
        self.assertEqual(adapter.model, "gpt-oss-120b")
        self.assertEqual(adapter.base_url, URL)

    def test_rejects_non_openai_compat_entry(self):
        entry = RosterEntry(id="claude", tier="sub", invoke=Invoke(kind="cli", cmd=["claude"]))
        with self.assertRaises(AdapterError):
            OpenAICompatAdapter.from_entry(entry)


if __name__ == "__main__":
    unittest.main()
