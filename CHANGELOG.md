# Changelog

All notable changes to TangleBrain are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
