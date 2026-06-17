"""Tier adapters.

Each tier is a node/adapter behind one uniform interface — ``run(prompt, opts) -> text``
(plan §4) — so adding or removing a model is local and contained. C1 ships only the
``openai-compat`` adapter (the free local tier); the ``cli`` adapter for the subscription tier
(claude / codex / gemini) lands in C2.
"""
from __future__ import annotations

from tanglebrain.adapters.api import ApiAdapter
from tanglebrain.adapters.base import Adapter, AdapterError
from tanglebrain.adapters.cli import CliAdapter
from tanglebrain.adapters.openai_compat import OpenAICompatAdapter

__all__ = ["Adapter", "AdapterError", "ApiAdapter", "CliAdapter", "OpenAICompatAdapter"]
