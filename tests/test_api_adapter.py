"""Tests for the paid-API adapter (tanglebrain/adapters/api.py).

The ApiAdapter rides the *same* transport as the openai-compat adapter (LiteLLM is OpenAI-compat);
these tests confirm the from_entry wiring, the kind-guard, and that the virtual key is never read at
construction time (secret-safety). The shared transport itself is covered by test_openai_compat.py.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from tanglebrain.adapters import ApiAdapter, OpenAICompatAdapter
from tanglebrain.adapters.base import AdapterError
from tanglebrain.roster import Invoke, RosterEntry

URL = "http://litellm.example:4000/v1"


def api_entry(key_ref: str = "file:/no/such/virtual.key") -> RosterEntry:
    return RosterEntry(
        id="gpt-5",
        tier="api",
        invoke=Invoke(kind="api", base_url=URL, model="gpt-5", key_ref=key_ref),
    )


class FromEntryTest(unittest.TestCase):
    def test_builds_from_api_entry(self):
        adapter = ApiAdapter.from_entry(api_entry())
        self.assertIsInstance(adapter, ApiAdapter)
        self.assertEqual(adapter.model, "gpt-5")
        self.assertEqual(adapter.base_url, URL)
        self.assertEqual(adapter.key_ref, "file:/no/such/virtual.key")

    def test_is_an_openai_compat_adapter(self):
        # The paid tier is the same transport as free local, by design (LiteLLM-fronted).
        self.assertIsInstance(ApiAdapter.from_entry(api_entry()), OpenAICompatAdapter)

    def test_rejects_non_api_entry(self):
        entry = RosterEntry(id="claude", tier="sub", invoke=Invoke(kind="cli", cmd=["claude"]))
        with self.assertRaises(AdapterError):
            ApiAdapter.from_entry(entry)

    def test_key_ref_not_read_at_construction(self):
        # Constructing an adapter for an entry whose (virtual) key file is absent must NOT fail —
        # the credential resolves lazily on run(). Points key_ref at a missing path and builds fine.
        adapter = ApiAdapter.from_entry(api_entry(key_ref="file:/definitely/missing.key"))
        self.assertEqual(adapter.key_ref, "file:/definitely/missing.key")


class RunTest(unittest.TestCase):
    """run() inherits the openai-compat transport; spot-check it carries the Bearer virtual key."""

    def _fake_client(self, response: httpx.Response) -> MagicMock:
        fake = MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        fake.post.return_value = response
        return fake

    def test_sends_bearer_virtual_key_and_returns_content(self):
        request = httpx.Request("POST", f"{URL}/chat/completions")
        resp = httpx.Response(200, request=request, json={"choices": [{"message": {"content": "paid answer"}}]})
        fake = self._fake_client(resp)
        with patch("tanglebrain.adapters.openai_compat.resolve_key_ref", return_value="sk-virtual-7"):
            with patch("tanglebrain.adapters.openai_compat.httpx.Client", return_value=fake):
                out = ApiAdapter.from_entry(api_entry()).run("hard question")
        self.assertEqual(out, "paid answer")
        self.assertEqual(fake.post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-virtual-7")


if __name__ == "__main__":
    unittest.main()
