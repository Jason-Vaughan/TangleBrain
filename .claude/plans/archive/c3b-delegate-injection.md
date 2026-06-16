# Build Plan — C3b: delegate-injection + flip default to frontier-first

**Chunk:** C3b (issue #7) · **Branch:** `feat/c3b-delegate-injection`
**Predecessors:** C2 (CLI adapters #5), C2b (MCP delegate #6), C3 (router #8).
**Status:** APPROVED (user: "push into c3b now"). Behavior-changing → plan-first.

---

## 1. Goal

Make frontier-first **cost-effective and default**: give each orchestrator the C2b `delegate_local`
tool per-invocation so it offloads grunt to free local gpt-oss, and flip the CLI default from
local-first to the router. This is what realizes the north star (sub decomposes → delegates grunt
to local at $0 → reviews).

**Proven feasible (live spike, 2026-06-16):** `claude -p` with `--mcp-config <json> --allowedTools
mcp__tanglebrain-delegate__delegate_local --strict-mcp-config` and `ANTHROPIC_API_KEY` scrubbed
**called the delegate** (num_turns 3), got `PONG` from gpt-oss, and reported it.

Success criteria:
- `tanglebrain "prompt"` routes via the frontier-first router **by default**; `--local` forces the
  C1 local path; `--model <id>` still overrides.
- The selected orchestrator is invoked with the delegate MCP server available + tool allowed.
- Injection is **config-driven** (roster field), per the §5 ethos — adding/adjusting per-CLI is a
  config edit, not a code change.
- Live-verified: claude (proven) delegates; codex + gemini best-effort verified (their flags set
  per `--help`; gemini needs a one-time `gemini mcp add`, documented).

---

## 2. Architecture

**Delegate MCP spec (mcp-free, in `tanglebrain/delegate.py`):**
- `delegate_mcp_config_json() -> str`: the claude-style `{"mcpServers":{"tanglebrain-delegate":
  {"command": <sys.executable>, "args": ["-m","tanglebrain.mcp_server"]}}}`. Using `python -m` (not
  the console script) makes it robust regardless of PATH/venv.

**Config-driven injection (roster):**
- New optional `Invoke.delegate_args: list[str]` — extra argv appended to the orchestrator's `cmd`
  when delegation is enabled. Tokens substituted at runtime:
  - `{delegate_mcp_json}` → `delegate_mcp_config_json()`.
- Roster values (from the live `--help` probes):
  - claude: `["--mcp-config","{delegate_mcp_json}","--allowedTools","mcp__tanglebrain-delegate__delegate_local","--strict-mcp-config"]` (proven).
  - codex: `["-c","mcp_servers.tanglebrain_delegate.command=...","-c","mcp_servers.tanglebrain_delegate.args=[...]"]` (TOML overrides; verify live).
  - gemini: `["--allowed-mcp-server-names","tanglebrain-delegate","--approval-mode","yolo"]` — **plus a one-time `gemini mcp add tanglebrain-delegate -- <py> -m tanglebrain.mcp_server`** (gemini has no per-invocation server-config flag). Documented in README.

**CliAdapter** (`adapters/cli.py`):
- `__init__(..., delegate_args=None, inject_delegate=False)`; `from_entry(entry, inject_delegate=False)`
  reads `entry.invoke.delegate_args`.
- In `run`: effective cmd = `self.cmd + substituted(delegate_args)` when `inject_delegate`; then
  the existing `build_argv` applies `{prompt}`/append. (Delegate flags land before the prompt arg.)

**selector.build_adapter(entry, inject_delegate=False)** → threads to `CliAdapter.from_entry`.
**Router** enables delegation: builds adapters with `inject_delegate=True` (orchestrators get the
tool). Add a `Router(..., inject_delegate=True)` default; keep injectable for tests.

**CLI default flip** (`cli.py`):
- `run_once(..., model=None, local=False, task=None)`: precedence `model` → `local` → **router (default)**.
- Flags: add `--local` (force C1 local path). `--route` stays accepted (now the default; help marks
  it redundant). `--task` unchanged.

---

## 3. Tests

Hermetic — extend `tests/test_cli_adapter.py` + `test_router.py` + `test_cli.py`:
- `delegate_mcp_config_json` is valid JSON naming the server + `-m tanglebrain.mcp_server`.
- CliAdapter with `inject_delegate=True` appends substituted `delegate_args` (and `{delegate_mcp_json}`
  is replaced); with it false, argv is unchanged. `from_entry` carries `delegate_args`.
- Router builds adapters with `inject_delegate=True` (patch `build_adapter`, assert the kwarg).
- CLI: default path now calls the Router (not local-first); `--local` forces local; `--model` still
  wins; `--task` threaded.

Gated live — extend `tests/test_live.py`:
- claude: routed via the router (or direct) **calls the delegate** and the key is scrubbed (combine
  with the C2b/C2 proofs). codex + gemini: best-effort (skip if binary/registration absent).

Manual definition-of-done: `tanglebrain "decompose-and-delegate task"` end-to-end with the default
(no flags) routing to an orchestrator that offloads to local.

---

## 4. Docs / process
- **CHANGELOG** `### Added` (+ note the **default behavior change** prominently — it's user-visible;
  still a minor bump, but call it out).
- **README**: default is now frontier-first; `--local` to force local; gemini `mcp add` prerequisite;
  how delegation works.
- **Docstrings** on new functions/params.
- **Plan §10**: mark C3b shipped; **Closes #7**.
- **Independent Critic review**; address findings.
- **Janitor**; **MEMORY.md** (default flipped; next = C4 measurement).
- **Branch + PR**; **merge MANUALLY** (private/Free plan). `Closes #7`.

## Out of scope
- Measurement / "spend avoided" rollup → C4. Paid-API tier → #2. A `gemini mcp add` auto-installer
  (document the manual step instead). LangGraph (§9 — not needed; delegation is emergent).

## History
- 2026-06-16: Plan drafted; claude per-invocation injection proven live before building.
