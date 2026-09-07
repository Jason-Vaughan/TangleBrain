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
make lint          # ruff (lint) + mypy (type-check)
make test          # lint + type-check + the unit test suite (hermetic; HTTP is mocked)
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
- [`docs/design/`](docs/design/) — the design reasoning: what each surface promises, what data
  survives a crash, what's protected and what's accepted risk, and where the project is currently
  weak. Start with [`docs/design/README.md`](docs/design/README.md). Every gap named there has a
  tracking issue, so it doubles as a map of what's open.

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
- **Changes to routing, adapters, or either HTTP surface should update the matching document in
  [`docs/design/`](docs/design/) in the same PR.** Those documents state what the project promises
  and why; a change that makes one of them wrong is incomplete, not merely undocumented. If your
  change closes a gap one of them names, delete the admission rather than leaving it stale.

## Code style & tests

- Follow the existing style and conventions of the surrounding code. **There is no formatter, on
  purpose** — `ruff format` was measured and declined (it would rewrite most of the tree while
  catching nothing), so please match the surrounding code by hand rather than running a formatter
  over your diff. The reasoning is in
  [`docs/design/nonfunctional-requirements.md`](docs/design/nonfunctional-requirements.md),
  "Code quality gates".
- **`make lint` must be clean.** Ruff is configured to catch defects, not style, so a finding
  usually means a real problem rather than a preference. If a rule is genuinely wrong about your
  code, waive it at the site with a reason — `# noqa: RULE — why` — rather than loosening the
  config; `RUF100` will tell you if the waiver later stops being needed.
- **mypy runs over `tanglebrain/`** at default strictness. Annotations there are checked, so a new
  public function should carry them.
- **All functions get a docstring**; keep functions short and single-purpose.
- **Write tests alongside the implementation.** New behavior needs hermetic coverage in `tests/`;
  bug fixes should add a regression test.
- Run `make test` and make sure it's green before opening the PR — it runs the gates above first,
  so a green `make test` is the whole bar.

## Filing issues

Use the [issue templates](.github/ISSUE_TEMPLATE/): **bug**, **feature**, or **add a backend /
adapter**. For bugs, include reproduction steps and what you expected. For security-sensitive
reports, don't open a public issue — see **Security rules** at the end of this file for the private
reporting route.

## License

By contributing, you agree that your contributions are licensed under the project's
[MIT License](LICENSE).

<!-- MIRRORED from https://github.com/Jason-Vaughan/.github/blob/main/CONTRIBUTING.md
     Edit that file first, then update this copy. Do not let them diverge.
     The markers below are load-bearing: a drift checker extracts exactly what lies between
     them and compares it byte-for-byte with upstream, so do not remove, rename or reformat
     them, and do not add repo-specific prose inside them — it belongs outside. -->

## Security rules that apply to every repository

<!-- BEGIN mirrored-security-rules -->
1. **The Clean Room Reconstruction Standard (Dual-Key Review).** Pull requests from contributors we do not know are reviewed as **raw text diffs** through a strict dual-key process:
   - **First Pass (Macro Filter):** The Coordinator session performs the initial security audit, explicitly checking for supply chain attacks, `package.json` tampering, and broad logical soundness.
   - **Second Pass (Micro Filter):** If the PR passes the Coordinator, the Builder session performs an independent raw-text audit to catch subtle logic bombs or regressions before execution.
   
   Maintainers will not check out your branch or run your code on their own machines. If your contribution clears both audits, the Builder re-implements the logic from scratch on `main` and **credits you as the author**. We merge ideas, not raw bytes.

   To be precise, because this is a security claim and a vague one is worthless: *continuous
   integration does run your tests* when a repository's workflows are triggered by pull requests.
   Those runs are sandboxed by GitHub, with a read-only token and no access to repository secrets.
   The commitment is that no maintainer executes your code on their own hardware.

2. **Because we reconstruct it, your explanation is worth more than your code.** The most valuable
   pull request describes the bug precisely, says why it happens, and explains the approach. A clear
   description gets reconstructed and shipped. A large, clever, unexplained diff does not, however
   good it is.

3. **Scope.** One issue per pull request, and nothing outside it. A diff that touches files
   unrelated to the issue will be closed regardless of quality — from the outside, scope overrun and
   probing are indistinguishable.

4. **Forbidden files.** Core infrastructure is off-limits unless an issue explicitly asks for a
   change there: CI workflows (`.github/workflows/`), build scripts and `Makefile`s, install or
   deploy scripts, git hooks, and orchestrator configuration. What these have in common is that they
   execute **without anyone choosing to run them**, which is what separates them from ordinary
   source. Unexplained modifications there are treated as payload and the pull request is closed.

5. **Reviewable text only.** No binary files, and no generated, minified or vendored code — none of
   them can be read as a diff, and a diff is the only review we perform.

   For the same reason, source must contain no **bidirectional control characters**, no
   **zero-width or invisible characters**, and no **non-ASCII homoglyphs standing in for ASCII in
   identifiers**. Those three make a diff *render* differently from what it *executes*
   ("Trojan Source", CVE-2021-42574) — an attack aimed precisely at a text-based review, which
   defeats our method by construction rather than by degree. Ordinary Unicode in prose, comments and
   string literals is fine: it is the invisible and the disguised that are the problem, not the
   non-English.

6. **No new dependencies.** Adding third-party libraries, npm packages or PyPI dependencies
   introduces supply-chain risk that these projects have deliberately designed out. Unless an issue
   explicitly asks for a new dependency, do not add one — and note that some of our repositories are
   strictly zero-dependency, where adding one is an automatic rejection. A solution built on the
   standard library is vastly preferred over importing a new package.

7. **No obfuscation.** Code must be clear and readable. Base64 payloads, hidden network requests,
   dynamic `eval`/`require` of constructed strings, or deliberately obscured logic will result in an
   immediate ban. This is the one rule where we assume intent, because none of those happen by
   accident.

<!-- END mirrored-security-rules -->

<!-- Repo-specific, deliberately OUTSIDE the mirrored block: the upstream rules do not carry a
     vulnerability-reporting route, and this one is per-repository (the Security tab is this
     repo's). Putting it inside the markers would break byte-identity with upstream and the
     drift checker would red. "Filing issues" above points here for the private route. -->

### Reporting a vulnerability

Do not open a public issue. Open this repository's **Security** tab and choose **Report a
vulnerability** — see the [security policy](../../security/policy).

Full contribution guide: https://github.com/Jason-Vaughan/.github/blob/main/CONTRIBUTING.md
