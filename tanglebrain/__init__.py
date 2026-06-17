"""TangleBrain — a local-first, config-driven router across OpenAI-compatible backends you own.

Routes each task to the cheapest capable tier: a free local model (via Ollama or any
OpenAI-compatible server you run) first, opt-in authenticated CLIs next, and your own paid API keys
as a last resort.

The default path is frontier-first orchestration: a configured orchestrator decomposes the task and
offloads sub-tasks to the free local backend over an MCP delegate, with rotation across the
orchestrators and failover when one errors, falling through to a gated, **off-by-default** paid-API
tier only as a genuine last resort. See ``ARCHITECTURE.md`` for the full design.

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
