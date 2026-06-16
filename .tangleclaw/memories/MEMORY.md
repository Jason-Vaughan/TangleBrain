# Session Memory — TangleBrain

This file persists context across AI sessions. Read it at session start.

## What TangleBrain is

A **cost-tiered LLM router**: free local first → flat-rate subscription CLIs → paid API last.
Optimize for tier-fit + rate-limit spread, **NOT $/token**. Canonical docs (the source of
truth — read these, don't re-derive from this file):

- **Plan:** `.claude/plans/tanglebrain.md` — north star (§1), where it runs (§2), roster +
  cost model (§3), architecture (§4), roster config (§5), routing logic LOCKED (§6),
  auth/safety (§7), chunk outline (§10), C0 findings (§11).
- **Contract:** `TANGLEBRAIN.md` — **FROZEN / SUPERSEDED** (banner at top). It predates the
  cost-tier pivot; the plan is canon. Do not build against it.

## Project home & roles

- TangleBrain runs on **Cursatory (this Mac)** — where the OAuth subs are logged in.
- Dependency: **TangleBrain → Monad (one-way)**. Monad never depends on TangleBrain.
- **Cross-session:** the **Monad-1 repo/session is the PM/coordinator**; TangleBrain sessions
  are **builders**. Do NOT write to or commit in the Monad-1 repo from here. Suggestions to
  the PM go via paste-back blocks. Shared infra (TC ports, group docs) is editable by either.

## Status (as of 2026-06-16)

- ✅ **C0** — frontier-first decompose spike. Shipped as Monad-1 PR #65 (merged). Verdict KEEP.
  Carry-forward: budget local/grunt calls generously (≥2048 tokens) — gpt-oss spends budget on
  internal reasoning; chain-of-thought returns in `reasoning_content` and is dropped.
- ✅ **C1** *(this repo, this session)* — package skeleton + roster loader (§5) + openai-compat
  adapter to local gpt-oss + one request end-to-end. **PR #1 open** on
  `feat/c1-skeleton-roster-adapter`; 50 hermetic tests green; live E2E passing; Independent
  Critic review done + findings addressed. **Repo is PRIVATE on a Free plan → auto-merge is
  Pro-gated; merge PRs MANUALLY** (`gh pr merge <N> --squash --delete-branch`), never `--auto`.
  Migrated plan + contract into this repo; re-pointed the TANGLEBRAIN / TANGLEBRAIN-PLAN
  shared-doc registrations to the in-repo copies (LITELLM stays on Monad-1).

## Next chunk = C2

CLI adapters for the three subs (claude / codex / gemini) with **env-scrub** (§7): `claude -p`
must run with **`ANTHROPIC_API_KEY` scrubbed** from the subprocess env (108-char paid key is
set on this Mac — we want the flat Max sub, not per-token billing). `scrub_env` is already a
first-class field on the roster `Invoke` object, ready to be honored by the CLI adapter.
Plus the gpt-oss MCP tool from C0 as each orchestrator's local delegate. (C3 = the real
router/rotation/failover — NOT C2.)

**Also fold into C2 (PM, 2026-06-16):** the full contract `TANGLEBRAIN.md` §2/§6 reconciliation —
rewrite the Monad-embedded framing (§2 → runs on the Mac) and the `direct/smart-fallback/
semantic-route` profile model (§6 → cost-tiered frontier-first). Invariant #3 is already reconciled
(this session); §2/§6 are what remain frozen.

## Two formerly-open decisions — RESOLVED 2026-06-16 (PM)

Both parked decisions are now ratified (plan §9.6–9.7, contract invariant #3, **issue #2**):

1. **Paid-API billing gate** → explicit `api_billing_enabled` flag, **default off**. When off,
   `tier: api` entries parse but never route. When on, each paid key is a `tier: api` roster entry
   with a per-key `enabled` toggle + budget cap; paid API stays last-resort (§6).
2. **Paid-key custody** → **LiteLLM-fronting preferred**: TangleBrain references a scoped LiteLLM
   virtual key (existing `key_ref`), raw provider key lives in LiteLLM on Monad. Holding raw keys
   directly is **not foreclosed** (the PM wants the feature available — cheap keys later, or other
   operators) but stays behind the toggle. Invariant #3 **softened, not reversed** — the durable
   rule is *no paid billing without the explicit `api_billing_enabled` toggle*.

The paid-API tier itself is unbuilt (issue #2, a later chunk). The broader contract §2/§6
architecture reconciliation (Monad-embedded → Mac; profile model → cost-tier) is **deferred to C2**
(PM, 2026-06-16) — fold it into the next chunk's work.

## Key facts (don't re-derive)

- Free-local endpoint: LiteLLM `http://monad-1.tail123678.ts.net:4000/v1`, model `gpt-oss-120b`.
  Scoped key at `~/.config/monad/tanglebrain-spike.key` (0600). The adapter calls LiteLLM
  DIRECTLY (not via the C0 MCP server). Key referenced via roster `key_ref: file:...`, never
  embedded/committed (`*.key` is gitignored).
- Conventions mirror Monad-1: stdlib `unittest` + mock, venv-based test target, `make lint/test`.
- LangGraph is DEFERRED (plan §9 decision 2) — plain Python until the loop justifies it.
