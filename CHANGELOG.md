# Changelog

All notable changes to TangleBrain are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **C1 — repo skeleton + roster loader + openai-compat adapter.** One request now routes to
  the free local tier (gpt-oss-120b on Monad via LiteLLM) end-to-end.
  - Python package skeleton (`tanglebrain/`), `pyproject.toml`, `Makefile`, and `tests/`
    mirroring Monad-1 conventions (stdlib `unittest`, venv-based test target, `make lint/test`).
  - Roster config loader (`tanglebrain/roster.py`): parses the YAML roster into typed
    objects. The roster is config-driven and open-ended — adding a model is an entry edit,
    not a code change. The starting roster is `gpt-oss-120b` + the three subscription CLIs.
  - `openai-compat` adapter (`tanglebrain/adapters/openai_compat.py`) with the uniform
    `run(prompt, opts) -> text` interface, calling the Monad LiteLLM endpoint directly.
    Returns only the final `content` (drops `reasoning_content`); defaults `max_tokens` to
    2048 per the C0 budget lesson. Resolves the scoped key via the contract's `key_ref`.
  - Local-first selector (`tanglebrain/selector.py`) and CLI entry point
    (`tanglebrain/cli.py`) wiring roster → local entry → adapter → text.
  - Migrated the canonical plan (`.claude/plans/tanglebrain.md`) and the orchestration
    contract (`TANGLEBRAIN.md`, with a SUPERSEDED banner) into this repo.
  - Baseline hygiene files: `README`, `LICENSE`, `CHANGELOG`, `.gitignore`.
