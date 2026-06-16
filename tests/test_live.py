"""Opt-in live end-to-end test against the real Monad LiteLLM endpoint.

Skipped by default — it needs network access to Monad over the tailnet and the scoped key at
``~/.config/monad/tanglebrain-spike.key``. Run it explicitly::

    make test-live
    # or
    TANGLEBRAIN_LIVE=1 python -m unittest tests.test_live -v

This is the C1 "definition of done" check: one request routes roster → local → gpt-oss → text.
"""
from __future__ import annotations

import os
import unittest

from tanglebrain.cli import run_once

LIVE = os.environ.get("TANGLEBRAIN_LIVE") == "1"


@unittest.skipUnless(LIVE, "set TANGLEBRAIN_LIVE=1 to run the live endpoint test")
class LiveEndToEndTest(unittest.TestCase):
    def test_one_request_routes_to_local_and_returns_text(self):
        text = run_once("Reply with exactly the word: pong", max_tokens=2048)
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip(), "expected non-empty text from gpt-oss-120b")


if __name__ == "__main__":
    unittest.main()
