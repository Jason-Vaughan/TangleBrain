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
- ✅ **C1** — package skeleton + roster loader (§5) + openai-compat adapter to local gpt-oss +
  one request end-to-end. **Merged (PR #1); released v0.1.0.** **Repo is PRIVATE on a Free plan →
  auto-merge is Pro-gated; merge PRs MANUALLY** (`gh pr merge <N> --squash --delete-branch`),
  never `--auto`. (No CI checks configured on the repo, so no gates to wait on.) Migrated plan +
  contract into this repo; re-pointed TANGLEBRAIN / TANGLEBRAIN-PLAN shared-doc registrations to
  the in-repo copies (LITELLM stays on Monad-1).
- ✅ **C2** *(this session)* — CLI adapters for the three subs (claude/codex/gemini) with
  **env-scrub** (§7). **PR #5 open** on `feat/c2-cli-adapters`; 99 hermetic + 6 gated live tests
  green; Independent Critic review done + findings addressed. **Merge manually after review**
  (feature PR → not `--auto`). Key facts for future sessions:
  - `CliAdapter` (`tanglebrain/adapters/cli.py`): subprocess, **no shell, ever**. Prompt injected
    via a `{prompt}` token in the roster `cmd` (substituted) else appended as the final arg.
  - **Verified CLI shapes** (probed live 2026-06-16): claude `-p --output-format json` →
    `{"result":...,"is_error":...}`; gemini `-p {prompt} --output-format json` → `{"response":...}`;
    codex `exec` → plain text on stdout (metadata on stderr). New roster field `invoke.parse` ∈
    {`claude-json`, `gemini-json`, `plain`} picks the parser. claude `cmd` is now `json`, NOT
    `stream-json`.
  - Env-scrub proven: a live test has claude run `printenv ANTHROPIC_API_KEY` → reports UNSET.
  - `AdapterError` now lives in `adapters/base.py` (re-exported from `openai_compat`).
  - `tanglebrain --model <id>` / `selector.select_by_id` drive a named entry end-to-end — an
    explicit override, NOT the router.

## Next chunk = C3 (with C2b folded in)

- **C2b (issue #4)** — port the C0 gpt-oss **MCP local-delegate** here as each orchestrator's
  local delegate. Split out of C2 (one-chunk rule); only has value once an orchestrator
  decomposes+delegates, so fold it in **near/with C3**.
- **C3** — the real **frontier-first router (§6)**: task-fit orchestrator selection + rotation
  across the `can_orchestrate` subs, 429/limit failover, and the decompose→delegate→review loop.
  The selector today is deliberately minimal (`select_local` / `select_by_id`) — build the router
  as its own module; do not grow the selector into it.

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
architecture reconciliation (Monad-embedded → Mac; profile model → cost-tier) is **bundled with
the paid-tier chunk (issue #2)** (PM, 2026-06-16) — not C2.

## Key facts (don't re-derive)

- Free-local endpoint: LiteLLM `http://monad-1.tail123678.ts.net:4000/v1`, model `gpt-oss-120b`.
  Scoped key at `~/.config/monad/tanglebrain-spike.key` (0600). The adapter calls LiteLLM
  DIRECTLY (not via the C0 MCP server). Key referenced via roster `key_ref: file:...`, never
  embedded/committed (`*.key` is gitignored).
- Conventions mirror Monad-1: stdlib `unittest` + mock, venv-based test target, `make lint/test`.
- LangGraph is DEFERRED (plan §9 decision 2) — plain Python until the loop justifies it.
