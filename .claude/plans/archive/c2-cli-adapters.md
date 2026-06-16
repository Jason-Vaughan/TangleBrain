# Build Plan — C2: CLI adapters for the three subs (+ env-scrub)

**Chunk:** C2 (plan §10) · **Branch:** `feat/c2-cli-adapters` · **Status:** PROPOSED (awaiting sign-off)
**Predecessor:** C1 shipped (v0.1.0) — openai-compat adapter, roster loader, local-first selector.

---

## 0. Scope decision (the one fork)

Plan §10 bundles C2 as two things:
1. **CLI adapters for claude / codex / gemini with env-scrub (§7).** — well-defined, safety-critical, fully hermetic-testable.
2. **The generalized gpt-oss MCP tool from C0 as each orchestrator's local delegate.**

**Recommendation: scope C2 to (1) only; split (2) into C2b (fold near C3).** Rationale:
- The MCP delegate only has *value* once an orchestrator decomposes + delegates — that is the
  frontier-first router, which is **C3**. Building the delegate now ships dead weight until C3.
- It is a distinct mechanism (an MCP server exposed *to* the CLI orchestrators), not part of the
  adapter `run(prompt) -> text` path. Bundling muddies the chunk.
- Prawduct: *"If a chunk is too large, split it and defer the rest."* (CLAUDE.md, Building phase.)

If the user prefers the original bundling, C2 grows a §6 (below) for the MCP delegate.

---

## 1. Goal

Make the three subscription CLIs (`claude`, `codex`, `gemini`) **invocable** through the uniform
`Adapter.run(prompt, opts) -> text` interface, with **env-scrub** honored so `claude -p` rides the
flat Max sub, never the per-token `ANTHROPIC_API_KEY` (confirmed live in this Mac's env, 108 chars).

Success criteria:
- Each of the three CLIs returns text end-to-end through `CliAdapter`.
- `claude` runs with `ANTHROPIC_API_KEY` **provably absent** from the child process env — verified
  in both a hermetic test and a gated live test.
- Roster `scrub_env` field is honored (already first-class on `Invoke`).
- Failures (non-zero exit, timeout, unparseable output) surface as `AdapterError` — no silent retry
  or fallback (matches the openai-compat adapter contract; the routing layer decides).

---

## 2. Architecture

New module `tanglebrain/adapters/cli.py` → `class CliAdapter` implementing the `Adapter` Protocol.

**Shared mechanics (identical across all three CLIs):**
- **Env-scrub** (`_scrubbed_env(scrub_env)`): return a **copy** of `os.environ` with the named vars
  removed. Never mutate the real environ. This is THE safety feature — tested hardest.
- **Subprocess**: `subprocess.run(argv, input=prompt, capture_output=True, text=True,
  timeout=..., env=scrubbed_env, check=False)`.
  - **Prompt via argv** (verified during build): a `{prompt}` token in the roster `cmd` is
    substituted, else the prompt is appended as the final arg. Required anyway — gemini's
    `-p {prompt}` needs the prompt as the flag's value, not on stdin. No `shell=True`, ever, so the
    prompt is never interpreted as shell syntax. stdin is closed with `input=""` so a CLI that
    probes stdin (codex) does not block. (Plan §1 originally proposed stdin-default; argv won on the
    gemini constraint + uniformity.)
- **Errors → `AdapterError`** (reuse the existing class from `adapters/openai_compat.py`, or promote
  it to `adapters/base.py` so both adapters share it — see §3 note): non-zero exit (include stderr),
  `subprocess.TimeoutExpired`, empty stdout, or parser failure.

**Per-CLI difference = output parsing.** Each tool's stdout shape differs. A small named-parser set,
selected per entry (resolved from the `--output-format` value already in the roster `cmd`, falling
back to plain stdout):
- `parse_claude_stream_json` — claude `-p`: JSONL; return the terminal `type:"result"` message's
  `result` text. (⚠️ verify: `--output-format stream-json` in headless `-p` may require `--verbose`;
  if so, either add `--verbose` to the roster `cmd` or switch to `--output-format json` — a single
  object with a `result` field, simpler to parse. **Decide during build against real output.**)
- `parse_json_result` — gemini `-p --output-format json`: parse one JSON object, extract the
  response/result field (exact key verified against captured sample).
- `parse_plain` — codex `exec` (no `--output-format`): stripped stdout, or codex's own format if
  structured (verified against captured sample).

**Grounding the parsers (de-risk):** capture ONE real sample stdout from each CLI during build
(all three are logged in on this Mac) and bake into `tests/fixtures/`. Parsers are written against
real output, not guessed. Hermetic tests mock `subprocess.run` with these fixtures; gated live tests
invoke the real CLIs.

**`from_entry(entry)`**: classmethod mirroring `OpenAICompatAdapter.from_entry`; rejects non-`cli`
kinds with `AdapterError`; reads `cmd`, `scrub_env`, picks the parser.

---

## 3. Wiring (without building the C3 router)

- `selector.build_adapter`: add a `cli` branch → `CliAdapter.from_entry(entry)`. Keep `select_local`
  as-is — do NOT grow the selector into the §6 router (its own docstring warns against this).
- CLI entry point: add optional `--model <id>` to route to a named roster entry through its kind's
  adapter, so a sub can be driven end-to-end **without** C3's routing logic. Default path
  (no `--model`) stays local-first, unchanged.
- **`AdapterError` location:** it currently lives in `openai_compat.py`. The `cli` adapter needs it
  too. Decision: **promote `AdapterError` to `adapters/base.py`** and re-export from `openai_compat`
  (back-compat import) so both adapters and the selector share one error type. (Decision-framework
  note goes in the PR body.)

---

## 4. Tests (mirror C1: ~50 hermetic + gated live)

Hermetic (`tests/test_cli_adapter.py`, all mock `subprocess.run`):
- **env-scrub**: scrubbed var absent from child env; non-scrubbed var passes through; `os.environ`
  unmutated after run; multiple vars scrubbed; empty `scrub_env` is a no-op.
- **argv / stdin**: argv == `cmd` (prompt NOT in argv); prompt delivered via `input=`.
- **parsers**: each format returns correct text from its fixture; malformed output → `AdapterError`;
  empty stdout → `AdapterError`.
- **process errors**: non-zero exit → `AdapterError` carrying stderr; `TimeoutExpired` → `AdapterError`.
- **from_entry**: builds for claude/codex/gemini; rejects non-`cli` kind.
- **selector/CLI**: `build_adapter` returns a `CliAdapter` for a `cli` entry; `--model` routes to it.

Gated live (`tests/test_live.py`, skipped unless an env flag is set — matches existing C1 live test):
- each CLI returns non-empty text for a trivial prompt;
- **safety-critical**: a claude live invocation asserts the child saw no `ANTHROPIC_API_KEY`
  (e.g. prompt the CLI to report whether it sees the var, or assert via the scrubbed-env path).

---

## 5. Docs / process (CLAUDE.md core + extension rules)

- **CHANGELOG** `[Unreleased] → ### Added` (CLI adapters = user-visible new capability → **minor** bump).
- **Docstrings** on every new function/class (core rule — JSDoc-equivalent for Python).
- **roster.yaml**: update the C1 comment ("subs not routable until C2") — they're routable now;
  adjust claude's `cmd` if the stream-json/`--verbose` finding requires it.
- **Plan §10**: mark C2 status; if scope split is approved, add the C2b line for the MCP delegate.
- **Independent Critic review** after build (medium+ work): edge cases, coverage gaps, scope creep,
  doc parity. Address findings before merge.
- **Janitor pass**: dead code, unused imports, TODOs, CHANGELOG, tests green.
- **MEMORY.md**: fix the stale "PR #1 open" line (C1 is merged + released v0.1.0); set next chunk.
- **Branch + PR**: `feat/c2-cli-adapters`; PR body has What / Why / Test plan + decision-framework
  note for the `AdapterError` move. **Merge MANUALLY** — repo is PRIVATE on Free plan, `--auto` is
  Pro-gated (per MEMORY.md). No `Fixes #N` (C2 has no tracking issue; #2 is the paid-API tier, later).

---

## 6. (Only if scope NOT split) gpt-oss MCP local delegate

Port the C0 MCP server (Monad-1 `openclaw-monad-mcp`) into this repo as a local delegate exposing
gpt-oss-120b as a tool, and wire each orchestrator CLI's MCP config to it. Adds an MCP server module,
its own tests, and per-CLI MCP registration. **This is what makes C2 "too large" → recommend C2b.**

---

## Out of scope (explicit)

- The §6 frontier-first **router** (orchestrator selection, rotation, 429 failover) — that's **C3**.
- The paid-API tier / `api_billing_enabled` gate — **issue #2**, a later chunk.
- Measurement / savings rollup — **C4**.

## History
- 2026-06-16: Plan drafted (C2 kickoff, plan-first).
