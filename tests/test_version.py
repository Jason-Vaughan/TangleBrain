"""Tests for the package version (tanglebrain.__version__).

The version must come from the installed distribution metadata (driven by ``pyproject.toml``), not
a hardcoded literal, so it never drifts from the released version again.
"""
from __future__ import annotations

import unittest
from importlib.metadata import version

import tanglebrain


class VersionTest(unittest.TestCase):
    def test_version_is_a_nonempty_string(self):
        self.assertIsInstance(tanglebrain.__version__, str)
        self.assertTrue(tanglebrain.__version__)

    def test_version_tracks_installed_metadata(self):
        # Single source of truth: __version__ == the installed dist's version (from pyproject).
        # The dev/test env installs the package editable (`make venv`), so metadata is present.
        self.assertEqual(tanglebrain.__version__, version("tanglebrain"))

    def test_not_the_stale_literal_or_sentinel(self):
        # Regression guards: the old frozen literal value and the uninstalled fallback must not
        # appear in an installed environment.
        self.assertNotEqual(tanglebrain.__version__, "0.1.0")
        self.assertNotEqual(tanglebrain.__version__, "0.0.0+unknown")


if __name__ == "__main__":
    unittest.main()
