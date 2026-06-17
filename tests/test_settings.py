"""Tests for the global settings loader (tanglebrain/settings.py) — the paid-API billing gate."""
from __future__ import annotations

import os
import tempfile
import unittest

from tanglebrain.settings import (
    Settings,
    SettingsError,
    default_settings_path,
    load_settings,
)


def write_yaml(text: str, test: unittest.TestCase) -> str:
    """Write YAML to a temp file and return its path, registering cleanup on the test."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(text)
    handle.close()
    test.addCleanup(os.unlink, handle.name)
    return handle.name


class PackagedSettingsTest(unittest.TestCase):
    """The settings shipped with the package parse and keep billing OFF by default."""

    def test_default_path_points_at_packaged_yaml(self):
        self.assertTrue(default_settings_path().exists())
        self.assertEqual(default_settings_path().name, "settings.yaml")

    def test_packaged_settings_ship_billing_disabled(self):
        # The safety contract: the shipped gate must be off.
        self.assertFalse(load_settings().api_billing_enabled)


class LoadSettingsTest(unittest.TestCase):
    """Loading defaults safely on absence, but hard-fails on a malformed gate."""

    def test_missing_file_yields_safe_defaults(self):
        settings = load_settings("/no/such/settings.yaml")
        self.assertEqual(settings, Settings())
        self.assertFalse(settings.api_billing_enabled)

    def test_empty_file_yields_defaults(self):
        self.assertFalse(load_settings(write_yaml("", self)).api_billing_enabled)

    def test_explicit_true_enables(self):
        self.assertTrue(load_settings(write_yaml("api_billing_enabled: true\n", self)).api_billing_enabled)

    def test_explicit_false_disables(self):
        self.assertFalse(load_settings(write_yaml("api_billing_enabled: false\n", self)).api_billing_enabled)

    def test_absent_key_defaults_off(self):
        self.assertFalse(load_settings(write_yaml("something_else: 1\n", self)).api_billing_enabled)

    def test_non_mapping_rejected(self):
        with self.assertRaises(SettingsError):
            load_settings(write_yaml("- just\n- a\n- list\n", self))

    def test_non_bool_gate_rejected(self):
        # A stray non-bool must NOT be coerced into "billing enabled". (Bare yes/no/on/off ARE
        # YAML booleans in PyYAML, so they are legitimately accepted; these are the non-bools.)
        for bad in ("1", "'true'", "1.5"):
            with self.assertRaises(SettingsError):
                load_settings(write_yaml(f"api_billing_enabled: {bad}\n", self))

    def test_invalid_yaml_rejected(self):
        with self.assertRaises(SettingsError):
            load_settings(write_yaml("api_billing_enabled: : :\n", self))


if __name__ == "__main__":
    unittest.main()
