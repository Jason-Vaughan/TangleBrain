# Build Plan — C2b: gpt-oss MCP local-delegate

**Chunk:** C2b (issue #4; the deferred half of plan §10's C2 line) · **Branch:** `feat/c2b-mcp-delegate`
**Predecessors:** C1 (openai-compat adapter, v0.1.0) + C2 (CLI adapters, merged #5).
**Status:** APPROVED (user chose C2b over C3 as the next chunk; it's the prerequisite that makes
frontier-first decompose actually offload grunt to free local — the north star).

---

## 1. Goal

Expose TangleBrain's **free local tier (gpt-oss-120b)** as an **MCP tool** that a frontier
orchestrator CLI (claude / codex / gemini) can call to delegate grunt work — at $0 marginal cost —
then review the result. This is the "generalized gpt-oss MCP tool from C0" plan §10 names.

Success criteria:
- A stdio MCP server (`tanglebrain-delegate`) exposes a `delegate_local(prompt, max_tokens?)` tool
  that routes to the roster's local tier and returns its final text.
- The delegation **reuses** the C1 `OpenAICompatAdapter` + roster `select_local` — no duplicated
  LiteLLM/HTTP logic, no second source of truth for the endpoint/key.
- A real orchestrator (claude) can register the server and successfully call the tool (manual live
  proof, like C2's env-scrub proof).
- The `mcp` SDK is an **optional dependency** — core `pip install tanglebrain` stays lean
  (httpx + PyYAML); only `tanglebrain[delegate]` pulls the MCP stack.

Out of scope: the §6 router (selection/rotation/failover) and the decompose→delegate→review loop —
that's **C3**. C2b ships the *delegate*; C3 builds the orchestration that drives it.

---

## 2. Architecture

Reference: C0's `Monad-1/tools/openclaw-monad-mcp/openclaw_monad_mcp.py` — `FastMCP`, async tools
POSTing to LiteLLM, raises-no-retry, env-driven. We **generalize**: roster-driven, adapter-reused.

Two modules, split so the delegation logic is importable & testable **without** the `mcp` SDK:

- **`tanglebrain/delegate.py`** (no `mcp` import — pure, hermetic):
  - `run_local_delegate(prompt, max_tokens=2048, roster_path=None) -> str`
  - Loads the roster, `select_local`, builds `OpenAICompatAdapter`, calls
    `.run(prompt, {"max_tokens": max_tokens})`. Reuses everything from C1.
  - `DEFAULT_DELEGATE_MAX_TOKENS = 2048` (the C0 budget lesson — gpt-oss spends budget on internal
    reasoning). Roster path may also come from `TANGLEBRAIN_ROSTER` env (the server runs as a
    subprocess launched by the CLI, so it must locate the roster; packaged default works).
  - Surfaces `SelectionError` / `AdapterError` to the caller — no retry/fallback (the orchestrator
    decides), matching the adapter contract and the C0 reference.

- **`tanglebrain/mcp_server.py`** (imports `mcp`):
  - `mcp = FastMCP("tanglebrain-delegate")`; `@mcp.tool() def delegate_local(prompt, max_tokens=2048)`
    → `run_local_delegate(...)`. **Sync** tool (FastMCP runs sync tools in a worker thread), so it
    calls our sync adapter directly — no async duplication. (Verify FastMCP sync support at build
    start; if it requires async, wrap via `anyio.to_thread.run_sync`.)
  - The tool **docstring is the orchestrator-facing contract** — write it richly (when to delegate,
    that it's free/local, hand result back for review), mirroring C0's `monad_grunt` doc.
  - `main()` → `mcp.run()`. Console entry `tanglebrain-delegate`.

---

## 3. Packaging

- `pyproject.toml`:
  - `[project.optional-dependencies] delegate = ["mcp>=1.0"]`.
  - `[project.scripts] tanglebrain-delegate = "tanglebrain.mcp_server:main"`.
- `Makefile`: `venv` installs `-e ".[delegate]"` so the server + its tests run locally; note the
  extra in `help`. (mcp 1.27.2 confirmed installable on this Python 3.14.3.)

---

## 4. Tests (mirror C1/C2: hermetic + gated live + manual proof)

Hermetic — `tests/test_delegate.py` (no `mcp` needed):
- `run_local_delegate` returns the adapter's text; threads `max_tokens` into the adapter opts;
  defaults to 2048; selects the local entry; propagates `SelectionError` (no local entry) and
  `AdapterError` (endpoint failure). Mock `build_adapter`/`load_roster` like `test_cli.py`.

Server — `tests/test_mcp_server.py`, `@skipUnless(mcp importable)` (it is, in the dev venv):
- the `delegate_local` tool is registered on the FastMCP instance;
- invoking the tool function calls `run_local_delegate` with the right args (patch it) and returns
  its result; default max_tokens is 2048.

Gated live — extend `tests/test_live.py` (`TANGLEBRAIN_LIVE=1`):
- `run_local_delegate("...")` hits real gpt-oss and returns non-empty text (reuses live infra).

Manual definition-of-done (documented, run once): register `tanglebrain-delegate` with claude
(`claude mcp add ...`), ask claude to call `delegate_local`, confirm it offloads and returns text.

---

## 5. Docs / process (CLAUDE.md rules)

- **CHANGELOG** `[Unreleased] → ### Added` (new MCP server + tool + optional extra = user-visible →
  **minor** bump).
- **Docstrings** on every function/tool (core rule); the tool docstring doubles as orchestrator UX.
- **README / a `docs/` note**: how to install (`pip install -e ".[delegate]"`) and register the
  server with each CLI (claude `mcp add`, codex MCP config, gemini `mcp add`) — flag shapes vary by
  CLI version (C0 README flagged this), so document the canonical shape + "check `<cli> mcp --help`".
- **Plan §10**: mark C2b shipped; close issue #4 via the PR (`Closes #4`).
- **Independent Critic review** after build; address findings before merge.
- **Janitor**: dead code, imports, TODOs, CHANGELOG, tests green.
- **MEMORY.md**: record C2b shipped; next = C3 (router + decompose→delegate→review loop, now that
  the delegate exists).
- **Branch + PR** `feat/c2b-mcp-delegate`; **merge MANUALLY** (private/Free plan → no `--auto`).
  `Closes #4`.

## Out of scope (explicit)
- §6 router (selection/rotation/failover) and the orchestration loop → **C3**.
- Paid-API tier / `api_billing_enabled` → issue #2.
- Re-exposing the Qwen coder/chat tools from C0 — TangleBrain's local tier is gpt-oss; one tool.

## History
- 2026-06-16: Plan drafted (C2b, continuation after C2 merged; user chose C2b next).
