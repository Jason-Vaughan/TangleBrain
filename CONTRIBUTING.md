# Contributing to TangleBrain

Thanks for your interest in TangleBrain — a local-first, config-driven router across
OpenAI-compatible backends you own. Contributions of all sizes are welcome: bug fixes, docs,
tests, and new backend/adapter support.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Please also read
the [Disclaimer](DISCLAIMER.md) — it explains the opt-in posture of the subscription /
authenticated-CLI adapters (your responsibility under each provider's Terms of Service) and the
bring-your-own-key, off-by-default paid-API tier. Keep contributions consistent with that posture.

## Dev setup

Requires **Python ≥ 3.10**.

```sh
make venv          # create .venv and install -e . (with dev + optional extras)
make help          # list all targets
make lint          # smoke-check every Python file parses
make test          # lint + run the unit test suite (hermetic; HTTP is mocked)
```

`make test` is the suite to run before every PR — it is fully **hermetic** (all network calls are
mocked), so it needs no backend and no credentials.

There is also a small **live** suite gated behind the `TANGLEBRAIN_LIVE` environment variable
(`make test-live`). It hits a real local endpoint your roster points at, end-to-end. **You do not
need it to contribute** — it requires a configured backend and is skipped by default. CI and
reviewers rely on `make test`.

## How the project is laid out

- `tanglebrain/` — the package: `roster.py` (config model + discovery), `adapters/` (the uniform
  `run(prompt, opts) -> text` surface: `openai_compat` / `cli` / `api`), `router.py` (orchestrator
  selection, rotation, failover), `classifier.py` (optional gate), `measurement.py` (per-task
  logging + rollup), `gui/` (the localhost knob panel), `mcp_server.py` + `delegate.py` (the
  local-delegate MCP tool), `settings.py` (the gates).
- `tanglebrain/config/` — the packaged example `roster.yaml`, `pricing.yaml`, `settings.yaml`.
- `tests/` — stdlib `unittest`, mock-based, hermetic.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — how the pieces fit together. Read this first if you're
  changing routing behavior.

## Good first contributions

**Adding a backend is a config edit, not a code change.** The roster is a plain YAML list — a new
local server, an authenticated CLI, or a paid endpoint is a new entry, not new Python. Great
starter contributions:

- **Document a new backend recipe** — a worked roster entry for a local server or an
  OpenAI-compatible gateway, with the exact `invoke` fields it needs.
- **Improve an adapter's robustness** — better error messages, a new `parse` option for a CLI's
  output shape, more test coverage.
- **Docs & examples** — clearer setup steps, fixing anything a newcomer stumbled on.

If a backend genuinely can't be expressed as a roster entry and needs a new `invoke.kind`, that's a
code change to `adapters/` + `roster.py` — open an issue first so we can agree on the shape.

## Branch & PR conventions

- **Branch from `main`** with a typed name: `feat/<short-name>`, `fix/<short-name>`,
  `docs/<short-name>`, `chore/<short-name>`, `refactor/<short-name>`, `test/<short-name>`.
- **Keep commits small and focused**; write messages that explain *why*, not just *what*.
- **Open a pull request** using the template. The body should have three sections:
  - **What** — the change in one or two sentences.
  - **Why** — the motivation / the problem it solves (link issues with `Fixes #N`).
  - **Test plan** — how you verified it (`make test` output, manual steps).
- **Update docs in the same PR as the code.** If behavior changes, update the relevant doc
  (`README.md` / `ARCHITECTURE.md`) and add a `CHANGELOG.md` entry under `[Unreleased]`.

## Code style & tests

- Follow the existing style and conventions of the surrounding code.
- **All functions get a docstring**; keep functions short and single-purpose.
- **Write tests alongside the implementation.** New behavior needs hermetic coverage in `tests/`;
  bug fixes should add a regression test.
- Run `make test` and make sure it's green before opening the PR.

## Filing issues

Use the [issue templates](.github/ISSUE_TEMPLATE/): **bug**, **feature**, or **add a backend /
adapter**. For bugs, include reproduction steps and what you expected. For security-sensitive
reports, please don't open a public issue — see the contact in the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).
