# C5a — Knob GUI (read-only panel + run + stats), TangleClaw-style

**Chunk:** C5a (first slice of plan §10 "C5 — Knob GUI"). **Bump:** minor (new user-facing surface).
**Issue:** file `[feature] C5a — read-only knob panel` before building; the editable half becomes
a separate `[feature] C5b` issue. (No C5 issue exists yet.)

> **Step 0 (project rule):** copy this plan to
> `/Users/jasonvaughan/Documents/Projects/TangleBrain/.claude/plans/c5a-knob-gui.md`
> and reference that project-local absolute path in memory/handoffs.

## Context

Plan §9.2/§10 call for an "editable parameters" web panel over the §5 config — TangleClaw web-UI
style — so the PM can see and tune the router without reading code. C5 is the last build chunk
before the deferred paid-API tier (#2). It's large and splittable, so this chunk ships the safe,
high-value **read-only** slice; the risky **editable** half (YAML write-back + re-validation +
secret-safety) is deferred to **C5b**.

This panel is a **local operator dashboard**, distinct from the public portfolio `costSaved` stat
that Monad-1's `monad-stats` publishes. It surfaces the local C4 `--stats` rollup, the live roster,
pricing, and a "run a prompt → see which tier served it" box.

## Decisions (per Decision Framework)

1. **Stack: stdlib `http.server` + a single-file vanilla HTML/CSS/JS page. Zero new runtime deps.**
   Alts (Flask / FastAPI) rejected — a thin localhost panel over a few config files doesn't justify
   a web framework, and zero-dep matches both TangleBrain's lean ethos (httpx + PyYAML only) and
   TangleClaw's own zero-dependency UI. No `[gui]` extra needed; just a new console script.
2. **`ThreadingHTTPServer`** (not the default single-threaded server): `/api/run` can block for
   seconds on a sub CLI subprocess; threading keeps the rest of the panel responsive.
3. **Bind `127.0.0.1` only.** The panel can spend real sub rate-limit quota (it runs prompts) and
   reads the roster — it must never be network-exposed. Localhost single-user → no auth.
4. **Read-only this chunk.** No write-back to `roster.yaml`/`pricing.yaml` (→ C5b).
5. **Secret-safety:** the API serves `key_ref` as the *reference string only* (e.g. `file:…`,
   `env:NAME`) exactly as stored — it is never resolved and key file contents are never read/served.
6. **Testability:** HTTP handlers are thin wrappers over pure functions returning dicts
   (`view_roster()`, `view_pricing()`, `view_stats()`, `run_prompt(payload)`), so tests exercise the
   logic hermetically with no socket bind and no network (mirrors `cli.py` being thin over `run_once`).

## Reused backend (no new logic needed for reads)

- `tanglebrain/roster.py` — `load_roster()`, `Roster.entries`, `RosterEntry`/`Invoke` (serialize via
  `dataclasses.asdict`, then drop/normalize nothing except confirming `key_ref` stays a ref string).
- `tanglebrain/measurement.py` — `load_pricing()`, `read_records()`, `rollup()` (the panel renders
  the summary dict as HTML; `format_rollup` stays the CLI renderer).
- `tanglebrain/cli.py` — `run_once(prompt, model?, local?, task?)` for the run box (it already meters
  each run via C4, so panel runs feed the stats automatically).

## Implementation

### 1. New package `tanglebrain/gui/`
- `tanglebrain/gui/__init__.py`.
- `tanglebrain/gui/views.py` — pure functions, fully unit-testable, each returns JSON-able dicts:
  - `view_roster() -> dict` — entries with tier/cost/good_at/can_orchestrate/invoke; `invoke` includes
    kind/base_url/model/parse but **key_ref shown as the raw ref string only** (documented redaction).
  - `view_pricing() -> dict` — reference_model, input/output per-Mtok, is_placeholder.
  - `view_stats() -> dict` — `rollup(read_records())` plus the pricing label/placeholder flag.
  - `run_prompt(payload: dict) -> dict` — validate `{prompt, task?, local?, model?}`, call `run_once`,
    return `{ok, text}` or `{ok: false, error}` (catch the same exceptions `main()` catches:
    RosterError/SelectionError/RouterError/AdapterError). Empty prompt → 400-shaped error dict.
- `tanglebrain/gui/server.py` — `ThreadingHTTPServer` + a `BaseHTTPRequestHandler` that routes:
  - `GET /` → the static page; `GET /api/roster|pricing|stats` → `json.dumps(view_*())`.
  - `POST /api/run` → parse JSON body, `run_prompt(...)`, return JSON.
  - `main(argv=None)` console entry: `--port` (default from env/PortHub), `--host` (default 127.0.0.1).
    Prints the URL on start. Returns exit code.
- `tanglebrain/gui/static/index.html` — single file, inline CSS+JS, TangleClaw aesthetic: black bg
  (`#000`), card bg (`#0D0D0D`), lime accent (`#8BC34A`), Apple system fonts, mobile-first. Sections:
  roster table, pricing card, spend-avoided stats card, and a run box (prompt textarea + optional
  task hint + "force local" checkbox + Run button → shows the response and the served tier/model).
  Packaged via `package-data` (add `gui/static/*.html`).

### 2. Wiring
- `pyproject.toml`: add console script `tanglebrain-gui = "tanglebrain.gui.server:main"`; extend
  `[tool.setuptools.package-data]` with `"gui/static/*.html"`. No new dependency, no `[gui]` extra.
- **PortHub**: register a permanent port in the 3200–3999 project range (target ~3250; query
  `GET /api/ports` first and pick a free one). Use the TangleClaw API at `https://localhost:3102`
  per CLAUDE.md (`POST /api/ports/lease`, `permanent:true`, `-k` for the mkcert cert). NB: the
  PortHub source uses `/api/leases`; **verify the exact live endpoint at build time** and follow
  whichever the running daemon answers. The chosen port becomes the `--port` default.

### 3. Tests — `tests/test_gui.py` (hermetic; no socket, no network)
- `view_roster` redaction: `key_ref` appears as its ref string; assert no secret resolution and no
  unexpected key-content read. `view_pricing`/`view_stats` shapes (mock `read_records`/`load_pricing`).
- `run_prompt`: happy path (mock `run_once` → text); empty prompt → error dict; each backend
  exception → `{ok: false, error}` (mock `run_once` to raise RouterError/AdapterError).
- Routing: instantiate the handler with a fake `rfile`/`wfile` (or factor a `dispatch(method, path,
  body) -> (status, body)` helper and test that) — unknown path → 404; `/api/run` bad JSON → 400.

### 4. Docs / runbook
- `tanglebrain/gui/README.md` (or a README "Knob panel" section + a `.claude/` runbook): how to launch
  (`tanglebrain-gui`), the registered port, the localhost-only note, what's read-only vs coming in C5b.
- `CHANGELOG.md` `### Added` (minor). README Status: add C5a; note C5b (editable) + #2 still pending.

## Verification

- `make test` — full hermetic suite stays green incl. new `test_gui.py`.
- Manual: `tanglebrain-gui` → open `http://127.0.0.1:<port>/` → roster/pricing/stats render; run a
  prompt with "force local" and confirm the response + served tier show, and that the run appended a
  record (`tanglebrain --stats` count increments / `usage.jsonl` grew). Confirm `key_ref` shows only
  the ref string in the roster view (view source / network tab).
- Confirm the PortHub lease is registered (`GET` the ports list shows the TangleBrain entry).

## Wrap

Independent Critic review before merge. Branch `feat/c5a-knob-gui` → PR with What/Why/Test-plan,
`Closes #<C5a>`. Repo PRIVATE on Free → **merge MANUALLY** (`gh pr merge <N> --squash
--delete-branch`), never `--auto` (and this is a feature → wait for PM sign-off regardless). File the
**C5b** issue (editable knobs) before wrap so the deferred half is tracked. Update `MEMORY.md`
(C5a shipped; next = C5b). One chunk — do not start C5b.
