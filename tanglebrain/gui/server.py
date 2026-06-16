"""Knob GUI HTTP server (C5a) — stdlib :mod:`http.server`, localhost-only, zero new deps.

The handler is a thin shell over :func:`dispatch`, a pure ``(method, path, body) -> (status,
content_type, body)`` function holding all routing so it can be tested without a socket. The page
itself is the packaged single-file ``static/index.html``.

Launched via the ``tanglebrain-gui`` console script. Binds ``127.0.0.1`` only — not configurable:
the panel runs prompts (spending real sub rate-limit quota) and reads the roster, so it must never
be network-exposed.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from tanglebrain.gui.views import (
    DEFAULT_PORT,
    run_prompt,
    save_pricing_view,
    view_pricing,
    view_roster,
    view_stats,
)

_JSON = "application/json; charset=utf-8"
_HTML = "text/html; charset=utf-8"


def _index_html() -> bytes:
    """Read the packaged single-file panel (``tanglebrain/gui/static/index.html``)."""
    return (resources.files("tanglebrain.gui") / "static" / "index.html").read_bytes()


def _json(status: int, obj: object) -> tuple[int, str, bytes]:
    """Serialize ``obj`` as a JSON HTTP response triple."""
    return status, _JSON, json.dumps(obj).encode("utf-8")


def dispatch(method: str, path: str, body: bytes = b"") -> tuple[int, str, bytes]:
    """Route one request to a view and return ``(status, content_type, body)``.

    Pure and side-effect-light (only the views touch config/log/subprocess), so tests call it
    directly with no socket. The query string, if any, is ignored.

    Args:
        method: HTTP method (``GET``/``POST``).
        path: Request path (may include a ``?query``).
        body: Raw request body bytes (for ``POST``).

    Returns:
        ``(status_code, content_type, body_bytes)``.
    """
    path = path.split("?", 1)[0]

    if method == "GET":
        if path in ("/", "/index.html"):
            return 200, _HTML, _index_html()
        view = {"/api/roster": view_roster, "/api/pricing": view_pricing, "/api/stats": view_stats}.get(path)
        if view is not None:
            try:
                return _json(200, view())
            except Exception as exc:  # a read view failed (e.g. malformed roster) — clean JSON, not a 500 traceback
                return _json(500, {"error": str(exc)})
        return _json(404, {"error": "not found"})

    if method == "POST":
        action = {"/api/run": run_prompt, "/api/pricing": save_pricing_view}.get(path)
        if action is not None:
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                return _json(400, {"ok": False, "error": "invalid JSON body"})
            if not isinstance(payload, dict):
                return _json(400, {"ok": False, "error": "body must be a JSON object"})
            result = action(payload)
            return _json(200 if result.get("ok") else 400, result)
        return _json(404, {"error": "not found"})

    return _json(405, {"error": "method not allowed"})


class Handler(BaseHTTPRequestHandler):
    """Thin HTTP handler delegating all routing to :func:`dispatch`."""

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        """Handle a GET by dispatching and writing the response."""
        self._respond(*dispatch("GET", self.path))

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        """Handle a POST by reading the body, dispatching, and writing the response."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self._respond(*dispatch("POST", self.path, body))

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
    """Console entry point: serve the knob panel until interrupted.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (``0``).
    """
    parser = argparse.ArgumentParser(
        prog="tanglebrain-gui",
        description="Serve the TangleBrain knob panel (read-only) on localhost.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"Port to bind (default {DEFAULT_PORT}, registered in PortHub).",
    )
    args = parser.parse_args(argv)

    # Loopback only, not configurable: the panel is unauthenticated and runs prompts / reads the
    # roster, so it must never be reachable off the machine.
    host = "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), Handler)
    print(f"TangleBrain knob panel: http://{host}:{args.port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
