# Boundaries

Contract surfaces where components interact. When a change crosses one of these, investigate
consumer impact **before** you finish the work — not after.

**What makes this project's boundaries unusual:** most consumers are outside this repo and cannot be
migrated in lockstep — a published PyPI package, an orchestrator *model* reading tool descriptions,
and arbitrary OpenAI clients. There is no "update both sides in one commit" option for the surfaces
marked **external** below. Full contracts live in [`api-contract.md`](api-contract.md).

## Contract surfaces

### HTTP — OpenAI-compatible serve endpoint ⚠️ **external**

- **Producer:** `tanglebrain/serve/views.py` (pure translation), `serve/server.py` (dispatch + socket)
- **Consumer:** any OpenAI-compatible client, anywhere. Will never read our docs.
- **Contract:** `POST /v1/chat/completions`, `GET /v1/models`. Request/response shapes are
  **OpenAI's, not ours** — compatibility is defined upstream. Errors are
  `{"error": {message, type, code}}` via `error_envelope`.
- **Crossing it means:** the `model` routing directive semantics, the error taxonomy, or the
  streaming contract (one flushed SSE event per delta; mid-stream failure ends with an
  `{"error": ...}` event and **no `[DONE]`**).

### MCP — delegate tool surface ⚠️ **external, hardest consumer**

- **Producer:** `tanglebrain/mcp_server.py` (tool definitions + descriptions),
  `tanglebrain/delegate.py` (routing logic)
- **Consumer:** an orchestrator **model**. It reads tool descriptions rather than documentation,
  cannot ask a clarifying question, and cannot be retrained when a signature changes.
- **Contract:** the four tool signatures, the `target` > `task` > local precedence, per-item `status`
  values (`ok` / `no_fit` / `error`), input-order results, and `NoDelegateFit` surfacing as an
  instruction rather than an error.
- **Crossing it means:** any signature change, **and any change to a tool description** — the
  description *is* the contract for this consumer. A misleading description causes silent
  misrouting, not an error.
- **Trap:** the `delegate` description enumerating the target menu is built **once at server
  startup**. A roster change is invisible to a running server.

### CLI flags ⚠️ **external**

- **Producer:** `tanglebrain/cli.py`
- **Consumer:** operators' shell history, scripts, aliases.
- **Contract:** flag names and semantics. Precedent (`--route`) is that a superseded flag becomes an
  accepted no-op rather than being removed.

### Roster config schema ⚠️ **external**

- **Producer:** `tanglebrain/roster.py` (`RosterEntry`, `Invoke`)
- **Consumer:** hand-authored operator YAML living **outside the repo**, plus `roster_edit.py` and
  the GUI.
- **Contract:** field names, types, and defaults. **A roster `id` is doubly public** — it is the
  `model` value on the HTTP surface and the `target` value on the MCP surface.
- **Crossing it means:** renaming or removing a field breaks operator configs that no migration can
  reach. New fields must be optional with safe defaults.

### Adapter interface *(internal)*

- **Producer:** `tanglebrain/adapters/base.py`
- **Consumer:** `router.py`, `selector.py`, `delegate.py`
- **Contract:** `run(prompt, opts) -> text`; failures normalize to `AdapterError`; streaming is an
  **optional** `run_stream` capability, never assumed.
- **Crossing it means:** widening the base interface — which forces every adapter to implement or
  stub a capability most backends lack.

### Usage record schema *(internal, but forward-compatible)*

- **Producer:** `tanglebrain/measurement.py`
- **Consumer:** the `--stats` rollup, the GUI, **and every record written by an older version**.
- **Contract:** always-present fields plus optional `task_id` / `parent_task_id` / `origin`. A
  missing field reads as "not applicable", never as an error.
- **Crossing it means:** adding a required field, or changing the meaning of `spend_avoided_usd` or
  the `task` vs `delegate` kind split that keeps the headline from double-counting.

### Lifetime totals file — `totals.json` *(internal, forward-compatible)*

- **Producer:** `tanglebrain/totals.py` — the format, its reader and its writer.
- **Consumer:** `rollup()` in `tanglebrain/measurement.py`, reached by both `--stats` and the GUI
  panel, **and every version of TangleBrain that shares the file.**
- **Contract:** an unknown key is ignored, a missing key reads as zero, and an absent or corrupt
  file reads as all-zeros — never an error. There is deliberately no schema-version field, so
  additive-only is the whole compatibility story and nothing else can arbitrate a conflict.
  Because of that, the **write** side is non-destructive: a field this version does not recognise
  is carried through from the file being replaced, at any depth, so an older TangleBrain's
  compaction cannot delete what a newer one wrote. It cannot *maintain* such a field either — the
  value goes stale rather than being lost, and that is the contract's stated limit, not an
  oversight. Every write is atomic *and* durable — staged beside the target, fsynced, then renamed, with the directory synced after (POSIX only). Atomicity alone would order the compaction's two writes against a killed process but not against a power loss.
- **Crossing it means:** removing or repurposing a field, or folding a value whose cardinality is
  unbounded — the delegates' `by_parent` tree is excluded for exactly that reason, and a figure
  moving between lifetime and window scope has to land in both renderers or the panel and the CLI
  disagree. The rollup's field list is built from this format's own declarations rather than
  restated, so the two cannot drift; a test pins the one thing that construction cannot, a key the
  rollup introduces on its own.

### Cross-process correlation *(internal, unenforceable)*

- **Producer:** `cli.py` (mints `task_id`, injects `TANGLEBRAIN_TASK_ID`)
- **Consumer:** `run_delegate`, via an orchestrator process **we do not own**
- **Contract:** the env var name, and that a missing value degrades to `unlinked` rather than
  raising.
- **Crossing it means:** renaming the variable silently unparents every delegation with no error
  anywhere. See [`architecture.md`](architecture.md).

### GUI panel endpoints *(internal, localhost-only)*

- **Producer:** `tanglebrain/gui/views.py` + `server.py`
- **Consumer:** `gui/static/*.html` (vanilla JS, same repo — the one boundary where both sides move
  together)
- **Contract:** JSON shapes, and the **fixed allow-list of editable roster fields** (`enabled`,
  `can_orchestrate`, `budget_usd_month`, `good_at`) which is the mass-assignment control. Widening
  it is a security change.

### Plugin manifest *(external)*

- **Producer:** `.claude-plugin/marketplace.json` → `plugins/tanglebrain-delegate/`
- **Consumer:** Claude Code's plugin loader
- **Contract:** manifest schema plus the console-script name. Guarded by
  `tests/test_plugin_manifest.py`.

### Packaging *(external)*

- **Producer:** `pyproject.toml`
- **Consumer:** every `pip install`
- **Contract:** console-script names, the optional `delegate` extra, dependency constraints,
  `requires-python`.
- **Crossing it means:** the v0.20.1 failure class. Guarded by `tests/test_packaging.py`, which asserts
  both ends of the `mcp` major (`>= 2, < 3`) — **update that test deliberately when a floor or
  ceiling moves; never delete it to green a build.** Verify from a clean venv against real PyPI,
  since a source checkout has the dependency already importable and cannot see the break.

## Test levels

| Level | Exists | When to run | Location |
|---|---|---|---|
| Unit | Yes | Every change | `tests/test_*.py` — hermetic, HTTP mocked at the adapter seam |
| Integration | Partial | Changes crossing internal boundaries | `tests/test_serve.py`, `test_gui.py` exercise `dispatch()` end-to-end without sockets |
| Contract | Yes | Packaging, plugin, or API-surface changes | `tests/test_packaging.py`, `tests/test_plugin_manifest.py`, `tests/test_openai_compat.py` |
| End-to-end | Opt-in | Before release, and after any adapter change | `tests/test_live.py`, gated by `TANGLEBRAIN_LIVE=1` (`make test-live`) |

## Coverage gaps

Recorded, not fixed.

1. **No automated test covers the `TANGLEBRAIN_TASK_ID` orchestrator hop.** It is verified live
   against one orchestrator (Claude Code), by hand. It cannot be tested hermetically because the
   middle hop belongs to software this project does not own.
2. **No socket-level test for either HTTP server.** `dispatch()` is tested directly and the bind
   address is now asserted (`tests/test_bind_address.py`), but nothing exercises a real socket: the
   `ThreadingHTTPServer` wrapper is verified by substitution, so a request never traverses an actual
   connection. The highest-consequence part — that the bind is loopback and not configurable — is
   covered; end-to-end transport behaviour is not.
