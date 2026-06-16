# Build Plan — C3: frontier-first router (control plane)

**Chunk:** C3 (plan §6/§10) · **Branch:** `feat/c3-router` · **Status:** APPROVED (router now;
delegate-injection split to C3b / issue #7).
**Predecessors:** C1 (local adapter), C2 (CLI adapters, #5), C2b (MCP delegate, #6).

---

## 0. Scope (the one fork — decided)

C3 = the **router control plane** only: task-fit orchestrator **selection** + round-robin
**rotation** (persisted) + 429/limit **failover** across the `can_orchestrate` subs. The
**delegate-injection** half (wire C2b's `delegate_local` into orchestrator runs; flip the CLI
default to frontier-first) is **C3b → issue #7** — deferred so the router stays deterministic and
we don't burn sub rate limits before delegation makes frontier-first cost-effective.

CLI default stays **local-first** this chunk; the router is exposed behind an explicit flag.

---

## 1. Goal

Implement §6's "multi-orchestrator rotation": route a task to a task-fit frontier sub, rotate the
orchestrator role across the subs for ~3× rate-limit runway, and fail over to the next sub on a
rate-limit/error. The win is real even without local delegation (that's the cost lever C3b adds).

Success criteria:
- `Router.route(prompt, task=None)` selects an orchestrator, runs it via the existing adapter, and
  returns text — choosing by task-fit when a `task` hint is given, else pure rotation.
- Rotation **persists across processes** (each `tanglebrain` run is a new process), so successive
  requests spread across the subs. Injectable state path; tests never touch the real location.
- On an orchestrator failure, failover advances through the remaining orchestrators once; if all
  fail, raise an aggregated error naming each failure.
- Lives in its **own module** (`tanglebrain/router.py`) — the C1 selector stays minimal (its
  docstring warns against growing into the router).

---

## 2. Architecture — `tanglebrain/router.py`

- **Candidate set**: `roster.orchestrators()` (entries with `can_orchestrate: true`), in declared
  order. Error if empty.
- **Task-fit**: if `task` (a `good_at` tag like `code` / `reasoning` / `long-context`) is given,
  candidates = orchestrators whose `good_at` contains it; if none match, fall back to all
  orchestrators (don't fail — task-fit is a preference, not a gate). Auto-classification of the
  task is **explicitly deferred** (§6: add a cheap local classifier gate "only if volume demands").
- **Rotation order**: start from a persisted cursor and walk the candidate list round-robin, so
  the starting orchestrator differs each call (load-spread). After a **successful** run, advance
  the cursor (persisted) past the orchestrator that served the request.
- **Failover**: try candidates in rotation order; on `AdapterError` from one, record it and try the
  next. A lightweight `_looks_like_rate_limit(msg)` (regex: `429`, `rate limit`, `quota`,
  `resource_exhausted`, `overloaded`) annotates the log line but failover happens on **any**
  `AdapterError` (safe superset — a dead orchestrator should also yield to the next). If all fail,
  raise `RouterError` aggregating each `(id, error)`.
- **State persistence** (`tanglebrain/router.py` or a tiny helper): a JSON file
  `{ "cursor": <int> }` under `TANGLEBRAIN_STATE_DIR` (default `~/.cache/tanglebrain/`). Missing or
  corrupt file → cursor 0 (never crash on bad state). Path is a constructor arg for tests.
- Reuses `build_adapter` (C2's selector) to invoke each orchestrator — no adapter logic here.

`RouterError(RuntimeError)` — new, in `router.py` (or reuse `SelectionError`? No — distinct: this is
"all orchestrators failed", a routing-exhaustion error). Carries the per-orchestrator failures.

---

## 3. CLI wiring (default unchanged)

- Add `--route` (use the frontier-first router) and `--task <kind>` (task-fit hint) to `cli.py`.
- `run_once` gains a `route: bool` / `task: str|None` path: when `--route`, build a `Router` and
  call `route(prompt, task)`; else the existing local-first / `--model` paths (unchanged).
- Document that `--route` becomes the default in C3b (#7), once delegate-injection lands.

---

## 4. Tests (mirror prior chunks: hermetic + a gated live)

Hermetic — `tests/test_router.py` (mock `build_adapter` → fake adapters; inject a temp state path):
- **selection**: task-fit picks matching orchestrators; unknown task falls back to all; empty
  orchestrator set → `RouterError`.
- **rotation**: cursor starts the walk at the right entry; advances after success; wraps around;
  persists to the state file and is re-read by a fresh `Router`.
- **failover**: first orchestrator succeeds (others untouched); first fails → second succeeds; all
  fail → `RouterError` naming each; cursor does **not** advance on total failure (or advances past
  the failed starting point — pick one, test it).
- **rate-limit classifier**: `_looks_like_rate_limit` true/false cases.
- **state file**: missing → cursor 0; corrupt JSON → cursor 0 (no crash); write/read round-trip;
  honors `TANGLEBRAIN_STATE_DIR`.
- **CLI**: `--route`/`--task` build a Router and call `route` (patch it); default path still
  local-first.

Gated live — extend `tests/test_live.py`: `Router.route("...")` returns text from some orchestrator
(skips per-CLI when a binary is absent, like the C2 live tests).

---

## 5. Docs / process

- **CHANGELOG** `[Unreleased] → ### Added` (router = new user-visible capability → **minor**).
- **Docstrings** on every function/class (core rule).
- **README**: note `--route`/`--task` and that frontier-first becomes default in C3b (#7).
- **Plan §10**: mark C3 (control plane) shipped; note C3b (#7) carries delegate-injection + default flip.
- **Independent Critic review**; address findings before merge.
- **Janitor**; **MEMORY.md** update (C3 shipped, next = C3b).
- **Branch + PR** `feat/c3-router`; **merge MANUALLY** (private/Free plan). Relates to #7 (not Closes).

## Out of scope (explicit)
- Delegate-injection + default flip → **C3b / #7**.
- Auto task-classification gate → §6 "only if volume demands" (future).
- Paid-API tier → #2. Measurement/savings → C4. LangGraph → §9 (revisit when the loop justifies it).

## History
- 2026-06-16: Plan drafted (C3 control plane; user continued in-session; scope confirmed router-now).
