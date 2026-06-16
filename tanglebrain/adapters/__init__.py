"""Tier adapters.

Each tier is a node/adapter behind one uniform interface — ``run(prompt, opts) -> text``
(plan §4) — so adding or removing a model is local and contained. C1 ships only the
``openai-compat`` adapter (the free local tier); the CLI adapters for the subscription tier
land in C2.
"""
from __future__ import annotations

from tanglebrain.adapters.base import Adapter
from tanglebrain.adapters.openai_compat import AdapterError, OpenAICompatAdapter

__all__ = ["Adapter", "AdapterError", "OpenAICompatAdapter"]
