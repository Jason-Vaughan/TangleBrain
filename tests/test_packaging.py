"""Tests for packaging metadata invariants declared in pyproject.toml.

Dependency constraints are not exercised by any runtime code path, so nothing catches a bad one
until an install resolves differently — which can be months after the constraint was written, in
someone else's environment. These tests pin the invariants that have already bitten.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only, where the check skips
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


@unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+; covered by the 3.11/3.12 CI jobs")
class OptionalDependencyPinTest(unittest.TestCase):
    """The ``delegate`` extra must not resolve to an SDK this code cannot import."""

    def setUp(self):
        """Parse pyproject.toml once per test."""
        self.pyproject = tomllib.loads(PYPROJECT_PATH.read_text())

    def _delegate_requirements(self):
        """Return the requirement strings declared by the ``delegate`` optional-dependency extra."""
        return self.pyproject["project"]["optional-dependencies"]["delegate"]

    def test_mcp_requirement_declares_an_upper_bound(self):
        # The SDK's 2.0.0 renamed FastMCP to MCPServer and dropped the `mcp.server.fastmcp` path
        # that tanglebrain/mcp_server.py imports. An open-ended `mcp >= 1.0` therefore ships a
        # delegate extra that cannot import — it did, in v0.20.0. Lifting this cap is a migration,
        # not a version bump, so the bound has to be deliberate rather than absent.
        mcp_requirements = [req for req in self._delegate_requirements() if req.startswith("mcp")]
        self.assertEqual(len(mcp_requirements), 1, "expected exactly one mcp requirement")
        requirement = mcp_requirements[0]
        self.assertIn(
            "<",
            requirement,
            f"the mcp requirement must carry an upper bound, got {requirement!r}",
        )


if __name__ == "__main__":
    unittest.main()
