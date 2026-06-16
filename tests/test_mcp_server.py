"""Tests for the MCP delegate server (tanglebrain/mcp_server.py).

Skipped when the optional `mcp` SDK is not installed (the server is an opt-in extra:
`pip install "tanglebrain[delegate]"`). When present, these assert the tool is registered and
that invoking it delegates to run_local_delegate — without spawning a server or hitting gpt-oss.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

try:
    import mcp  # noqa: F401

    HAS_MCP = True
except ImportError:
    HAS_MCP = False


def run(coro):
    """Run an async coroutine synchronously (FastMCP's list/call APIs are async)."""
    return asyncio.run(coro)


@unittest.skipUnless(HAS_MCP, "install the 'delegate' extra (mcp) to run the MCP server tests")
class McpServerTest(unittest.TestCase):
    def setUp(self):
        # Imported lazily so the module (which imports mcp) is only loaded when mcp is present.
        from tanglebrain import mcp_server

        self.server = mcp_server

    def test_tool_is_registered(self):
        tools = run(self.server.mcp.list_tools())
        self.assertIn("delegate_local", [t.name for t in tools])

    def test_tool_exposes_max_tokens_param(self):
        tool = next(t for t in run(self.server.mcp.list_tools()) if t.name == "delegate_local")
        # The tool advertises both params to the orchestrator via its input schema.
        props = tool.inputSchema.get("properties", {})
        self.assertIn("prompt", props)
        self.assertIn("max_tokens", props)

    def test_invoking_tool_delegates(self):
        with patch(
            "tanglebrain.mcp_server.run_local_delegate", return_value="delegated text"
        ) as delegated:
            result = run(self.server.mcp.call_tool("delegate_local", {"prompt": "do grunt"}))
        # FastMCP returns (content_blocks, structured_result); pull the text out of the blocks
        # rather than stringifying the whole tuple, so we assert on the actual tool output.
        content_blocks = result[0]
        texts = [c.text for c in content_blocks if getattr(c, "type", None) == "text"]
        self.assertIn("delegated text", texts)
        delegated.assert_called_once()
        self.assertEqual(delegated.call_args.args[0], "do grunt")
        # Default max_tokens (2048) is passed when the caller omits it.
        self.assertEqual(delegated.call_args.kwargs.get("max_tokens"), 2048)

    def test_invoking_tool_threads_max_tokens(self):
        with patch(
            "tanglebrain.mcp_server.run_local_delegate", return_value="x"
        ) as delegated:
            run(self.server.mcp.call_tool("delegate_local", {"prompt": "q", "max_tokens": 256}))
        self.assertEqual(delegated.call_args.kwargs.get("max_tokens"), 256)


if __name__ == "__main__":
    unittest.main()
