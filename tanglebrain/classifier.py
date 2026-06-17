"""Cheap local classifier gate (plan §6 evolution path) — OFF by default.

The §6 routing strategy is frontier-first with multi-orchestrator rotation: every request consumes a
sub's rate-limit budget. The plan's escape valve, "only if volume demands": put a **cheap local
classifier in front** that does one narrow job — decide whether a request is *trivial* (free local
gpt-oss can fully handle it) or *needs-frontier* (route to the orchestrator). Trivial requests then
skip the subs entirely, preserving rate-limit runway.

Two deliberate design rules from §6:

- **Narrow classification, not self-judgement.** The classifier rates *task complexity*, it does not
  ask the local model "can YOU do this?" — that framing is unreliable.
- **Fail safe toward the capable path.** Any ambiguity, parse miss, or classifier error resolves to
  ``frontier``. The gate must never trap a hard task on the local tier because the classifier was
  unsure or broke — at worst it falls back to today's normal frontier-first routing.

This is built ahead of the §8 data trigger (no rate-limit pressure observed yet), so it is inert
unless explicitly enabled (``classifier_gate_enabled`` in settings, or ``--gate`` on the CLI).
"""
from __future__ import annotations

import re
from typing import Callable

from tanglebrain.adapters import OpenAICompatAdapter
from tanglebrain.adapters.base import Adapter
from tanglebrain.roster import Roster, load_roster
from tanglebrain.selector import select_local

TRIVIAL = "trivial"
FRONTIER = "frontier"

# gpt-oss spends part of its budget on internal reasoning (the C0 lesson), so give the classify call
# enough headroom to finish reasoning AND emit the verdict; a truncated (null) response just fails
# safe to FRONTIER. Kept modest because this runs in front of every gated request.
CLASSIFY_MAX_TOKENS = 1024

_INSTRUCTIONS = (
    "You are a routing classifier. Decide how complex the USER REQUEST below is, so a dispatcher "
    "can send simple work to a small local model and hard work to a frontier model.\n\n"
    "Classify by the TASK's intrinsic complexity (not by who should do it):\n"
    "- TRIVIAL: short, well-specified, single-step work — a factual lookup, a small/simple code "
    "snippet, a quick rewrite or format, a direct question with a known answer.\n"
    "- FRONTIER: anything needing multi-step reasoning, decomposition, architecture or design, "
    "debugging across files, careful trade-offs, or ambiguous/open-ended judgement.\n\n"
    "Decide quickly; do not overthink and do not attempt the task. When unsure, answer FRONTIER."
)


def _build_prompt(user_request: str) -> str:
    """Fold the classify instructions and the request into one user message (the adapter sends one)."""
    return (
        f"{_INSTRUCTIONS}\n\n--- USER REQUEST ---\n{user_request}\n--- END REQUEST ---\n\n"
        "Answer with exactly one word: TRIVIAL or FRONTIER."
    )


def _parse_verdict(text: str) -> str:
    """Map a classifier response to ``TRIVIAL`` or ``FRONTIER``, defaulting to ``FRONTIER``.

    We instruct the model to answer with exactly one word, so the verdict is ``TRIVIAL`` **only** when
    the response's first word token is exactly ``trivial`` and ``frontier`` appears nowhere. Anything
    else — ``frontier``, a both-words answer, prose, a *negation* like "not trivial", junk, empty — is
    ``FRONTIER`` (the safe default). The first-token rule is deliberately strict: free-form prose can't
    leak a spurious ``TRIVIAL`` and strand a hard task on local.
    """
    words = re.findall(r"[a-z]+", (text or "").lower())
    if "frontier" in words:
        return FRONTIER
    return TRIVIAL if words[:1] == ["trivial"] else FRONTIER


def classify(
    prompt: str,
    roster: Roster | None = None,
    adapter_factory: Callable[..., Adapter] = OpenAICompatAdapter.from_entry,
    max_tokens: int = CLASSIFY_MAX_TOKENS,
) -> str:
    """Classify ``prompt`` as :data:`TRIVIAL` or :data:`FRONTIER` using free local gpt-oss.

    Never raises and never blocks routing: any failure (no local entry, transport error, truncated
    or unparsable response) resolves to :data:`FRONTIER`, so a broken classifier degrades to today's
    normal frontier-first routing rather than trapping a task on the local tier.

    Args:
        prompt: The user request to classify.
        roster: The loaded roster (defaults to the packaged roster).
        adapter_factory: Builds the local adapter from the selected entry (injectable for tests).
        max_tokens: Budget for the classify call (gpt-oss needs reasoning headroom; see the constant).

    Returns:
        :data:`TRIVIAL` or :data:`FRONTIER`.
    """
    try:
        entry = select_local(roster if roster is not None else load_roster())
        adapter = adapter_factory(entry)
        text = adapter.run(_build_prompt(prompt), {"max_tokens": max_tokens})
    except Exception:  # noqa: BLE001 — deliberately total: a broken classifier must fail safe, never block
        return FRONTIER
    return _parse_verdict(text)
