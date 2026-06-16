"""TangleBrain — a cost-tiered LLM router.

Routes each task to the cheapest tier that can do it: free local (gpt-oss-120b on local via
LiteLLM) first, flat-rate subscription CLIs next, paid API last. The optimization target is
tier-fit + rate-limit spread, NOT $/token.

C1 (this version) ships the foundation: a config-driven roster loader, an openai-compat
adapter to the free local tier, and a trivial local-first selector that routes one request
end-to-end. The cost-tiered router itself (orchestrator selection, rotation, failover) lands
in later chunks — see ``.claude/plans/tanglebrain.md``.
"""
from __future__ import annotations

__version__ = "0.1.0"
