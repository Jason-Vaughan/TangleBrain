"""Permission warning for ``key_ref: file:PATH`` credentials.

``ARCHITECTURE.md`` describes the intended posture as a ``0600`` file, and nothing verified it
— a world-readable key was read silently. These tests pin the warning's behaviour, including
the two properties that decide whether it is useful in practice: it must not fail the run, and
it must not repeat per call.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from tanglebrain.adapters import openai_compat
from tanglebrain.adapters.openai_compat import resolve_key_ref


@unittest.skipUnless(os.name == "posix", "POSIX mode bits only")
class KeyFilePermissionWarningTest(unittest.TestCase):
    """The warning fires on loose modes, stays silent on 0600, and never blocks the read."""

    def setUp(self) -> None:
        """Isolate each test from the process-wide once-per-file warning ledger."""
        openai_compat._PERMISSION_WARNED.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key_path = Path(self._tmp.name) / "api.key"
        self.key_path.write_text("sk-secret\n")

    def resolve(self, mode: int) -> tuple[str | None, str]:
        """Resolve the key file at ``mode`` and capture anything written to stderr.

        Args:
            mode: Permission bits to apply before resolving.

        Returns:
            A ``(resolved_key, stderr_text)`` pair.
        """
        self.key_path.chmod(mode)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            key = resolve_key_ref(f"file:{self.key_path}")
        return key, err.getvalue()

    def test_owner_only_is_silent(self) -> None:
        """A 0600 file is the intended posture and must produce no noise."""
        key, err = self.resolve(0o600)
        self.assertEqual(key, "sk-secret")
        self.assertEqual(err, "")

    def test_group_readable_warns(self) -> None:
        """Group-readable is a real misconfiguration and must be surfaced."""
        key, err = self.resolve(0o640)
        self.assertEqual(key, "sk-secret", "the warning must not block the read")
        self.assertIn("readable beyond its owner", err)
        self.assertIn("0640", err)
        self.assertIn(str(self.key_path), err)

    def test_warning_never_echoes_the_credential(self) -> None:
        """The warning names the path and mode, never the secret it is about.

        Holds by construction today (the helper never reads the file), which is exactly why it
        needs pinning: nothing would fail if a future edit added the key to the message.
        """
        _key, err = self.resolve(0o644)
        self.assertNotIn("sk-secret", err)

    def test_world_readable_warns(self) -> None:
        """World-readable is the worst case and must be surfaced."""
        _key, err = self.resolve(0o644)
        self.assertIn("readable beyond its owner", err)
        self.assertIn("0644", err)

    def test_warning_does_not_repeat_for_the_same_file(self) -> None:
        """A per-call warning would train the operator to ignore it.

        The credential is resolved on every routed request, so this fires once per file per
        process — the second resolve must be silent while still returning the key.
        """
        _first, first_err = self.resolve(0o644)
        self.assertIn("readable beyond its owner", first_err)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            key = resolve_key_ref(f"file:{self.key_path}")
        self.assertEqual(key, "sk-secret")
        self.assertEqual(err.getvalue(), "", "the warning repeated on a second resolve")

    def test_a_second_file_still_warns(self) -> None:
        """Suppression is per file, not global — a different loose key must still warn."""
        self.resolve(0o644)
        other = Path(self._tmp.name) / "other.key"
        other.write_text("sk-other\n")
        other.chmod(0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            resolve_key_ref(f"file:{other}")
        self.assertIn(str(other), err.getvalue())

    def test_missing_file_still_raises(self) -> None:
        """The permission check must not disturb the existing not-found contract."""
        missing = Path(self._tmp.name) / "absent.key"
        with self.assertRaises(Exception) as raised:
            resolve_key_ref(f"file:{missing}")
        self.assertIn("not found", str(raised.exception))



@unittest.skipUnless(os.name == "posix", "POSIX mode bits only")
class UnreadableKeyFileTest(unittest.TestCase):
    """An unreadable key file must surface as AdapterError, not a raw OSError.

    ``cli.main`` catches ``AdapterError`` and prints a single ``tanglebrain: ...`` line. A
    ``PermissionError`` escaping that clause gives the operator a Python traceback instead, and
    contradicts the function's documented ``Raises:`` contract.
    """

    def test_unreadable_file_raises_adapter_error(self) -> None:
        """A present-but-unreadable credential file yields the documented error type."""
        from tanglebrain.adapters.base import AdapterError

        if os.geteuid() == 0:
            self.skipTest("root bypasses file permissions")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locked.key"
            path.write_text("sk-secret\n")
            path.chmod(0o000)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(AdapterError) as raised:
                    resolve_key_ref(f"file:{path}")
            self.assertIn("unreadable", str(raised.exception))

if __name__ == "__main__":
    unittest.main()
