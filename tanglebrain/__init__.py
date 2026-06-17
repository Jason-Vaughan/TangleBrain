"""TangleBrain — a cost-tiered LLM router.

Routes each task to the cheapest tier that can do it: free local (gpt-oss-120b on Monad via
LiteLLM) first, flat-rate subscription CLIs (Claude / Codex / Gemini) next, paid API last. The
optimization target is tier-fit + rate-limit spread, NOT $/token.

The router is frontier-first: a subscription orchestrator decomposes the task and offloads grunt
to free local gpt-oss over an MCP delegate, rotating across the subs for rate-limit runway with
429 failover, and falling through to a gated, **off-by-default** paid-API tier only as a genuine
last resort. See ``.claude/plans/tanglebrain.md`` for the full design.

``__version__`` is read from the installed package metadata — the single source of truth is
``pyproject.toml`` — so it can never drift from the released version. When imported from a source
checkout that was never installed, it falls back to a clearly-not-a-release sentinel.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tanglebrain")
except PackageNotFoundError:  # running from a source tree that hasn't been installed
    __version__ = "0.0.0+unknown"
