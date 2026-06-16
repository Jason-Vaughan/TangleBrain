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
  **env-scrub** (§7). **Merged (PR #5).** 99 hermetic + 6 gated live tests; Critic review done.
  Key facts for future sessions:
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
- ✅ **C2b** *(this session — built right after C2, protocol break for momentum)* — gpt-oss
  **MCP local-delegate**. **Merged (PR #6); issue #4 CLOSED.** Key facts:
  - `tanglebrain-delegate` console script → `tanglebrain/mcp_server.py` (`FastMCP`, **sync** tool
    `delegate_local(prompt, max_tokens?)`). Routing logic is `tanglebrain/delegate.py`
    `run_local_delegate(...)`, which **reuses** `select_local` + `OpenAICompatAdapter` (no
    duplicated endpoint/key — generalizes C0, which hardcoded them).
  - `mcp` is an **optional extra**: `pip install "tanglebrain[delegate]"` (Makefile `venv` now
    installs `.[delegate]`). No core module imports `mcp` (verified). `TANGLEBRAIN_ROSTER` env
    points the server at a non-default roster.
  - Proven end-to-end over **real MCP stdio**: client spawns server, calls `delegate_local`, gets
    gpt-oss text. README documents per-CLI registration (`claude/gemini mcp add ...`).
- ✅ **C3** *(this session — 3rd chunk, protocol break for momentum)* — **frontier-first router
  control plane**. **Merged (PR #8).** Key facts:
  - `tanglebrain/router.py` `Router.route(prompt, task=None, opts=None)`: task-fit selection
    (prefer orchestrators whose `good_at` has `task`, else all), round-robin **rotation** across
    `can_orchestrate` subs, **failover** on `AdapterError` → `RouterError` if all fail (rate-limit
    ones annotated `[rate-limit]`). Reuses `build_adapter`; the selector stayed minimal.
  - Rotation cursor **persisted across processes** at `~/.cache/tanglebrain/router-state.json`
    (override `TANGLEBRAIN_STATE_DIR`); tracks the served orchestrator's position in the FULL
    list (not the task-filtered sublist); only advances on success; missing/corrupt/negative → 0.
    Writes are non-atomic on purpose (cursor is a load-spread hint).
  - Exposed via `tanglebrain --route [--task]`. **CLI default is still local-first** (precedence:
    `--model` > `--route` > local). Live-observed rotation: claude→codex→gemini→claude.

## Next chunk = C3b (issue #7) — make frontier-first the default

The router (C3) and the delegate (C2b) both exist; C3b connects them and flips the default:
- **Inject `delegate_local` into orchestrator invocations** so a sub offloads grunt to local
  mid-task: pass the MCP server per-invocation (claude `--mcp-config`, codex/gemini equivalents)
  or rely on documented pre-registration. This is the per-CLI MCP-config plumbing — verify each
  CLI's actual flag (varies by version).
- **Flip the CLI default** from local-first to `--route` (keep `--local`/`--model` overrides).
- Then the decompose→delegate→review behavior is largely emergent from the orchestrator having
  the tool (§6); verify live. (§9 deferred LangGraph — revisit only if the loop justifies it.)
- Changes default behavior + touches MCP-config mechanics → **plan-first**; a fresh session is
  reasonable. Selector stays minimal; don't grow it into the router.

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
