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
  - Live-observed rotation: claude→codex→gemini→claude. (Default flipped to router in C3b below.)
- ✅ **C3b** *(this session — 4th chunk)* — **delegate-injection + default flip**. **Merged
  (PR #9); issue #7 CLOSED.** The frontier-first system is now complete end-to-end. Key facts:
  - **CLI default is now the frontier-first router**: `tanglebrain "…"` routes; `--local` forces
    the direct gpt-oss tier; `--model <id>` pins an entry. Precedence in `run_once`: `model` >
    `local` > router. `--route` is now a no-op (back-compat).
  - **Config-driven delegate injection**: roster field `invoke.delegate_args` per orchestrator;
    `CliAdapter` appends them (substituting `{delegate_mcp_json}` / `{delegate_mcp_command}` via
    `delegate.delegate_substitutions()`) when `inject_delegate=True`; `Router` sets that for
    orchestrators. Delegate runs as `python -m tanglebrain.mcp_server` (no PATH assumptions).
    env-scrub unaffected (claude still strips `ANTHROPIC_API_KEY` while delegating).
  - **All three verified live delegating to gpt-oss**: claude (`--mcp-config`+`--allowedTools`),
    codex (`-c mcp_servers…` + `--dangerously-bypass-approvals-and-sandbox` — needed or codex
    cancels the tool call headless), gemini (needs one-time `gemini mcp add tanglebrain-delegate
    -- <py> -m tanglebrain.mcp_server`, then `--allowed-mcp-server-names`+`--approval-mode yolo`).

- ✅ **C4** *(this session — 5th chunk)* — **measurement / "spend avoided" rollup (§8)**. **Merged
  (PR #11); issue #10 CLOSED.** No default-behavior change. Key facts:
  - `tanglebrain/measurement.py`: each routed task is logged as one JSON line in an append-only
    `~/.cache/tanglebrain/usage.jsonl` (honors `TANGLEBRAIN_STATE_DIR`, same dir as router-state);
    `tanglebrain --stats` reads + rolls up a spend-avoided figure. `Router.last_served` surfaces the
    served entry so the CLI metering seam (`run_once`, all 3 paths) tags tier/model without changing
    `route()`'s `str` return. **All I/O fault-tolerant** — logging never breaks routing, corrupt log
    lines never break the rollup.
  - **Tokens are ESTIMATED**, not measured: uniform `chars/4` heuristic over visible prompt+response
    across ALL tiers (CLI subs expose no usable counts; local `usage` is inflated by dropped gpt-oss
    reasoning tokens). One consistent approximate methodology; no adapter changes.
  - **Pricing** = `tanglebrain/config/pricing.yaml`, mirrors monad-stats `costSaved` anchor
    **verbatim: Claude Sonnet $3/$15 per MTok** (Monad-1 `tools/monad-stats/publish.py` +
    `models.json`, methodology ratified 2026-06-13). `placeholder:false`; rollup shows a PLACEHOLDER
    caveat only if the anchor is forked. C5's knob GUI will tune this config.
  - **Scope**: top-level routed tasks only. Delegate sub-calls (`run_local_delegate`) intentionally
    NOT metered (they run inside an already-counted sub task → would double-count).
  - Plan-hygiene: shipped C2/C2b/C3/C3b plans archived to `.claude/plans/archive/` (commit fab2eea).
    NB: plans are git-TRACKED in this clone (the old "plans are gitignored on TC" note is wrong here).

- ✅ **C5a** *(this session — 6th chunk)* — **knob GUI, read-only slice (§10 C5)**. **Merged
  (PR #14); issue #12 CLOSED.** Split C5 into read-only (C5a, shipped) + editable (C5b, deferred).
  Key facts:
  - New `tanglebrain/gui/` package + `tanglebrain-gui` console script. **Stdlib `http.server` + one
    vanilla HTML/CSS/JS file (`gui/static/index.html`), ZERO new runtime deps** (no `[gui]` extra),
    TangleClaw dark/lime aesthetic. Binds **127.0.0.1 ONLY, not configurable** (no `--host` — panel
    is unauthenticated, runs prompts = real sub quota, reads roster → must not be exposed).
  - `gui/views.py` = pure testable functions (`view_roster/pricing/stats`, `run_prompt`) reusing
    `load_roster`/`load_pricing`/`read_records`+`rollup`/`run_once`. `gui/server.py` = pure
    `dispatch(method,path,body)->(status,ctype,body)` (socket-free tests) + `ThreadingHTTPServer`.
  - **Secret-safety (tested)**: roster view emits `key_ref` as the reference string only — never
    resolved, no key file opened (a test patches `open` to fail if touched). Read-view errors → clean
    JSON 500, not tracebacks. XSS-escaped output in the page.
  - **Port 3250** leased PERMANENT in PortHub for project TangleBrain. NB: live PortHub API is the
    CLAUDE.md-documented `/api/ports/lease` on `https://localhost:3102` (`-k`); the `/api/leases`
    shape in the PortHub *source* is NOT what the running daemon serves — use `/api/ports*`.
  - `run_prompt` shows the served tier by reading the last C4 usage record (`_last_served`) — a
    documented best-effort, single-user race under threading (two concurrent runs could swap). Proper
    fix = have `run_once` return the served entry; folded into C5b.

## Next chunk = C5b — editable knobs (§10 C5, issue #13)

**C5b** (issue **#13**) = make the C5a panel **edit** config and persist: write-back to `roster.yaml`
/ `pricing.yaml` (new `save_roster`/`save_pricing` — no round-trip serializer exists yet),
**re-validate on save** (reuse `_parse_entry`/`_parse_invoke` checks), file-write safety
(atomic/backup) + a confirm UI, and **never** accept/echo resolved `key_ref` secrets. Also fold in
the `_last_served` race fix (run_once returns served entry). Builds on `tanglebrain/gui/`. Stays
localhost-only. Plan-first. (Still open & deferred after C5b: **issue #2** paid-API tier + contract
§2/§6 reconciliation — the final build chunk.)

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
