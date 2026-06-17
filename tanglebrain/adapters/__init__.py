"""Tier adapters.

Each tier is a node/adapter behind one uniform interface — ``run(prompt, opts) -> text`` — so
adding or removing a backend is local and contained. The ``openai-compat`` adapter serves the free
local tier; the ``cli`` adapter drives an authenticated CLI; the ``api`` adapter serves a paid
backend.
"""
from __future__ import annotations

from tanglebrain.adapters.api import ApiAdapter
from tanglebrain.adapters.base import Adapter, AdapterError
from tanglebrain.adapters.cli import CliAdapter
from tanglebrain.adapters.openai_compat import OpenAICompatAdapter

__all__ = ["Adapter", "AdapterError", "ApiAdapter", "CliAdapter", "OpenAICompatAdapter"]
