"""Bind-address contract for the two HTTP surfaces.

``tanglebrain-gui`` and ``tanglebrain-serve`` are both unauthenticated and both spend real
backend quota. The ``127.0.0.1`` bind is therefore not a default — it is the entire
authorization model, and widening it does not weaken the posture, it voids it.

These tests assert the *address handed to the server*, never merely that a server starts: a
test that binds and connects over localhost passes just as happily against ``0.0.0.0``, which
is precisely the change that must fail. Nothing here opens a real socket.
"""

from __future__ import annotations

import contextlib
import io
import ipaddress
import unittest
from unittest import mock

from tanglebrain.gui import server as gui_server
from tanglebrain.serve import server as serve_server

# Both console entry points, exercised identically — the invariant is the same for each.
SURFACES = (
    ("tanglebrain-gui", gui_server),
    ("tanglebrain-serve", serve_server),
)


def bind_address(module, argv: list[str] | None = None) -> tuple[tuple[str, int], int]:
    """Run a server module's ``main`` and capture the address it would bind.

    The real ``ThreadingHTTPServer`` is replaced by a recorder, so no socket is opened and no
    port is claimed. The stand-in raises ``KeyboardInterrupt`` from ``serve_forever`` to unwind
    ``main`` through its own shutdown path rather than a special test branch.

    Args:
        module: The server module under test (``gui.server`` or ``serve.server``).
        argv: Argument list passed to ``main``. Defaults to no arguments.

    Returns:
        A ``((host, port), exit_code)`` pair.
    """
    captured: dict[str, tuple[str, int]] = {}

    class Recorder:
        """Stands in for ``ThreadingHTTPServer``, recording the requested bind address."""

        def __init__(self, address: tuple[str, int], handler: object) -> None:
            """Record the address the server was asked to bind.

            Args:
                address: The ``(host, port)`` tuple under test.
                handler: The request handler class; unused.
            """
            captured["address"] = address

        def serve_forever(self) -> None:
            """Unwind ``main`` immediately via its existing interrupt path."""
            raise KeyboardInterrupt

        def server_close(self) -> None:
            """No socket was opened, so closing is a no-op."""

    with mock.patch.object(module, "ThreadingHTTPServer", Recorder):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = module.main(argv or [])
    return captured["address"], code


class BindAddressTest(unittest.TestCase):
    """The loopback bind is the authorization model; these are its regression tests."""

    def test_binds_loopback_only(self) -> None:
        """Each surface binds a loopback address, and exits cleanly."""
        for name, module in SURFACES:
            with self.subTest(surface=name):
                (host, _port), code = bind_address(module)
                self.assertEqual(
                    host,
                    "127.0.0.1",
                    f"{name} must bind 127.0.0.1; it is unauthenticated and spends quota",
                )
                self.assertTrue(
                    ipaddress.ip_address(host).is_loopback,
                    f"{name} bound {host!r}, which is reachable off this machine",
                )
                self.assertEqual(code, 0)

    def test_port_flag_does_not_widen_the_bind(self) -> None:
        """Choosing a port stays a port choice — it must not reach the host."""
        for name, module in SURFACES:
            with self.subTest(surface=name):
                (host, port), _code = bind_address(module, ["--port", "45999"])
                self.assertEqual(port, 45999)
                self.assertEqual(host, "127.0.0.1", f"{name} let --port alter the bind host")

    def test_host_is_not_configurable(self) -> None:
        """Both modules document the bind as not configurable; no flag may override it.

        Adding a ``--host`` flag would void the invariant without ever touching the literal,
        so the absence of that flag is part of the contract rather than an implementation
        detail.
        """
        for name, module in SURFACES:
            with self.subTest(surface=name):
                with self.assertRaises(SystemExit) as raised:
                    bind_address(module, ["--host", "0.0.0.0"])
                self.assertEqual(
                    raised.exception.code,
                    2,
                    f"{name} accepted a --host argument; the bind must not be configurable",
                )


if __name__ == "__main__":
    unittest.main()
