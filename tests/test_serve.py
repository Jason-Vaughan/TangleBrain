"""Tests for the OpenAI-compatible serve endpoint (tanglebrain/serve/).

All hermetic: routing is patched at the ``tanglebrain.serve.views.run_once`` seam, so no backend
is ever invoked. The dispatch tests exercise the HTTP layer socket-free (mirroring test_gui); one
loopback-socket test proves the real handler wiring end-to-end, including that the
``Authorization`` header is ignored.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from tanglebrain.adapters import AdapterError
from tanglebrain.roster import RosterError
from tanglebrain.router import RouterError
from tanglebrain.selector import SelectionError
from tanglebrain.serve.server import Handler, dispatch
from tanglebrain.serve.views import (
    AUTO_ALIAS,
    BadRequestError,
    completion_envelope,
    flatten_messages,
    handle_chat_completion,
    list_models,
    sse_body,
    wants_stream,
)

_SERVED = {"path": "router", "tier": "sub", "model": "claude", "task_id": "abc123"}


def _messages(*contents: str) -> list[dict]:
    """Build a single-user messages array (or role-alternating for multiple contents)."""
    roles = ["user", "assistant"]
    return [{"role": roles[i % 2], "content": c} for i, c in enumerate(contents)]


def _chat_payload(**overrides) -> dict:
    """A minimal valid chat-completions payload, with overrides merged in."""
    payload = {"model": "auto", "messages": _messages("hello")}
    payload.update(overrides)
    return payload


class FlattenMessagesTest(unittest.TestCase):
    def test_single_user_message(self):
        self.assertEqual(flatten_messages([{"role": "user", "content": "hi"}]), "[user]\nhi")

    def test_multi_turn_preserves_order_and_roles(self):
        messages = [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
        ]
        self.assertEqual(
            flatten_messages(messages),
            "[system]\nbe brief\n\n[user]\nhi\n\n[assistant]\nhello\n\n[user]\nagain",
        )

    def test_text_content_parts_are_concatenated(self):
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "part one "}, {"type": "text", "text": "part two"}],
            }
        ]
        self.assertEqual(flatten_messages(messages), "[user]\npart one part two")

    def test_non_text_part_is_rejected_not_dropped(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look:"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
        with self.assertRaisesRegex(BadRequestError, "image_url"):
            flatten_messages(messages)

    def test_rejects_non_list_empty_list_and_malformed_messages(self):
        for bad in (None, "hi", {}, []):
            with self.assertRaises(BadRequestError):
                flatten_messages(bad)
        with self.assertRaises(BadRequestError):
            flatten_messages(["not an object"])
        with self.assertRaises(BadRequestError):
            flatten_messages([{"content": "no role"}])
        with self.assertRaises(BadRequestError):
            flatten_messages([{"role": "user"}])  # content absent (null content unsupported)


class HandleChatCompletionTest(unittest.TestCase):
    def test_auto_routes_with_no_pinned_model(self):
        run = MagicMock(return_value=("routed text", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            status, body = handle_chat_completion(_chat_payload())
        self.assertEqual(status, 200)
        self.assertIsNone(run.call_args.kwargs["model"])
        self.assertEqual(body["choices"][0]["message"]["content"], "routed text")

    def test_absent_model_defaults_to_auto(self):
        run = MagicMock(return_value=("t", dict(_SERVED)))
        payload = _chat_payload()
        del payload["model"]
        with patch("tanglebrain.serve.views.run_once", run):
            status, body = handle_chat_completion(payload)
        self.assertEqual(status, 200)
        self.assertIsNone(run.call_args.kwargs["model"])
        self.assertEqual(body["tanglebrain"]["requested_model"], AUTO_ALIAS)

    def test_roster_id_is_pinned(self):
        run = MagicMock(return_value=("t", {**_SERVED, "path": "model"}))
        with patch("tanglebrain.serve.views.run_once", run):
            status, _ = handle_chat_completion(_chat_payload(model="claude"))
        self.assertEqual(status, 200)
        self.assertEqual(run.call_args.kwargs["model"], "claude")

    def test_unknown_pinned_model_is_404_model_not_found(self):
        run = MagicMock(side_effect=SelectionError("no roster entry with id 'nope'"))
        with patch("tanglebrain.serve.views.run_once", run):
            status, body = handle_chat_completion(_chat_payload(model="nope"))
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "model_not_found")

    def test_selection_error_on_auto_path_is_502_not_404(self):
        # e.g. gate-local with no local entry — an upstream problem, not a bad model id.
        run = MagicMock(side_effect=SelectionError("no local-tier entry"))
        with patch("tanglebrain.serve.views.run_once", run):
            status, body = handle_chat_completion(_chat_payload())
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["type"], "upstream_error")

    def test_router_and_adapter_failures_are_502_with_detail(self):
        for exc in (RouterError("all 3 candidate(s) failed: ..."), AdapterError("backend down")):
            run = MagicMock(side_effect=exc)
            with patch("tanglebrain.serve.views.run_once", run):
                status, body = handle_chat_completion(_chat_payload())
            self.assertEqual(status, 502)
            self.assertEqual(body["error"]["type"], "upstream_error")
            self.assertIn(str(exc), body["error"]["message"])

    def test_broken_roster_is_500(self):
        run = MagicMock(side_effect=RosterError("roster file not found"))
        with patch("tanglebrain.serve.views.run_once", run):
            status, body = handle_chat_completion(_chat_payload())
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["type"], "server_error")

    def test_max_tokens_passes_through_and_is_validated(self):
        run = MagicMock(return_value=("t", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            status, _ = handle_chat_completion(_chat_payload(max_tokens=512))
        self.assertEqual(status, 200)
        self.assertEqual(run.call_args.kwargs["max_tokens"], 512)
        for bad in (0, -1, "512", 1.5, True):
            status, body = handle_chat_completion(_chat_payload(max_tokens=bad))
            self.assertEqual(status, 400, f"max_tokens={bad!r}")
            self.assertEqual(body["error"]["type"], "invalid_request_error")

    def test_non_string_model_is_400(self):
        status, body = handle_chat_completion(_chat_payload(model=42))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_request_error")

    def test_bad_messages_is_400_and_never_routes(self):
        run = MagicMock()
        with patch("tanglebrain.serve.views.run_once", run):
            status, _ = handle_chat_completion({"model": "auto", "messages": []})
        self.assertEqual(status, 400)
        run.assert_not_called()

    def test_sampling_knobs_are_accepted_and_ignored(self):
        run = MagicMock(return_value=("t", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            status, _ = handle_chat_completion(
                _chat_payload(temperature=0.2, top_p=0.9, n=1, tools=[{"type": "function"}])
            )
        self.assertEqual(status, 200)


class EnvelopeTest(unittest.TestCase):
    def test_envelope_shape(self):
        body = completion_envelope("hi there", dict(_SERVED), "auto", "[user]\nhello")
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["id"], "chatcmpl-abc123")  # reuses the minted task id
        self.assertEqual(body["model"], "claude")  # the SERVED id, not the requested alias
        choice = body["choices"][0]
        self.assertEqual(choice["message"], {"role": "assistant", "content": "hi there"})
        self.assertEqual(choice["finish_reason"], "stop")
        usage = body["usage"]
        self.assertEqual(usage["total_tokens"], usage["prompt_tokens"] + usage["completion_tokens"])
        self.assertGreater(usage["completion_tokens"], 0)
        ext = body["tanglebrain"]
        self.assertEqual(ext["requested_model"], "auto")
        self.assertEqual(ext["path"], "router")
        self.assertEqual(ext["tier"], "sub")
        self.assertTrue(ext["tokens_estimated"])

    def test_envelope_tolerates_unknown_served(self):
        body = completion_envelope("t", None, "auto", "p")
        self.assertEqual(body["model"], "auto")  # falls back to the requested value
        self.assertTrue(body["id"].startswith("chatcmpl-"))
        self.assertIsNone(body["tanglebrain"]["path"])


class SseBodyTest(unittest.TestCase):
    def test_single_chunk_emulation_framing(self):
        envelope = completion_envelope("streamed text", dict(_SERVED), "auto", "p")
        events = sse_body(envelope).decode("utf-8").strip().split("\n\n")
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e.startswith("data: ") for e in events))
        content = json.loads(events[0][len("data: "):])
        self.assertEqual(content["object"], "chat.completion.chunk")
        self.assertEqual(content["id"], envelope["id"])
        self.assertEqual(
            content["choices"][0]["delta"], {"role": "assistant", "content": "streamed text"}
        )
        self.assertIsNone(content["choices"][0]["finish_reason"])
        finish = json.loads(events[1][len("data: "):])
        self.assertEqual(finish["choices"][0], {"index": 0, "delta": {}, "finish_reason": "stop"})
        self.assertEqual(events[2], "data: [DONE]")

    def test_wants_stream(self):
        self.assertTrue(wants_stream({"stream": True}))
        self.assertFalse(wants_stream({"stream": False}))
        self.assertFalse(wants_stream({}))


class ListModelsTest(unittest.TestCase):
    def test_auto_plus_every_roster_id(self):
        entry = MagicMock()
        entry.id = "local-ollama"
        roster = MagicMock()
        roster.entries = [entry]
        with patch("tanglebrain.serve.views.load_roster", return_value=roster):
            body = list_models()
        self.assertEqual(body["object"], "list")
        self.assertEqual([m["id"] for m in body["data"]], ["auto", "local-ollama"])
        self.assertTrue(all(m["owned_by"] == "tanglebrain" for m in body["data"]))


class DispatchTest(unittest.TestCase):
    def _post(self, path: str, payload: object) -> tuple[int, str, dict | str]:
        status, ctype, body = dispatch("POST", path, json.dumps(payload).encode("utf-8"))
        text = body.decode("utf-8")
        return status, ctype, (json.loads(text) if ctype.startswith("application/json") else text)

    def test_chat_completion_roundtrip(self):
        run = MagicMock(return_value=("pong", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            status, ctype, body = self._post("/v1/chat/completions", _chat_payload())
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(body["choices"][0]["message"]["content"], "pong")

    def test_stream_true_returns_sse(self):
        run = MagicMock(return_value=("pong", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            status, ctype, body = self._post("/v1/chat/completions", _chat_payload(stream=True))
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn("data: [DONE]", body)

    def test_stream_error_is_plain_json(self):
        run = MagicMock(side_effect=RouterError("all failed"))
        with patch("tanglebrain.serve.views.run_once", run):
            status, ctype, body = self._post("/v1/chat/completions", _chat_payload(stream=True))
        self.assertEqual(status, 502)
        self.assertIn("application/json", ctype)
        self.assertEqual(body["error"]["type"], "upstream_error")

    def test_invalid_json_and_non_object_bodies_are_400(self):
        status, _, body = dispatch("POST", "/v1/chat/completions", b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("valid JSON", json.loads(body)["error"]["message"])
        status, _, body = self._post("/v1/chat/completions", ["a", "list"])
        self.assertEqual(status, 400)

    def test_models_endpoint(self):
        entry = MagicMock()
        entry.id = "local-ollama"
        roster = MagicMock()
        roster.entries = [entry]
        with patch("tanglebrain.serve.views.load_roster", return_value=roster):
            status, ctype, body = dispatch("GET", "/v1/models?x=1")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in json.loads(body)["data"]], ["auto", "local-ollama"])

    def test_models_roster_failure_is_clean_json_500(self):
        with patch("tanglebrain.serve.views.load_roster", side_effect=RosterError("bad yaml")):
            status, _, body = dispatch("GET", "/v1/models")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["type"], "server_error")

    def test_unknown_paths_and_methods(self):
        self.assertEqual(dispatch("GET", "/")[0], 404)
        self.assertEqual(dispatch("POST", "/v1/embeddings")[0], 404)
        self.assertEqual(dispatch("DELETE", "/v1/models")[0], 405)


class LiveHandlerTest(unittest.TestCase):
    """One loopback-socket test proving the real Handler wiring, auth-ignorance included."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_request_with_garbage_authorization_succeeds(self):
        run = MagicMock(return_value=("pong", dict(_SERVED)))
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(_chat_payload()).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # Local callers need no key — absent, empty, or garbage must all be ignored.
                "Authorization": "Bearer definitely-not-a-real-key",
            },
            method="POST",
        )
        with patch("tanglebrain.serve.views.run_once", run):
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                body = json.loads(response.read().decode("utf-8"))
        self.assertEqual(body["choices"][0]["message"]["content"], "pong")
        self.assertEqual(body["model"], "claude")


if __name__ == "__main__":
    unittest.main()
