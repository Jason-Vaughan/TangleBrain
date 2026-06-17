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

## Status (as of 2026-06-17)

> **Session 2026-06-16/17 (this builder session) — PROJECT REACHED FEATURE-COMPLETE.** Planned #2
> (paid-API tier) then ran straight through the rest of the backlog (PM-directed, momentum protocol-
> breaks): C6a→C6b→C6c (paid tier, closed #2), GUI roster editing, the #17 version-drift fix, and the
> §6 classifier gate. Releases **v0.6.0 → v0.9.0** (each CHANGELOG-driven + GitHub release; every
> feature via branch→PR→independent Critic review). **All planned work shipped** — see the "ALL PLANNED
> WORK SHIPPED" section near the bottom for the (non-buildable) remainder and the next-session entry point.

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

- ✅ **C5b** *(this session — 7th chunk)* — **editable pricing knob + served-entry fix (§10 C5)**.
  **Merged (PR #15); issue #13 CLOSED.** Released **v0.5.0**. **Scope = PRICING ONLY** (PM decision —
  roster editing deferred; its dense inline comments need a comment-preserving editor). Key facts:
  - `measurement.validate_pricing()` (strict: rejects non-numeric/negative/NaN/inf rates,
    bool-as-rate, empty model, non-bool placeholder) + `save_pricing()`: **atomic** (temp +
    `os.replace`), **backup** to `<state_dir>/backups/` (sub-second stamp), and **preserves the
    target file's existing leading comment block VERBATIM** (`_leading_comment_block`) — fallback
    `PRICING_HEADER` only when the file is absent. `_render_pricing` uses `json.dumps` for the model
    string (valid YAML; round-trips colons/quotes/unicode/newline/backslash).
  - **`cli.run_once(return_served=True)` → `(text, served={path,tier,model})`** (default still bare
    `str`, callers unchanged). Panel uses it → no usage-log re-read → **C5a `_last_served` race gone**
    (`_last_served` deleted). `gui` POST `/api/pricing` → `save_pricing_view`.
  - GUI edits write the **tracked** `tanglebrain/config/pricing.yaml` (git-visible; operator commits).
  - Critic caught + fixed: a real **self-XSS** (editable field put `reference_model` in an HTML
    `value="…"` attr but `esc()` only escaped `&<>` — now full 5-char escape incl quotes); header
    constant had drifted (now preserve-existing-verbatim); adversarial round-trip test added.

- ✅ **C6a** *(this session — 8th chunk; planning + build in one session, PM-authorized override of
  the "clean session" note since context was 91% free)* — **paid-API tier scaffolding, off by default
  (§10 C6, issue #2 slice 1/3)**. **Merged (PR #16; squash `6cb4144`).** Issue #2 stays OPEN
  (C6b/C6c remain). Releases as **v0.6.0** on next bump (CHANGELOG `### Added` → minor). Key facts:
  - **Gate**: new `tanglebrain/settings.py` + `config/settings.yaml`, `Settings.api_billing_enabled`
    **default False**. `load_settings`: missing file → defaults (gate off); malformed → `SettingsError`
    (never a coincidental enable); **non-bool rejected** (a stray `1`/`"true"` can't enable billing;
    bare `yes/no/on/off` ARE YAML bools and legitimately accepted). Packaged settings.yaml ships off.
  - **`api` adapter** = `tanglebrain/adapters/api.py` `ApiAdapter(OpenAICompatAdapter)` — paid APIs are
    LiteLLM-fronted (OpenAI-compat), so it **reuses the openai-compat transport**; `from_entry` only
    checks kind. Same transport, different *policy*. `key_ref` → scoped LiteLLM **virtual key**,
    resolved lazily on `run` (never at construction; test patches a missing key file to prove it).
  - **Roster** (`roster.py`): `_parse_invoke` `api` branch requires `base_url`+`model`+`key_ref`. New
    `RosterEntry` fields `enabled: bool = True` (per-key kill-switch) + `budget_usd_month: float|None`
    (must be >0; **display-only in v1**, LiteLLM enforces the hard cap — PM decision). Both parsed
    universally but only enforced for api in C6a.
  - **Gate enforcement** in `selector.build_adapter(entry, inject_delegate, settings=None)`: `api`
    branch builds `ApiAdapter` ONLY if `settings.api_billing_enabled` AND `entry.enabled`, else
    `AdapterError`. Default-loads settings only when an api entry is actually built (no file read for
    local/cli). All existing call sites (cli `--model`/`--local`, router) unchanged — settings defaults.
  - **"Parse but never route" verified unbypassable** by the Critic across `--model`/`--local`/router.
    `measurement.record_task` already zeroes `spend_avoided` for `tier=="api"` (pre-existing, untouched).
  - **Contract §2/§6 + invariant #3 reconciliation** (the bundled docs task) was **ALREADY DONE** in
    `TANGLEBRAIN.md` (banner + inline, 2026-06-16) → no edit needed this session.
  - 250 hermetic tests pass (was 249). New: `test_settings.py`, `test_api_adapter.py`, api roster tests,
    gated selector matrix, a CLI-boundary test (`--model <api>` inert + meters nothing, gate off).

- 🔄 **C6b** *(this session — 9th chunk; 2nd build chunk this session — protocol break for momentum,
  PM-authorized "continue now")* — **last-resort paid-API routing (§10 C6, issue #2 slice 2/3)**.
  **Merged (PR #18); released in v0.7.0.** Key facts:
  - `Router.route()`: after ALL orchestrators fail, if `settings.api_billing_enabled`, try enabled
    `tier: api` entries in **roster order**. Paid success → `last_served` set (meters tier=api,
    spend_avoided=0) but **cursor NOT advanced** (api isn't in the §6 rotation). Paid failures fail
    over + are listed in `RouterError` with the same `[rate-limit]` annotation.
  - **In-session design decision**: gate lives in the Router (`self.settings`, injectable; defaults
    to packaged settings.yaml) so the api block is only *attempted* when billing on → clean failover
    log. `build_adapter` still independently gates the `--model` path (defense in depth; same file in
    prod so no divergence — Critic-verified). **Requires ≥1 orchestrator** (never paid-routes a
    no-subs roster; `--model` is the explicit paid path). **Skips already-attempted ids** so a
    degenerate `api`+`can_orchestrate` entry isn't double-run.
  - 260 hermetic tests (was 259→260 after Critic nits). New `LastResortApiFallbackTest` (12 cases).
    Critic: no blockers; 2 nits applied (double-attempt guard + seeded-cursor test).

- ✅ **C6c** *(this session — 10th chunk; 3rd build chunk this session — protocol break for momentum)* —
  **paid-API visibility in the knob panel + runbook (§10 C6, issue #2 slice 3/3, the closer)**. **Merged
  (PR #19); released in v0.7.0. CLOSES issue #2.** Thin/read-only per the LiteLLM-only-v1 decision. Key facts:
  - `gui/views.py`: `view_roster` now emits per-entry `enabled` + `budget_usd_month` (no new file read →
    the open-patched secret-safety tests still hold). New `view_settings()` → `{api_billing_enabled}`
    reading only `config/settings.yaml`. `gui/server.py`: `GET /api/settings`.
  - `gui/static/index.html`: roster card shows a `disabled` pill, a `budget: $N/mo (LiteLLM-enforced)`
    note, and a **Paid-API billing: ON/OFF** banner (from the gate). All values esc()'d; settings fetch
    `.catch`-degrades to OFF. README gained a **runbook** (mint budget-scoped LiteLLM virtual key →
    store 0600 → `key_ref` → flip gate → pause via `enabled:false`/gate).
  - 265 tests. Critic: SHIP, no blockers (secret-safety/XSS/read-only all traced).
  - ⚠️ **LESSON**: issue #2 was auto-closed prematurely earlier by the *C6b memory commit* `fe6b5b3`
    whose prose body contained the literal `closes #2` (GitHub treats it as a keyword anywhere on the
    default branch). Reopened, then properly closed by PR #19. **Never put `close(s)/fix(es)/resolve(s)
    #N` in commit-message prose — only in PR bodies where the close is intended.**

- ✅ **C7 (post-#2 deferred item)** *(this session — 11th chunk; 4th build chunk this session)* —
  **editable roster in the knob panel (plan §5/§9.2; the deferred half of C5)**. **Merged (PR #21);
  released in v0.8.0.** Closed the last GUI gap. Key facts:
  - **PM decision**: no-dep **surgical** comment-preserving edit (NOT ruamel.yaml — kept the GUI's
    zero-new-runtime-dep stance). New `tanglebrain/roster_edit.py` edits the targeted value on the
    targeted line in place → all comments, the nested `invoke` block, the commented api example
    survive byte-for-byte. Editable fields ONLY: `enabled`, `can_orchestrate`, `budget_usd_month`
    (cleared→line removed), `good_at`. NO add/remove/reorder entries, NO invoke editing (still hand-edits).
  - Write-safety mirrors C5b: validate → **re-parse candidate with real `load_roster` before any write**
    (surgical slip can't land a malformed roster) → backup `<state_dir>/backups/roster-<ts>.yaml` →
    atomic. `views.save_roster_view` + `POST /api/roster`; panel sends only changed fields, confirms.
  - Insert-absent-field lands at **block END** (after all the entry's lines) so it never splices a
    block-style key from its continuation (Critic should-fix). Non-finite budget rejected (nit).
  - 295 tests. Critic: ship-ready, no blockers. `roster_edit._atomic_write`/`_backup_dir` reused from
    `measurement` (package-internal).

- ✅ **Classifier gate** *(this session — 12th chunk; 5th build chunk this session)* — **local
  classifier gate (plan §6 evolution), OFF by default**. **Merged (PR #22); released in v0.9.0.** Built
  **ahead of the §8 data trigger by PM direction** (no rate-limit data yet) → ships off, no behavior
  change until enabled. Key facts:
  - `tanglebrain/classifier.py` `classify(prompt)→"trivial"|"frontier"` via the local openai-compat
    adapter. Rates TASK complexity (not "can the model do it"). **FAILS SAFE to frontier** on any
    error/ambiguity. **Strict parse**: TRIVIAL only when the FIRST word token is exactly `trivial` and
    `frontier` is absent (so prose/negation like "not trivial" → frontier — Critic-hardened).
    `CLASSIFY_MAX_TOKENS=1024` (gpt-oss reasoning headroom; truncation just fails safe to frontier).
  - `settings.classifier_gate_enabled` (default false; shared `_bool` validator with the billing gate).
    `cli.run_once(gate=None|bool)`: default path only; gate-on+trivial → free local (`path=gate-local`),
    else router. `--gate`/`--no-gate` override the setting. `--model`/`--local` bypass the gate.
  - 316 tests. Critic: ship-able, no blockers (2 should-fix applied).

## ALL PLANNED WORK SHIPPED — project complete; no active next chunk

**As of 2026-06-17: the entire §10 build outline (C0→C7), the deferred C5 GUI roster-editing item, AND
the §6 classifier-gate evolution are all shipped.** Latest release **v0.9.0**. The cost-tiered router is
end-to-end: optional local classifier gate → free-local-first → frontier-first orchestrator rotation
with delegate offload → measurement/rollup → knob GUI (view + **editable** pricing & roster) → paid-API
last resort (off by default, gated, last-resort-routed, visible). **No canonical next build chunk; no
open feature issues.**

**Remaining = NOT autonomously buildable:**
- **Real paid-key trial — DECIDED SKIP (2026-06-17, PM).** The paid tier is hermetically tested but
  **never run against a real paid endpoint — by design (anti-key stance)**; the hooks are in, live path
  unverified. Filed as **issue #23** for a future operator (mint a budget-scoped LiteLLM virtual key on
  Monad, run the live round-trip, fix any provider-shape deltas). README has a "Live status" caveat +
  the runbook. Do NOT attempt autonomously (Monad-side, spends real money, cross-session boundary).
- **Future tuning** — only on explicit PM direction or §8 data (e.g. turn the classifier gate on if
  rotation stops keeping under sub rate limits; surface `classifier_gate_enabled` in the GUI if wanted).

### Original #2 plan-time context (kept for reference)
**PLANNED 2026-06-16 → split into C6a/C6b/C6c.** Canonical build plan:
`/Users/jasonvaughan/Documents/Projects/TangleBrain/.claude/plans/c6-paid-api-tier.md`.

**Issue #2** = the paid-API tier: explicit `api_billing_enabled` global flag **default OFF** (when off,
`tier: api` entries parse but never route); when on, each paid key is a `tier: api` roster entry with
per-key `enabled` toggle + budget cap; paid API stays LAST resort (§6). **Bundled with the contract
§2/§6 reconciliation** (Monad-embedded→Mac, profile-model→cost-tier — PM, see below). Custody:
**LiteLLM-fronted virtual key preferred** (`key_ref`), raw keys not foreclosed but gated by the flag
(invariant #3 softened — durable rule: *no paid billing without the explicit toggle*).

**Two PM decisions ratified 2026-06-16 (in the plan):**
1. **Budget enforcement = LiteLLM-only for v1.** TB stores/displays `budget_usd_month` but does NOT
   meter+block on it; the budget-scoped LiteLLM virtual key is the hard cap. → C6c is a thin
   docs+display step, not TB-side spend metering.
2. **C6a (next build session) = gate + `api` adapter + inert parse ONLY** — never routable.

**Discovery facts (seams already cut):** `api` is in `VALID_KINDS`/`VALID_TIERS` and `tier: api` already
parses; `build_adapter` has the explicit api-tier gap (`AdapterError ... issue #2`) +
`test_api_entry_has_no_adapter_yet` asserts it (flips when built); `measurement.record_task` already sets
`spend_avoided=0` for `tier=="api"`; `resolve_key_ref` is the custody primitive. **Insight:** LiteLLM is
OpenAI-compat → `api` adapter reuses the openai-compat transport (don't duplicate httpx). **No global-config
concept exists** → plan adds `tanglebrain/settings.py` + `config/settings.yaml` for the flag (roster stays a
bare list; folding the flag into roster would be a breaking list→mapping parse change).

Also still open/non-build: **roster editing in the GUI** (deferred from C5; needs comment-preserving YAML).

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
