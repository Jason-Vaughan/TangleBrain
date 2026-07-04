"""Serve HTTP server — stdlib :mod:`http.server`, localhost-only, zero new deps.

The handler is a thin shell over :func:`dispatch`, a pure ``(method, path, body) -> (status,
content_type, body)`` function holding all routing so it can be tested without a socket
(mirroring :mod:`tanglebrain.gui.server`).

Launched via the ``tanglebrain-serve`` console script. Binds ``127.0.0.1`` only — not
configurable: the endpoint is unauthenticated by design (local callers need no key; the
``Authorization`` header is never read), and a request spends real backend quota — real money
when the paid-API gates are on — so it must never be network-exposed. The roster is resolved the
same way as the CLI (``TANGLEBRAIN_ROSTER`` env → user config → packaged example).
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tanglebrain.serve.views import (
    DEFAULT_PORT,
    error_envelope,
    handle_chat_completion,
    list_models,
    sse_body,
    wants_stream,
)

_JSON = "application/json; charset=utf-8"
_SSE = "text/event-stream; charset=utf-8"


def _json_response(status: int, obj: object) -> tuple[int, str, bytes]:
    """Serialize ``obj`` as a JSON HTTP response triple."""
    return status, _JSON, json.dumps(obj).encode("utf-8")


def dispatch(
    method: str, path: str, body: bytes = b"", content_type: str = "application/json"
) -> tuple[int, str, bytes]:
    """Route one request to a view and return ``(status, content_type, body)``.

    Pure apart from what the views themselves do, so tests call it directly with no socket. The
    query string, if any, is ignored. A ``stream: true`` completion request gets its successful
    envelope framed as the single-chunk SSE emulation; errors are always plain JSON (matching
    OpenAI, which rejects a bad streaming request with a JSON error before any SSE starts).

    POST requires ``Content-Type: application/json``. Besides being what every OpenAI client
    sends, this closes the browser "simple request" hole: a cross-origin ``fetch`` from a
    malicious page can POST ``text/plain`` to localhost without a CORS preflight, and this is an
    unauthenticated surface that spends real quota — a non-JSON content type must never reach
    routing.

    Args:
        method: HTTP method (``GET``/``POST``).
        path: Request path (may include a ``?query``).
        body: Raw request body bytes (for ``POST``).
        content_type: The request's ``Content-Type`` header value (POST only; defaults to JSON
            so socket-free tests needn't supply it).

    Returns:
        ``(status_code, content_type, body_bytes)``.
    """
    path = path.split("?", 1)[0]

    if method == "GET":
        if path == "/v1/models":
            try:
                return _json_response(200, list_models())
            except Exception as exc:  # a broken roster must yield clean JSON, not a traceback
                return _json_response(500, error_envelope(str(exc), "server_error"))
        return _json_response(404, error_envelope(f"unknown path: {path}", "invalid_request_error"))

    if method == "POST":
        if path == "/v1/chat/completions":
            if not (content_type or "").lower().strip().startswith("application/json"):
                return _json_response(
                    415,
                    error_envelope(
                        "Content-Type must be application/json", "invalid_request_error"
                    ),
                )
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                return _json_response(
                    400, error_envelope("request body is not valid JSON", "invalid_request_error")
                )
            if not isinstance(payload, dict):
                return _json_response(
                    400, error_envelope("request body must be a JSON object", "invalid_request_error")
                )
            try:
                status, obj = handle_chat_completion(payload)
            except Exception as exc:  # noqa: BLE001 — any escape must be clean JSON, never a
                # dropped connection (e.g. a malformed settings.yaml raising SettingsError on the
                # auto path). Typed, expected failures are already mapped inside the handler.
                return _json_response(500, error_envelope(str(exc), "server_error"))
            if status == 200 and wants_stream(payload):
                return 200, _SSE, sse_body(obj)
            return _json_response(status, obj)
        return _json_response(404, error_envelope(f"unknown path: {path}", "invalid_request_error"))

    return _json_response(405, error_envelope(f"method not allowed: {method}", "invalid_request_error"))


class Handler(BaseHTTPRequestHandler):
    """Thin HTTP handler delegating all routing to :func:`dispatch`.

    ``Authorization`` is deliberately never consulted: local callers need no key, and any dummy
    bearer a client insists on sending is simply ignored. The only headers read are the framing
    ones — ``Content-Length`` and ``Content-Type`` (see :func:`dispatch` for why the latter is
    enforced).
    """

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        """Handle a GET by dispatching and writing the response."""
        self._respond(*dispatch("GET", self.path))

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        """Handle a POST by reading the body, dispatching, and writing the response."""
        try:
            length = max(0, int(self.headers.get("Content-Length", 0) or 0))
        except ValueError:
            self._respond(
                *_json_response(
                    400, error_envelope("invalid Content-Length header", "invalid_request_error")
                )
            )
            return
        body = self.rfile.read(length) if length else b""
        self._respond(*dispatch("POST", self.path, body, self.headers.get("Content-Type", "")))

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        """Write a complete HTTP response."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the default per-request stderr logging."""


def main(argv: list[str] | None = None) -> int:
    """Console entry point: serve the OpenAI-compatible endpoint until interrupted.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (``0``).
    """
    parser = argparse.ArgumentParser(
        prog="tanglebrain-serve",
        description=(
            "Serve TangleBrain's router as a local OpenAI-compatible endpoint "
            "(POST /v1/chat/completions; model 'auto' = full router, a roster id = explicit pin)."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to bind (default {DEFAULT_PORT}).",
    )
    args = parser.parse_args(argv)

    # Loopback only, not configurable: the endpoint is unauthenticated and spends real backend
    # quota (real money when the paid gates are on), so it must never be reachable off the machine.
    host = "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"TangleBrain serve: http://{host}:{args.port}/v1  (model 'auto' routes; Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
