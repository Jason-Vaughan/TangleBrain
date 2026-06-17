# Changelog

All notable changes to TangleBrain are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **C6b — last-resort paid-API routing.** The frontier-first router can now fall through to a paid
  `tier: api` entry as a genuine last resort (plan §6): **only** after *every* `can_orchestrate` sub
  has failed/exhausted, and **only** when the `api_billing_enabled` gate is on. With the gate off
  (the default) the router never reaches a paid tier — behavior is unchanged. Part of #2.
  - Enabled `api` entries are tried in roster order; a paid success is surfaced on
    `Router.last_served` (so the run is metered `tier=api`, `spend_avoided=0`) but does **not**
    advance the orchestrator rotation cursor. Paid failures fail over to the next `api` entry and
    are listed in the `RouterError` with the same `[rate-limit]` annotation as orchestrators.
  - The router requires at least one orchestrator to be present — it never paid-routes a roster with
    no subs to exhaust (use `--model <id>` for an explicit paid call). `Router(... settings=)` is
    injectable; it defaults to the packaged `config/settings.yaml`.

## [0.6.0] - 2026-06-16

### Added

- **C6a — paid-API tier scaffolding (off by default).** A new `api` adapter and the global billing
  gate that guards it. A `tier: api` roster entry now parses fully but is **never routable** until
  it is explicitly enabled — preserving today's safe, zero-paid-spend default (issue #2).
  - **The gate**: new `tanglebrain/settings.py` + `config/settings.yaml` with `api_billing_enabled`
    (**default `false`**). A missing settings file defaults the gate *off*; a malformed one is a hard
    error (never a coincidental enable). `selector.build_adapter` builds an `api` entry only when the
    global gate **and** the entry's own `enabled` flag are both on, else raises clearly.
  - **The adapter**: `tanglebrain/adapters/api.py` `ApiAdapter` — paid APIs are LiteLLM-fronted, so
    it reuses the OpenAI-compat transport and references a scoped LiteLLM **virtual key** via
    `key_ref` (never a raw provider key, resolved lazily at call time).
  - **Roster fields**: `api` invoke now requires `base_url` + `model` + `key_ref`; new per-entry
    `enabled` (kill-switch, default `true`) and `budget_usd_month` (display-only in v1 — the hard cap
    is enforced LiteLLM-side on the virtual key). A commented example entry ships in `roster.yaml`.
  - Last-resort routing (wiring `api` into the router) is **not** in this change — that is C6b.

## [0.5.0] - 2026-06-16

### Added

- **C5b — editable pricing in the knob panel.** The panel's pricing card is now editable: change the
  input/output $/MTok, the reference-model label, and the placeholder flag, then **Save** to persist
  to `tanglebrain/config/pricing.yaml`. Closes #13.
  - **Write-safety**: strict validation before any write (rejects non-numeric/negative rates and an
    empty reference model — nothing is persisted on a bad value); the file is written **atomically**
    (temp + `os.replace`) and the prior version is **backed up** to `<state_dir>/backups/` first.
  - **Comment-preserving**: the canonical methodology header is re-emitted on every save, so GUI/
    programmatic edits never strip it — no new dependency. (Roster editing stays out — its dense
    inline comments need a comment-preserving mechanism, deferred to a later chunk.)
  - New `measurement.validate_pricing()` / `save_pricing()`; the panel writes the tracked repo config
    so an edit is git-visible and committed by the operator.

### Changed

- `cli.run_once()` gained an optional `return_served=True` that also returns the served
  `{path, tier, model}`. The knob panel uses it to report which tier handled a run **without
  re-reading the usage log** — removing the C5a best-effort race. Default behavior (returns a bare
  string) is unchanged.

## [0.4.0] - 2026-06-16

### Added

- **C5a — knob GUI (read-only panel), TangleClaw-style.** A new `tanglebrain-gui` console script
  serves a thin, **localhost-only** web panel (stdlib `http.server` + a single vanilla HTML/CSS/JS
  page — zero new runtime dependencies) on port 3250 (registered in TangleClaw PortHub). The panel:
  views the live roster (§5), the pricing reference, and the local C4 spend-avoided rollup, and runs
  a prompt through the router (prompt in → final out), showing which tier/model served it (read from
  the C4 usage log; panel runs are metered automatically). First slice of plan §10's "C5 — Knob GUI".
  - **Read-only this chunk** — config editing (write-back to YAML) is deferred to C5b (#13).
  - **Secret-safety**: the roster view emits `key_ref` as the stored reference string only; it is
    never resolved and no key file is read, so no secret material reaches the browser.
  - Binds `127.0.0.1` only — the panel spends real sub rate-limit quota when it runs prompts and
    reads the roster, so it must not be network-exposed. New `tanglebrain/gui/` package; HTTP routing
    is a pure `dispatch()` over testable view functions (`tanglebrain/gui/views.py`).

## [0.3.0] - 2026-06-16

### Added

- **C4 — measurement / "spend avoided" rollup (plan §8).** Every routed task is now logged as one
  JSON line in an append-only usage log (`~/.cache/tanglebrain/usage.jsonl`, honoring
  `TANGLEBRAIN_STATE_DIR`): the execution path, tier, model, estimated tokens, and the
  cloud-equivalent cost it avoided. `tanglebrain --stats` rolls those records up into a
  "spend avoided" figure — what the work would have cost on a paid frontier API. Closes #10.
  - **Uniform token estimation**: CLI subs expose no usable token counts, so tokens are estimated
    with a single `chars/4` heuristic over the visible prompt + response, applied identically to
    every tier — one consistent (if approximate) methodology. No adapter or routing behavior change.
  - **Config-driven pricing** (`tanglebrain/config/pricing.yaml`) carrying coordinator's `usage-stats`
    `costSaved` anchor — Claude Sonnet at $3/$15 per MTok (methodology ratified 2026-06-13) — so the
    two projects value avoided spend identically. A `placeholder` flag (false by default) makes the
    rollup render a PLACEHOLDER caveat if the anchor is ever forked before re-ratifying.
  - **New module** `tanglebrain/measurement.py`; the router now exposes `Router.last_served` so the
    CLI metering seam can record which tier handled each task. All measurement I/O is
    fault-tolerant — a logging failure never affects the returned answer, and a corrupt log line
    never breaks the rollup.
  - **Scope**: meters top-level routed tasks only (the three `run_once` paths). The gpt-oss MCP
    delegate's sub-calls are intentionally not metered (they run inside an already-counted sub task).

## [0.2.0] - 2026-06-16

### Changed

- **C3b — frontier-first is now the default, and orchestrators offload grunt to free local
  (BEHAVIOR CHANGE).** `tanglebrain "prompt"` (no flags) now routes through the frontier-first
  router instead of going straight to the local tier; pass `--local` for the old direct-to-gpt-oss
  behavior, or `--model <id>` to pin an entry. Each orchestrator is now invoked with the C2b
  `delegate_local` tool available, so it decomposes the task and offloads grunt to free local
  gpt-oss at $0 — this is what makes frontier-first cost-effective (plan §6). Closes #7.
  - **Config-driven injection**: a new `invoke.delegate_args` roster field carries the per-CLI
    flags that register + allow the delegate, with `{delegate_mcp_json}` / `{delegate_mcp_command}`
    tokens substituted at runtime (the delegate runs as `python -m tanglebrain.mcp_server`, so it
    resolves without PATH assumptions). Adding/adjusting a CLI is a config edit (§5).
  - **Verified live, all three orchestrators delegate to gpt-oss**: claude (`--mcp-config` +
    `--allowedTools`, API key scrubbed), codex (`-c mcp_servers…` + approval bypass), gemini
    (after a one-time `gemini mcp add` + `--approval-mode yolo`). See the README for gemini's setup.

### Added

- **C3 — frontier-first router (control plane).** `tanglebrain/router.py` routes a task to a
  frontier sub acting as orchestrator, rotating the role across the `can_orchestrate` subs for ~3×
  the rate-limit runway, with automatic failover (plan §6).
  - **Task-fit selection**: a `--task <good_at-tag>` hint prefers orchestrators good at it (falling
    back to all when none match — a preference, not a gate). Auto-classification stays deferred
    (§6: "only if volume demands").
  - **Rotation**: round-robin across orchestrators, with the cursor **persisted across processes**
    (`~/.cache/tanglebrain/router-state.json`, override via `TANGLEBRAIN_STATE_DIR`) so successive
    `tanglebrain` invocations actually spread load. Missing/corrupt state resets to 0, never crashes.
  - **Failover**: on an `AdapterError` from one orchestrator, advance to the next; if all fail,
    raise `RouterError` naming each failure (rate-limit-looking ones are annotated `[rate-limit]`).
  - Exposed via `tanglebrain --route [--task <kind>]`. **The CLI default stays local-first** — the
    router becomes the default in C3b (#7), once the local-delegate is wired into orchestrator runs
    (routing whole tasks to subs without local offload would burn rate limits for no cost benefit).
  - Lives in its own module; the C1 selector stays minimal. Rotation/failover are proven by the
    hermetic suite (round-robin, wraparound, failover, persisted cursor); a gated live test
    confirms a real route returns text end-to-end.

- **C2b — gpt-oss MCP local-delegate.** `tanglebrain-delegate`, an MCP server exposing a single
  `delegate_local(prompt, max_tokens?)` tool, lets a frontier orchestrator (claude / codex /
  gemini) offload grunt work to the free local tier (gpt-oss-120b) at $0 — the mechanism behind
  frontier-first decompose (plan §6). Closes #4.
  - `tanglebrain/delegate.py`: `run_local_delegate(...)` — the routing logic, **reusing** C1's
    roster + `select_local` + `OpenAICompatAdapter` (no duplicated LiteLLM/endpoint/key logic).
    MCP-free so it stays hermetically testable; failures surface to the orchestrator (no retry).
  - `tanglebrain/mcp_server.py`: a thin `FastMCP` wrapper exposing the sync `delegate_local` tool
    (its docstring is the orchestrator-facing contract). Console entry `tanglebrain-delegate`
    serves over stdio. Verified end-to-end: a real MCP stdio client calls the tool and gets
    gpt-oss text back.
  - The `mcp` SDK is an **optional dependency** — `pip install "tanglebrain[delegate]"`; the core
    install stays lean (httpx + PyYAML). README documents per-CLI registration.

- **C2 — CLI adapters for the three subscription tools (claude / codex / gemini), with
  env-scrub.** The subscription tier is now invocable end-to-end through the uniform
  `run(prompt, opts) -> text` interface.
  - `CliAdapter` (`tanglebrain/adapters/cli.py`): runs a sub CLI as a subprocess (never via a
    shell) and returns its final text. Prompt injection is config-driven — a `{prompt}` token
    in the roster `cmd` is substituted (gemini's `-p {prompt}`), otherwise the prompt is
    appended as the final argument (claude, codex).
  - **Env-scrub (§7), the safety-critical piece:** `invoke.scrub_env` strips named vars from a
    *copy* of the environment handed to the subprocess (the parent `os.environ` is never
    mutated), so `claude -p` rides the flat Max subscription instead of the per-token
    `ANTHROPIC_API_KEY`. Proven by a live test: claude reports the key as `UNSET`.
  - Output parsers selected per entry via a new `invoke.parse` roster field: `claude-json`
    (single `{"result": ...}` object), `gemini-json` (`{"response": ...}`), and `plain`
    (stripped stdout, for codex `exec`). Parsers were written against real captured CLI output.
  - `AdapterError` promoted to `tanglebrain/adapters/base.py` so the openai-compat and CLI
    adapters and the routing layer share one error type (re-exported from `openai_compat` for
    backwards-compatible imports).
  - `selector.build_adapter` now builds the `cli` adapter; `selector.select_by_id` plus a new
    `tanglebrain --model <id>` flag let a named sub be driven end-to-end. This is an explicit
    override, **not** the §6 cost-tiered router (still C3).
  - Roster `cmd` for claude switched from `stream-json` to `--output-format json` (a single
    parseable object). The gpt-oss MCP local-delegate (the other half of plan §10's C2 line)
    was split out to issue #4 (C2b), to land near C3 where it has a consumer.

## [0.1.0] - 2026-06-16

### Added

- **C1 — repo skeleton + roster loader + openai-compat adapter.** One request now routes to
  the free local tier (gpt-oss-120b on local via LiteLLM) end-to-end.
  - Python package skeleton (`tanglebrain/`), `pyproject.toml`, `Makefile`, and `tests/`
    mirroring coordinator conventions (stdlib `unittest`, venv-based test target, `make lint/test`).
  - Roster config loader (`tanglebrain/roster.py`): parses the YAML roster into typed
    objects. The roster is config-driven and open-ended — adding a model is an entry edit,
    not a code change. The starting roster is `gpt-oss-120b` + the three subscription CLIs.
  - `openai-compat` adapter (`tanglebrain/adapters/openai_compat.py`) with the uniform
    `run(prompt, opts) -> text` interface, calling the local LiteLLM endpoint directly.
    Returns only the final `content` (drops `reasoning_content`); defaults `max_tokens` to
    2048 per the C0 budget lesson. Resolves the scoped key via the contract's `key_ref`.
  - Local-first selector (`tanglebrain/selector.py`) and CLI entry point
    (`tanglebrain/cli.py`) wiring roster → local entry → adapter → text.
  - Migrated the canonical plan (`.claude/plans/tanglebrain.md`) and the orchestration
    contract (`TANGLEBRAIN.md`, with a SUPERSEDED banner) into this repo.
  - Baseline hygiene files: `README`, `LICENSE`, `CHANGELOG`, `.gitignore`.

### Internal

- Resolved the two parked design decisions (PM, 2026-06-16; see issue #2 and plan §9.6–9.7):
  paid-API billing will be gated by an explicit `api_billing_enabled` flag (**default off**),
  with each paid key a `tier: api` roster entry carrying a per-key enable toggle + budget cap,
  **fronted through LiteLLM** (TangleBrain references a scoped virtual key — preferred over a raw
  provider key, which is not foreclosed but stays behind the toggle). Reconciled contract invariant
  #3 accordingly — it now *softens, not reverses* (the durable rule is *no paid billing without the
  explicit toggle*). No code behavior change yet; the paid-API tier itself is a later chunk (#2).
