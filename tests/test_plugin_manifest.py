"""Tests for the Claude Code plugin manifests (.claude-plugin/ + plugins/tanglebrain-delegate/).

The plugin is pure declarative wiring — JSON manifests that register the ``tanglebrain-delegate``
console script as a stdio MCP server. Nothing imports these files at runtime, so CI is the only
thing that catches drift: a renamed console script, a marketplace entry pointing at a missing
directory, or a plugin/marketplace name mismatch would otherwise ship silently broken.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKETPLACE_PATH = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_DIR = REPO_ROOT / "plugins" / "tanglebrain-delegate"
PLUGIN_MANIFEST_PATH = PLUGIN_DIR / ".claude-plugin" / "plugin.json"


class MarketplaceManifestTest(unittest.TestCase):
    def setUp(self):
        self.marketplace = json.loads(MARKETPLACE_PATH.read_text())

    def test_required_fields(self):
        # Users install as `<plugin>@<marketplace-name>` — the name is part of the public interface.
        self.assertEqual(self.marketplace["name"], "tanglebrain")
        self.assertTrue(self.marketplace["owner"]["name"])

    def test_plugin_entry_points_at_an_existing_plugin_dir(self):
        entries = self.marketplace["plugins"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["name"], "tanglebrain-delegate")
        source = entry["source"]
        self.assertTrue(source.startswith("./"), "source must be repo-root-relative (./...)")
        self.assertEqual((REPO_ROOT / source).resolve(), PLUGIN_DIR)
        self.assertTrue(PLUGIN_MANIFEST_PATH.is_file())


class PluginManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(PLUGIN_MANIFEST_PATH.read_text())

    def test_name_matches_the_marketplace_entry(self):
        marketplace = json.loads(MARKETPLACE_PATH.read_text())
        self.assertEqual(self.manifest["name"], marketplace["plugins"][0]["name"])

    def test_mcp_server_command_is_the_pyproject_console_script(self):
        # The plugin wires (not vendors) the pip-installed entry point; if the console script is
        # renamed in pyproject.toml this must fail. String check because tomllib needs 3.11+ and
        # the package floor is 3.10.
        server = self.manifest["mcpServers"]["tanglebrain-delegate"]
        self.assertEqual(server["command"], "tanglebrain-delegate")
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        self.assertIn('tanglebrain-delegate = "tanglebrain.mcp_server:main"', pyproject)

    def test_version_is_semver_shaped(self):
        major, minor, patch = self.manifest["version"].split(".")
        for part in (major, minor, patch):
            self.assertTrue(part.isdigit())


class InstallDocsParityTest(unittest.TestCase):
    """The `/plugin install <plugin>@<marketplace>` string in the docs is derived from BOTH
    manifests' name fields — a rename that updates the manifests (and the manifest tests) can still
    leave the documented command stale, so pin the docs to the manifests directly."""

    def test_readmes_document_the_manifest_derived_install_command(self):
        marketplace = json.loads(MARKETPLACE_PATH.read_text())
        plugin = json.loads(PLUGIN_MANIFEST_PATH.read_text())
        install_cmd = f"/plugin install {plugin['name']}@{marketplace['name']}"
        for doc in (REPO_ROOT / "README.md", PLUGIN_DIR / "README.md"):
            self.assertIn(install_cmd, doc.read_text(), f"stale install command in {doc.name}")


if __name__ == "__main__":
    unittest.main()
