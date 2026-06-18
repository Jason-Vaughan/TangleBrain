# Changelog

All notable changes to TangleBrain are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Internal

- **Documented the synthesis/reduce pattern (scatter-gather roadmap #39, slice 4).** README +
  ARCHITECTURE now spell out that the orchestrator synthesises `delegate_many` results itself (it
  holds the original task context), and offloads a *mechanical* stitch with an ordinary
  `delegate(task=…)` call — so no dedicated reducer tool ships. Documentation of existing behaviour;
  no code change. The reduce step stays the orchestrator's by design until observability data (a later
  slice) shows a TB-side reducer would earn its keep.

## [0.13.0] - 2026-06-18

### Added

- **Parallel fan-out (`delegate_many`).** A new MCP tool lets an orchestrator fan **several sub-tasks
  out concurrently** in one call and collect them, instead of delegating one at a time. Each item
  (`{prompt, target?, task?, max_tokens?}`) routes independently — a batch can mix backends — and runs
  on a `ThreadPoolExecutor` over the existing sync `run_delegate` (plain Python, no new deps). Results
  come back **in input order** with a per-item `status` (`ok` / `no_fit` / `error`); one failing
  sub-task never sinks the batch. Concurrency is bounded by a **system-derived default**
  (`os.cpu_count()`), an **operator override** (new `delegate_max_concurrency` in `settings.yaml` —
  pin it to your backend's real parallelism, e.g. `OLLAMA_NUM_PARALLEL`), and an optional per-call
  `max_concurrency` that may lower it. Dispatch + collect only — synthesis stays the orchestrator's
  job. Third slice of the scatter-gather roadmap (#39).

## [0.12.0] - 2026-06-18

### Added

- **Capability-routed delegation.** The `delegate` MCP tool gains a **`task`** parameter: instead of
  naming a backend id, an orchestrator can ask for a *capability* (a `good_at` tag, e.g. `code`) and
  TangleBrain selects the **cheapest `can_delegate` backend** good_at it (`local` before `sub`, ties
  by declared order) — sub-task-level task-fit mirroring the request-level router. Precedence is
  `target` (explicit id) > `task` (capability) > free local default. **Paid `api` backends are never
  auto-selected by `task`** (the ratified paid-is-last-resort invariant; reach one only via an
  explicit `target`). When no backend fits a `task`, the tool **hands the sub-task back to the
  orchestrator to do itself** — a returned instruction, not an error (a new `NoDelegateFit` signal
  caught at the MCP boundary). Second slice of the scatter-gather roadmap (#39).

## [0.11.0] - 2026-06-18

### Added

- **Generalized / tiered delegate.** An orchestrator can now offload a sub-task to a *configured*
  backend, not just the free local model. The `tanglebrain-delegate` MCP server gains two tools
  alongside the unchanged `delegate_local`: **`delegate(prompt, target?, max_tokens?)`** routes to
  any roster entry flagged the new **`can_delegate: true`** (mirrors `can_orchestrate`), and
  **`delegate_targets()`** lists the configured menu (`id`, `tier`, `good_at`, `cost`, `kind`) so the
  orchestrator can pick by fit; the `delegate` tool's description also enumerates the menu, built at
  server startup. Targets are invoked as leaves (no recursive delegation); `api` targets stay behind
  the billing gate. Secret-safe (the menu never emits a `key_ref`). The shipped roster flags its
  local tier `can_delegate: true` and carries a commented non-local target example. Non-local
  delegate spend is not metered in this version (orchestration-tree observability is tracked on the
  scatter-gather roadmap, #39). First slice of #39. Closes #38.
- **Project logo.** A snake-and-circuit-brain mark now brands the README (hosted in the
  `project-assets` repo) and the knob panel — `tanglebrain-gui` ships a packaged copy, serves it at
  `/logo.png`, and uses it as the page header + favicon.

### Changed

- **README restructured around Problem → Solution.** A "Cloud-by-Default Routing / routing debt"
  problem statement and a "Local-First Router You Own" solution lead the page, plus a "Standalone, or
  part of the Tangle family" section (welcomes forks/PRs; notes optional integration with
  [TangleClaw](https://github.com/Jason-Vaughan/TangleClaw)). Status line corrected to
  *v0.10.0 — first public release*.
- **README surfaces the OAuth-/local-first credential model and prompt-aware routing.** Clarifies
  that TangleBrain prefers your local models and authenticated (OAuth) tool sessions — never injecting
  an API key into a CLI — with the raw-API-key tier a deliberate, gated opt-in; and that an optional
  classifier reads each request and routes grunt work to the free local backend. The measurement
  bullet is reframed as cost measurement (spent vs avoided). Doc-only; no feature change.
- **Knob-panel header copy.** The panel subtitle now reads "roster & pricing config · local
  spend-avoided rollup" (was a stale "read-only — cost-tiered router config …"; the panel has been
  editable since the pricing/roster knobs landed).

## [0.10.0] - 2026-06-17

First public release.

### Changed

- **Neutral positioning + local-only default roster (public-OSS rollout, R2a).** Reframed the project
  as a *local-first, config-driven router across OpenAI-compatible backends you own*. The packaged
  `config/roster.yaml` now ships **one active entry — the free local tier**; the subscription /
  authenticated-CLI tier (claude/codex/gemini) ships **commented out** as an opt-in example like the
  paid tier, so a fresh clone routes to local out of the box. README rewritten for newcomers (neutral
  headline, capability list, `--local`-first quickstart); new `ARCHITECTURE.md` (clean-room, neutral)
  and `DISCLAIMER.md` (subscription/CLI adapters are opt-in and your responsibility under each
  provider's ToS; paid tier is bring-your-own-key, off by default). `PackagedRosterTest` updated to
  the one-active-entry reality.
- **Generic shipped roster + external roster discovery.** The bundled `config/roster.yaml` is now a
  **generic example** (free local tier points at Ollama on `localhost:11434`, opt-in subscription-CLI
  entries, no maintainer infra). Your real roster lives **outside the repo** and is auto-discovered:
  `TANGLEBRAIN_ROSTER` env → `~/.config/tanglebrain/roster.yaml` (XDG) → the packaged example. So a
  `git pull` never clobbers your config, and the package ships nothing deployment-specific. The
  `--roster` flag still takes precedence. Part of the public-OSS rollout.

### Added

- **`tanglebrain --version`** prints the package version (from `tanglebrain.__version__`) and exits.
  Closes #29.
- **Contributor mechanics (public-OSS rollout, R2b).** `CONTRIBUTING.md` (dev setup via
  `make venv` / `make test`, branch & PR conventions, What/Why/Test-plan, and "adding a backend is a
  config edit" first-contribution framing), `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1), GitHub
  issue templates (`bug`, `feature`, `add a backend/adapter`), and a pull-request template with a
  What/Why/Test-plan body and a docs-updated checklist. README gained a Contributing section.
- `roster.packaged_roster_path()` (the bundled example) and `roster.default_roster_path()` discovery,
  mirroring the existing state-dir resolution pattern.

### Internal

- **Dropped local-tooling references from product files.** Neutralized cosmetic mentions of the
  local development tooling in `CHANGELOG.md`, `tanglebrain/gui/views.py`, `tanglebrain/gui/server.py`
  (the `--port` help text), and the `.gitignore` comment — they described the maintainer's local
  workflow, not the product. No behavior change.
- **Aligned code docstrings, comments, the CLI `--help` text, and shipped config comments with the
  project's documentation.** A consistency pass so the in-code descriptions match the
  README/ARCHITECTURE framing — the router is described as orchestrator rotation + failover for
  resilience — and a generic example hostname replaces a deployment-specific one in the tests.
  Docstrings/comments/strings only — no behavior change (verified by an AST-token structural diff).
  Closes #30. A GitHub Actions workflow
  (`.github/workflows/ci.yml`) runs `make test` (the hermetic suite) on every push to `main` and on
  pull requests, across Python 3.10/3.11/3.12. The `TANGLEBRAIN_LIVE`-gated tests stay skipped (CI has
  no backend). README gained a CI status badge. CI immediately surfaced a test-isolation gap — three
  `--model "claude"` CLI tests relied on the dev machine's ambient `~/.config/tanglebrain/roster.yaml`
  (the packaged example is local-only since R2a) — now fixed to pin a self-contained roster.
- Live e2e test (`tests/test_live.py`) pins the **direct-local** path (`run_once(..., local=True)`)
  and asserts it was served by the active roster's own local entry (roster-agnostic). Bare `run_once`
  has routed through the frontier-first router since the default flip, so the acceptance assertion had
  quietly stopped exercising the local path (#24). Test-only.

## [0.9.0] - 2026-06-17

### Added

- **Local classifier gate (plan §6 evolution path), off by default.** An optional cheap local
  classify can now run in front of the router: it rates each request's complexity using free local
  gpt-oss and sends **trivial** work straight to free local (skipping the rate-limited subs), while
  **frontier** work falls through to the normal frontier-first router. This preserves sub rate-limit
  runway when rotation alone isn't enough.
  - **Off by default** — new `classifier_gate_enabled` setting (`config/settings.yaml`, default
    `false`); per-run `--gate` / `--no-gate` override the setting. Built ahead of the §8 data trigger,
    so existing routing behaviour is unchanged until it's turned on.
  - **Fail-safe by design** — the classifier rates *task complexity* (not "can the local model do
    it?"), and any ambiguity, parse miss, or classifier error resolves to **frontier**, so the gate
    can never trap a hard task on the local tier. New `tanglebrain/classifier.py`; gated work is
    metered with `path=gate-local`.

## [0.8.0] - 2026-06-17

### Added

- **Editable roster in the knob panel (plan §5/§9.2).** The `tanglebrain-gui` roster card is now
  editable for a focused set of per-entry scalar fields — `enabled`, `can_orchestrate`,
  `budget_usd_month`, and `good_at` — each row with its own Save. Completes the deferred half of the
  C5 knob GUI (pricing became editable in C5b).
  - **Comment-preserving, zero new deps**: a new `tanglebrain/roster_edit.py` edits the targeted
    value on the targeted line *in place*, so every inline comment, blank line, the nested `invoke`
    block, and the commented paid-API example survive byte-for-byte — no YAML round-trip library.
    Adding/removing/reordering entries and editing the `invoke` block stay hand-edits (out of scope).
  - **Write-safety** mirrors C5b: edits are validated (and the candidate is **re-parsed with the real
    loader before any write**, so a surgical slip can never land a malformed roster), the prior file
    is backed up to `<state_dir>/backups/roster-<ts>.yaml`, and the write is atomic. The panel sends
    only changed fields and confirms before overwriting the tracked `config/roster.yaml`.
  - New `views.save_roster_view()` + `POST /api/roster`.

## [0.7.1] - 2026-06-16

### Fixed

- `tanglebrain.__version__` now derives from the installed package metadata
  (`importlib.metadata.version`) instead of a hardcoded literal, so it always tracks
  `pyproject.toml` and can no longer drift from the released version — it had been frozen at
  `0.1.0` since C1 while releases moved on to 0.7.0 (#17). Falls back to `0.0.0+unknown` when
  imported from an uninstalled source checkout.

## [0.7.0] - 2026-06-16

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

- **C6c — paid-API visibility in the knob panel + runbook.** Closes #2. The `tanglebrain-gui` roster
  card now surfaces each entry's `enabled` kill-switch (a `disabled` pill) and `budget_usd_month`
  (a display-only `budget: $N/mo` note), and shows a **Paid-API billing: ON/OFF** banner from the
  global gate — so an operator never misreads a paid entry's own `enabled` flag as "live" when the
  global gate is off. New `view_settings()` view + `GET /api/settings` route (reads only
  `config/settings.yaml`; no key file touched). All read-only — per the v1 decision, TangleBrain
  does **not** meter or enforce spend; the hard budget cap stays LiteLLM-side on the virtual key.
  - README gains a step-by-step **runbook** for minting a budget-scoped LiteLLM virtual key on your
    LiteLLM gateway and wiring it via `key_ref`, plus how to pause spend (`enabled: false` or the gate).

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

- **C5a — knob GUI (read-only panel), a simple dark-themed panel.** A new `tanglebrain-gui` console
  script serves a thin, **localhost-only** web panel (stdlib `http.server` + a single vanilla
  HTML/CSS/JS page — zero new runtime dependencies) on port 3250. The panel:
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
  - **Config-driven pricing** (`tanglebrain/config/pricing.yaml`) carrying a local pricing source's
    `costSaved` anchor — Claude Sonnet at $3/$15 per MTok (methodology ratified 2026-06-13) — so
    avoided spend is valued consistently. A `placeholder` flag (false by default) makes the
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
  `delegate_local` tool available, so it decomposes the task and offloads sub-tasks to the free
  local backend — the offload behind frontier-first decompose (plan §6). Closes #7.
  - **Config-driven injection**: a new `invoke.delegate_args` roster field carries the per-CLI
    flags that register + allow the delegate, with `{delegate_mcp_json}` / `{delegate_mcp_command}`
    tokens substituted at runtime (the delegate runs as `python -m tanglebrain.mcp_server`, so it
    resolves without PATH assumptions). Adding/adjusting a CLI is a config edit (§5).
  - **Verified live, all three orchestrators delegate to gpt-oss**: claude (`--mcp-config` +
    `--allowedTools`, API key scrubbed), codex (`-c mcp_servers…` + approval bypass), gemini
    (after a one-time `gemini mcp add` + `--approval-mode yolo`). See the README for gemini's setup.

### Added

- **C3 — frontier-first router (control plane).** `tanglebrain/router.py` routes a task to a
  frontier sub acting as orchestrator, rotating the role across the `can_orchestrate` subs with
  automatic failover for resilience (plan §6).
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
    mutated), so `claude -p` uses its own authenticated session rather than the injected
    `ANTHROPIC_API_KEY`. Proven by a live test: claude reports the key as `UNSET`.
  - Output parsers selected per entry via a new `invoke.parse` roster field: `claude-json`
    (single `{"result": ...}` object), `gemini-json` (`{"response": ...}`), and `plain`
    (stripped stdout, for codex `exec`). Parsers were written against real captured CLI output.
  - `AdapterError` promoted to `tanglebrain/adapters/base.py` so the openai-compat and CLI
    adapters and the routing layer share one error type (re-exported from `openai_compat` for
    backwards-compatible imports).
  - `selector.build_adapter` now builds the `cli` adapter; `selector.select_by_id` plus a new
    `tanglebrain --model <id>` flag let a named sub be driven end-to-end. This is an explicit
    override, **not** the §6 frontier-first router (still C3).
  - Roster `cmd` for claude switched from `stream-json` to `--output-format json` (a single
    parseable object). The gpt-oss MCP local-delegate (the other half of plan §10's C2 line)
    was split out to issue #4 (C2b), to land near C3 where it has a consumer.

## [0.1.0] - 2026-06-16

### Added

- **C1 — repo skeleton + roster loader + openai-compat adapter.** One request now routes to
  the free local tier (a local gpt-oss model via LiteLLM) end-to-end.
  - Python package skeleton (`tanglebrain/`), `pyproject.toml`, `Makefile`, and `tests/`
    following the project's conventions (stdlib `unittest`, venv-based test target, `make lint/test`).
  - Roster config loader (`tanglebrain/roster.py`): parses the YAML roster into typed
    objects. The roster is config-driven and open-ended — adding a model is an entry edit,
    not a code change. The starting roster is `gpt-oss-120b` + the three subscription CLIs.
  - `openai-compat` adapter (`tanglebrain/adapters/openai_compat.py`) with the uniform
    `run(prompt, opts) -> text` interface, calling the local LiteLLM endpoint directly.
    Returns only the final `content` (drops `reasoning_content`); defaults `max_tokens` to
    2048 per the C0 budget lesson. Resolves the scoped key via the contract's `key_ref`.
  - Local-first selector (`tanglebrain/selector.py`) and CLI entry point
    (`tanglebrain/cli.py`) wiring roster → local entry → adapter → text.
  - Brought the project's planning and design docs into this repo (the current architecture is
    documented in `ARCHITECTURE.md`).
  - Baseline hygiene files: `README`, `LICENSE`, `CHANGELOG`, `.gitignore`.

### Internal

- Resolved the two parked design decisions (PM, 2026-06-16; see issue #2 and plan §9.6–9.7):
  paid-API billing will be gated by an explicit `api_billing_enabled` flag (**default off**),
  with each paid key a `tier: api` roster entry carrying a per-key enable toggle + budget cap,
  **fronted through LiteLLM** (TangleBrain references a scoped virtual key — preferred over a raw
  provider key, which is not foreclosed but stays behind the toggle). Reconciled contract invariant
  #3 accordingly — it now *softens, not reverses* (the durable rule is *no paid billing without the
  explicit toggle*). No code behavior change yet; the paid-API tier itself is a later chunk (#2).
