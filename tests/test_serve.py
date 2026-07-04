"""Tests for the OpenAI-compatible serve endpoint (tanglebrain/serve/).

All hermetic: routing is patched at the ``tanglebrain.serve.views.run_once`` seam, so no backend
is ever invoked. The dispatch tests exercise the HTTP layer socket-free (mirroring test_gui); one
loopback-socket test proves the real handler wiring end-to-end, including that the
``Authorization`` header is ignored.
"""
from __future__ import annotations

import http.client
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
    PARENT_TASK_HEADER,
    BadRequestError,
    completion_envelope,
    flatten_messages,
    handle_chat_completion,
    handle_chat_completion_stream,
    list_models,
    sanitize_parent_task,
    sse_stream_events,
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

    def test_present_but_falsy_or_non_string_model_is_400_never_routes(self):
        # Only an ABSENT model defaults to auto — "", null, 0, false are broken client configs
        # and must fail loudly rather than silently engage the router (Critic S1).
        run = MagicMock()
        for bad in (42, "", None, 0, False):
            with patch("tanglebrain.serve.views.run_once", run):
                status, body = handle_chat_completion(_chat_payload(model=bad))
            self.assertEqual(status, 400, f"model={bad!r}")
            self.assertEqual(body["error"]["type"], "invalid_request_error")
        run.assert_not_called()

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


class _CloseTracking:
    """A delta-iterator stand-in that records whether close() was called."""

    def __init__(self, inner, closed: list):
        self._inner = inner
        self._closed = closed

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._inner)

    def close(self):
        self._closed.append(True)


def _decode_events(chunks: list[bytes]) -> list[dict | str]:
    """Decode framed SSE event byte strings into parsed JSON dicts (or the ``[DONE]`` literal)."""
    events = []
    for chunk in chunks:
        text = chunk.decode("utf-8")
        assert text.startswith("data: ") and text.endswith("\n\n"), text
        data = text[len("data: "):-2]
        events.append("[DONE]" if data == "[DONE]" else json.loads(data))
    return events


class SseStreamEventsTest(unittest.TestCase):
    """sse_stream_events — incremental chunk framing, finish/usage, mid-stream errors (c13-S2)."""

    def test_incremental_framing_and_finish_chunk(self):
        events = _decode_events(
            list(sse_stream_events("Hel", iter(["lo", "!"]), dict(_SERVED), "auto", "[user]\nhi"))
        )
        self.assertEqual(len(events), 5)  # first + 2 deltas + finish + [DONE]
        first, second, third, finish, done = events
        self.assertEqual(first["object"], "chat.completion.chunk")
        self.assertEqual(first["id"], "chatcmpl-abc123")  # reuses the minted task id
        self.assertEqual(first["model"], "claude")  # the SERVED id, not the requested alias
        self.assertEqual(first["choices"][0]["delta"], {"role": "assistant", "content": "Hel"})
        self.assertIsNone(first["choices"][0]["finish_reason"])
        self.assertEqual(second["choices"][0]["delta"], {"content": "lo"})
        self.assertEqual(third["choices"][0]["delta"], {"content": "!"})
        self.assertEqual(finish["choices"][0], {"index": 0, "delta": {}, "finish_reason": "stop"})
        usage = finish["usage"]  # always present, flagged estimated (ratified D6)
        self.assertEqual(usage["total_tokens"], usage["prompt_tokens"] + usage["completion_tokens"])
        self.assertGreater(usage["completion_tokens"], 0)
        ext = finish["tanglebrain"]
        self.assertEqual(ext["requested_model"], "auto")
        self.assertEqual(ext["path"], "router")
        self.assertTrue(ext["tokens_estimated"])
        self.assertEqual(done, "[DONE]")

    def test_single_delta_stream_matches_v1_emulation_shape(self):
        # An emulated path (router / cli-kind) delivers one content chunk + finish + [DONE] —
        # the same client-visible contract v1's sse_body produced.
        events = _decode_events(
            list(sse_stream_events("whole text", iter([]), dict(_SERVED), "auto", "p"))
        )
        self.assertEqual(len(events), 3)
        self.assertEqual(
            events[0]["choices"][0]["delta"], {"role": "assistant", "content": "whole text"}
        )
        self.assertEqual(events[1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(events[2], "[DONE]")

    def test_mid_stream_error_yields_error_event_and_no_done(self):
        def dying():
            yield "par"
            raise AdapterError("backend died mid-answer")

        events = _decode_events(
            list(sse_stream_events("first", dying(), dict(_SERVED), "auto", "p"))
        )
        self.assertEqual(len(events), 3)  # first + "par" + error; NO finish, NO [DONE]
        self.assertEqual(events[1]["choices"][0]["delta"], {"content": "par"})
        error = events[2]["error"]
        self.assertEqual(error["type"], "upstream_error")
        self.assertIn("backend died mid-answer", error["message"])
        self.assertNotIn("[DONE]", events)

    def test_unknown_served_falls_back_to_requested_model(self):
        events = _decode_events(list(sse_stream_events("x", iter([]), None, "auto", "p")))
        self.assertEqual(events[0]["model"], "auto")
        self.assertTrue(events[0]["id"].startswith("chatcmpl-"))

    def test_non_adapter_escape_still_frames_an_error_event(self):
        # A nonconforming backend / internal bug must NOT silently close the stream (the exact
        # alternative D5 rejected) — it frames as server_error, still no [DONE] (Critic S2).
        def buggy():
            yield "ok"
            raise ValueError("nonconforming adapter leaked a raw error")

        events = _decode_events(
            list(sse_stream_events("first", buggy(), dict(_SERVED), "auto", "p"))
        )
        error = events[-1]["error"]
        self.assertEqual(error["type"], "server_error")
        self.assertIn("nonconforming adapter", error["message"])
        self.assertNotIn("[DONE]", events)

    def test_rest_iterator_is_closed_however_the_stream_ends(self):
        # Metering-on-abandonment rides on the delta iterator's close — it must fire
        # deterministically, not on GC timing, for all three endings (Critic S2).
        def endings():
            yield "exhausted", iter(["a"])

            def dying():
                yield "b"
                raise AdapterError("died")
            yield "error", dying()
            yield "abandoned", iter(["c", "never-pulled"])

        for label, rest in endings():
            closed = []
            wrapped = _CloseTracking(rest, closed)
            stream = sse_stream_events("first", wrapped, dict(_SERVED), "auto", "p")
            if label == "abandoned":
                next(stream)
                stream.close()  # client walked away after the first event
            else:
                list(stream)
            self.assertEqual(closed, [True], f"rest not closed on {label!r} ending")


class HandleChatCompletionStreamTest(unittest.TestCase):
    """handle_chat_completion_stream — prime-the-pump status mapping (c13-S2)."""

    def test_success_returns_200_and_event_iterator(self):
        stream = MagicMock(return_value=(iter(["a", "b"]), dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, body = handle_chat_completion_stream(
                _chat_payload(stream=True, max_tokens=512)
            )
        self.assertEqual(status, 200)
        events = _decode_events(list(body))
        self.assertEqual(events[0]["choices"][0]["delta"], {"role": "assistant", "content": "a"})
        self.assertEqual(events[-1], "[DONE]")
        self.assertIsNone(stream.call_args.kwargs["model"])  # auto → no pin
        self.assertEqual(stream.call_args.kwargs["max_tokens"], 512)  # threaded through

    def test_connect_time_failure_is_plain_502_never_sse(self):
        # The first pull raises (lazy connect failed) — must map to (502, json), no iterator.
        def dead_stream(*a, **k):
            def gen():
                raise AdapterError("connection refused")
                yield  # pragma: no cover
            return gen(), dict(_SERVED)

        with patch("tanglebrain.serve.views.run_once_stream", dead_stream):
            status, body = handle_chat_completion_stream(_chat_payload(stream=True))
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["type"], "upstream_error")
        self.assertIn("connection refused", body["error"]["message"])

    def test_unknown_pinned_model_is_404(self):
        stream = MagicMock(side_effect=SelectionError("no roster entry with id 'nope'"))
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, body = handle_chat_completion_stream(
                _chat_payload(model="nope", stream=True)
            )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "model_not_found")

    def test_selection_error_on_auto_path_is_502(self):
        stream = MagicMock(side_effect=SelectionError("no local-tier entry"))
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, body = handle_chat_completion_stream(_chat_payload(stream=True))
        self.assertEqual(status, 502)

    def test_router_failure_and_broken_roster_map_like_plain_handler(self):
        for exc, expected in ((RouterError("all failed"), 502), (RosterError("bad yaml"), 500)):
            stream = MagicMock(side_effect=exc)
            with patch("tanglebrain.serve.views.run_once_stream", stream):
                status, _ = handle_chat_completion_stream(_chat_payload(stream=True))
            self.assertEqual(status, expected, str(exc))

    def test_bad_request_is_400_and_never_routes(self):
        stream = MagicMock()
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, body = handle_chat_completion_stream(
                {"model": "", "messages": _messages("hi"), "stream": True}
            )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        stream.assert_not_called()

    def test_empty_stream_is_defensive_502(self):
        stream = MagicMock(return_value=(iter([]), dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, body = handle_chat_completion_stream(_chat_payload(stream=True))
        self.assertEqual(status, 502)
        self.assertIn("before any content", body["error"]["message"])


class ParentTaskAttributionTest(unittest.TestCase):
    """#74: origin + X-TangleBrain-Parent-Task threading from transport to run_once*."""

    def test_sanitize_parent_task(self):
        self.assertIsNone(sanitize_parent_task(None))
        self.assertIsNone(sanitize_parent_task(""))
        self.assertIsNone(sanitize_parent_task("   "))
        self.assertIsNone(sanitize_parent_task(42))
        self.assertEqual(sanitize_parent_task("  tc-42  "), "tc-42")
        self.assertEqual(len(sanitize_parent_task("x" * 5000)), 128)  # length-capped
        # Control chars are dropped, not recorded: header folding can smuggle \r\n through the
        # stdlib parser, and ANSI escapes would bite any consumer printing the field raw.
        self.assertEqual(sanitize_parent_task("a\r\nb"), "ab")
        self.assertEqual(sanitize_parent_task("\x1b[31mevil\x1b[0m"), "[31mevil[0m")
        self.assertIsNone(sanitize_parent_task("\x1b\x00\x07"))  # nothing printable left

    def test_plain_handler_threads_origin_and_parent_task(self):
        run = MagicMock(return_value=("t", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            status, _ = handle_chat_completion(_chat_payload(), "tc-session-42")
        self.assertEqual(status, 200)
        self.assertEqual(run.call_args.kwargs["origin"], "serve")
        self.assertEqual(run.call_args.kwargs["parent_task_id"], "tc-session-42")

    def test_plain_handler_defaults_parent_task_to_none(self):
        run = MagicMock(return_value=("t", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            handle_chat_completion(_chat_payload())
        self.assertEqual(run.call_args.kwargs["origin"], "serve")
        self.assertIsNone(run.call_args.kwargs["parent_task_id"])

    def test_stream_handler_threads_origin_and_parent_task(self):
        stream = MagicMock(return_value=(iter(["a"]), dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, body = handle_chat_completion_stream(
                _chat_payload(stream=True), "tc-session-42"
            )
            list(body)
        self.assertEqual(status, 200)
        self.assertEqual(stream.call_args.kwargs["origin"], "serve")
        self.assertEqual(stream.call_args.kwargs["parent_task_id"], "tc-session-42")

    def test_dispatch_sanitizes_the_raw_header_value(self):
        run = MagicMock(return_value=("t", dict(_SERVED)))
        payload = json.dumps(_chat_payload()).encode("utf-8")
        with patch("tanglebrain.serve.views.run_once", run):
            dispatch("POST", "/v1/chat/completions", payload, parent_task="  tc-42  ")
            dispatch("POST", "/v1/chat/completions", payload, parent_task="   ")
        first, second = run.call_args_list
        self.assertEqual(first.kwargs["parent_task_id"], "tc-42")
        self.assertIsNone(second.kwargs["parent_task_id"])


class WantsStreamTest(unittest.TestCase):
    def test_wants_stream_only_on_json_true(self):
        self.assertTrue(wants_stream({"stream": True}))
        self.assertFalse(wants_stream({"stream": False}))
        self.assertFalse(wants_stream({}))
        # Non-bool values degrade to a plain JSON envelope the client can still read.
        for sloppy in ("false", "true", 1, [], {}):
            self.assertFalse(wants_stream({"stream": sloppy}), f"stream={sloppy!r}")


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

    def test_stream_true_returns_sse_event_iterator(self):
        stream = MagicMock(return_value=(iter(["po", "ng"]), dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, ctype, body = dispatch(
                "POST", "/v1/chat/completions",
                json.dumps(_chat_payload(stream=True)).encode("utf-8"),
            )
            text = b"".join(body).decode("utf-8")  # body is an Iterator[bytes], not bytes
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", ctype)
        self.assertIn('"content": "po"', text)
        self.assertIn('"content": "ng"', text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"))

    def test_stream_error_is_plain_json(self):
        stream = MagicMock(side_effect=RouterError("all failed"))
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, ctype, body = self._post("/v1/chat/completions", _chat_payload(stream=True))
        self.assertEqual(status, 502)
        self.assertIn("application/json", ctype)
        self.assertEqual(body["error"]["type"], "upstream_error")

    def test_stream_unexpected_escape_is_clean_json_500(self):
        # The pump primes inside dispatch's guard — a raw escape from the backend connection
        # must come back as JSON, never a committed-then-broken SSE stream.
        stream = MagicMock(side_effect=RuntimeError("settings file is malformed"))
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, ctype, body = self._post("/v1/chat/completions", _chat_payload(stream=True))
        self.assertEqual(status, 500)
        self.assertIn("application/json", ctype)
        self.assertEqual(body["error"]["type"], "server_error")

    def test_stream_non_json_content_type_still_rejected(self):
        stream = MagicMock()
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            status, _, _ = dispatch(
                "POST", "/v1/chat/completions",
                json.dumps(_chat_payload(stream=True)).encode("utf-8"),
                content_type="text/plain",
            )
        self.assertEqual(status, 415)
        stream.assert_not_called()

    def test_invalid_json_and_non_object_bodies_are_400(self):
        status, _, body = dispatch("POST", "/v1/chat/completions", b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("valid JSON", json.loads(body)["error"]["message"])
        status, _, body = self._post("/v1/chat/completions", ["a", "list"])
        self.assertEqual(status, 400)

    def test_non_json_content_type_is_rejected_before_routing(self):
        # A cross-origin browser fetch can POST text/plain to localhost with no CORS preflight —
        # this unauthenticated surface spends real quota, so non-JSON must never reach routing
        # (Critic S2).
        run = MagicMock()
        payload = json.dumps(_chat_payload()).encode("utf-8")
        with patch("tanglebrain.serve.views.run_once", run):
            status, ctype, body = dispatch(
                "POST", "/v1/chat/completions", payload, content_type="text/plain;charset=UTF-8"
            )
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"]["message"])
        run.assert_not_called()
        # Charset-qualified JSON is fine.
        run = MagicMock(return_value=("t", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            status, _, _ = dispatch(
                "POST", "/v1/chat/completions", payload, content_type="application/json; charset=utf-8"
            )
        self.assertEqual(status, 200)

    def test_unexpected_exception_is_clean_json_500_not_a_dropped_connection(self):
        # e.g. a malformed settings.yaml raises SettingsError on the auto path — any escape must
        # come back as a JSON error body, never a traceback + connection reset (Critic B1).
        run = MagicMock(side_effect=RuntimeError("settings file is malformed"))
        with patch("tanglebrain.serve.views.run_once", run):
            status, ctype, body = self._post("/v1/chat/completions", _chat_payload())
        self.assertEqual(status, 500)
        self.assertIn("application/json", ctype)
        error = body["error"]
        self.assertEqual(error["type"], "server_error")
        self.assertIn("settings file is malformed", error["message"])

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

    def test_parent_task_header_reaches_routing_over_the_socket(self):
        # #74 end-to-end: the real Handler reads X-TangleBrain-Parent-Task off the wire.
        run = MagicMock(return_value=("pong", dict(_SERVED)))
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(_chat_payload()).encode("utf-8"),
            headers={"Content-Type": "application/json", PARENT_TASK_HEADER: "tc-session-42"},
            method="POST",
        )
        with patch("tanglebrain.serve.views.run_once", run):
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(run.call_args.kwargs["parent_task_id"], "tc-session-42")
        self.assertEqual(run.call_args.kwargs["origin"], "serve")

    def test_streaming_deltas_arrive_incrementally_over_the_socket(self):
        # The point of c13: the first content chunk must be readable while the backend is still
        # generating. The second delta is gated on an Event the test only sets AFTER it has read
        # the first chunk off the socket — if the handler buffered the whole body, this would
        # deadlock (and time out) instead of passing.
        gate = threading.Event()

        def deltas():
            yield "early token"
            if not gate.wait(5):  # pragma: no cover — only on test failure
                raise AssertionError("first chunk was never read off the socket")
            yield "late token"

        stream = MagicMock(return_value=(deltas(), dict(_SERVED)))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            connection.request(
                "POST", "/v1/chat/completions",
                json.dumps(_chat_payload(stream=True)),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            # Streamed framing: length unknown up front, connection close delimits the body.
            self.assertIsNone(response.getheader("Content-Length"))
            self.assertEqual(response.getheader("Connection"), "close")
            first_line = response.fp.readline().decode("utf-8")
            self.assertIn("early token", first_line)  # read while the backend is still gated
            gate.set()
            remainder = response.read().decode("utf-8")
        self.assertIn("late token", remainder)
        self.assertIn('"finish_reason": "stop"', remainder)
        self.assertIn("data: [DONE]", remainder)

    def test_client_disconnect_finalizes_the_stream_and_server_survives(self):
        # Quota-accounting integrity: when the client walks away mid-stream, the delta iterator
        # must still be finalized (S1's metering-on-abandonment fires from its close), and the
        # server must survive to take the next request (Critic S2).
        finalized = threading.Event()
        release = threading.Event()

        def deltas():
            try:
                yield "first"
                release.wait(5)
                yield "second"
                yield "third"
            finally:
                finalized.set()

        stream = MagicMock(return_value=(deltas(), dict(_SERVED)))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        with patch("tanglebrain.serve.views.run_once_stream", stream):
            connection.request(
                "POST", "/v1/chat/completions",
                json.dumps(_chat_payload(stream=True)),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertIn("first", response.fp.readline().decode("utf-8"))
            connection.close()  # client gives up mid-stream
            release.set()
            self.assertTrue(
                finalized.wait(5), "delta iterator was not finalized after client disconnect"
            )
        # The handler thread absorbed the disconnect; the server still serves.
        run = MagicMock(return_value=("still alive", dict(_SERVED)))
        with patch("tanglebrain.serve.views.run_once", run):
            follow_up = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/chat/completions",
                data=json.dumps(_chat_payload()).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(follow_up, timeout=5) as after:
                self.assertEqual(after.status, 200)

    def test_malformed_content_length_is_400_not_a_reset(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "abc")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("Content-Length", json.loads(response.read())["error"]["message"])


if __name__ == "__main__":
    unittest.main()
