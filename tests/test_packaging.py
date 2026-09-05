"""Tests for packaging metadata invariants declared in pyproject.toml.

Dependency constraints are not exercised by any runtime code path, so nothing catches a bad one
until an install resolves differently — which can be months after the constraint was written, in
someone else's environment. These tests pin the invariants that have already bitten.

The history behind them: the ``delegate`` extra shipped as ``mcp >= 1.0``. When the SDK released
2.0.0 it renamed ``FastMCP`` to ``MCPServer`` and dropped the ``mcp.server.fastmcp`` import path
``tanglebrain/mcp_server.py`` uses, so every fresh resolve installed an SDK the code could not
import. Nothing in the repo had changed. The defect was never the SDK's release — it was the
open-ended constraint that let a major land unannounced.
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
class DependencyBoundTest(unittest.TestCase):
    """Every declared dependency must carry an upper bound, and ``mcp`` must stay below 2.x."""

    def setUp(self):
        """Parse pyproject.toml once per test."""
        self.pyproject = tomllib.loads(PYPROJECT_PATH.read_text())

    def _all_requirements(self):
        """Yield ``(source, requirement)`` for every dependency the project declares.

        Covers the core ``project.dependencies`` list and every
        ``project.optional-dependencies`` extra, so a dependency added to a new extra is held to
        the same rule without anyone remembering to extend this test.

        Returns:
            A list of ``(source, requirement)`` pairs, where ``source`` names the table the
            requirement came from (for a legible failure message).
        """
        project = self.pyproject["project"]
        pairs = [("dependencies", req) for req in project.get("dependencies", [])]
        for extra, reqs in project.get("optional-dependencies", {}).items():
            pairs.extend((f"optional-dependencies.{extra}", req) for req in reqs)
        return pairs

    def test_every_declared_dependency_carries_an_upper_bound(self):
        # The general rule, not a restatement of the mcp case: an unbounded constraint lets a
        # major version land in a user's fresh install with no announcement, no commit to blame,
        # and a green CI that predates the release. Bounding every declared dependency makes a
        # major bump a deliberate act with a migration behind it.
        requirements = self._all_requirements()
        self.assertTrue(requirements, "expected pyproject to declare at least one dependency")
        for source, requirement in requirements:
            with self.subTest(source=source, requirement=requirement):
                self.assertIn(
                    "<",
                    requirement,
                    f"{source}: {requirement!r} has no upper bound — an unbounded constraint "
                    "lets the next major land unannounced (see the module docstring)",
                )

    def test_mcp_stays_below_the_2x_api_break(self):
        # Stronger than the general rule above, and separate from it on purpose. `mcp` needs a
        # specific ceiling rather than merely *a* ceiling, because 2.x is an API break this code
        # has not migrated to — `mcp >= 2, < 3` would satisfy the general rule while shipping an
        # extra that cannot import. Raising this is the migration, so it must fail loudly and be
        # changed deliberately rather than drift upward with a dependency refresh.
        delegate = self.pyproject["project"]["optional-dependencies"]["delegate"]
        mcp_requirements = [req for req in delegate if req.startswith("mcp")]
        self.assertEqual(len(mcp_requirements), 1, "expected exactly one mcp requirement")
        self.assertIn(
            "< 2",
            mcp_requirements[0],
            f"the mcp requirement must stay below 2.x until mcp_server.py is migrated, got "
            f"{mcp_requirements[0]!r}",
        )


if __name__ == "__main__":
    unittest.main()
