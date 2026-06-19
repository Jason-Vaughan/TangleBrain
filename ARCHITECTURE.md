# Architecture

TangleBrain is a **local-first, config-driven router across OpenAI-compatible backends you own.**
A request enters through one front door, the router decides which configured backend should serve
it, an adapter performs the call, and the result is logged and returned. Everything routable is
described in a plain editable config file — adding or removing a backend is a config edit, not a
code change.

This document describes the architecture as of **v0.15.0**. Per-change history lives in
[`CHANGELOG.md`](CHANGELOG.md); how to run it lives in [`README.md`](README.md); the opt-in /
bring-your-own-key posture lives in [`DISCLAIMER.md`](DISCLAIMER.md).

## High-level shape

```
            ┌──────────────────────────────────────────────┐
 prompt ───▶│  CLI (tanglebrain)  /  GUI (tanglebrain-gui)  │
            └──────────────────────────────────────────────┘
                                │
                  ┌──────────────┴──────────────┐
                  │  (optional) classifier gate  │   trivial ─▶ local tier
                  └──────────────┬──────────────┘
                                │ frontier
                                ▼
            ┌──────────────────────────────────────────────┐
            │                  Router                       │
            │  selects an orchestrator (task-fit), rotates  │
            │  across orchestrators, fails over on error    │
            └──────────────────────────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────────┐
        ▼                       ▼                            ▼
  openai-compat            cli adapter                  api adapter
  (local server)        (authenticated CLI)        (gated paid backend)
        │                       │                            │
        │            delegate (MCP) ◀── orchestrator offloads sub-tasks to a configured target
        ▼                       ▼                            ▼
            ┌──────────────────────────────────────────────┐
            │   measurement: append one record per task     │
            └──────────────────────────────────────────────┘
                                │
                                ▼
                          final answer
```

## Components

### Roster — the config model (`roster.py`)

The roster is a plain YAML list of **entries**, each describing one routable backend. An entry has
an `id`, a `tier` (`local` / `sub` / `api`), an `invoke` block, and routing hints (`good_at`,
`can_orchestrate`). The `invoke.kind` selects the adapter (`openai-compat` / `cli` / `api`) and
carries kind-specific fields. Credentials are referenced, never embedded: `key_ref` is a *reference*
(`env:NAME` or `file:PATH`), resolved lazily at call time.

The active roster is auto-discovered so a fresh clone works out of the box and an operator's real
config lives outside the repo (a `git pull` never clobbers it). Resolution order, first hit wins:

1. `$TANGLEBRAIN_ROSTER` (explicit path)
2. `~/.config/tanglebrain/roster.yaml` (XDG user config)
3. the packaged example (`tanglebrain/config/roster.yaml`)

`default_roster_path()` implements that order; `packaged_roster_path()` always points at the bundled
example. The packaged default ships **one active entry — the free local tier** — with the
subscription-CLI and paid-API tiers present as commented opt-in examples.

### Adapters — uniform call surface (`adapters/`)

Every backend is reached through an adapter exposing one method, `run(prompt, opts) -> text`, so the
router treats all tiers the same and adding a tier is contained to one adapter. Errors normalize to
`AdapterError` (`adapters/base.py`).

- **`openai_compat.py` — `OpenAICompatAdapter`.** HTTP POST to any OpenAI-compatible
  `/chat/completions` endpoint (a local server such as Ollama, a self-hosted gateway, etc.). Returns
  the assistant `content`.
- **`cli.py` — `CliAdapter`.** Drives an installed, authenticated command-line tool as a subprocess
  (**never via a shell**). The prompt is substituted into a `{prompt}` token in the configured `cmd`
  or appended as the final argument; `invoke.parse` selects an output parser; `invoke.scrub_env`
  strips named variables from the subprocess environment so the call uses the intended credential
  path. `invoke.delegate_args` lets the adapter hand the backend the local-delegate tool (below).
- **`api.py` — `ApiAdapter`.** A subclass of the openai-compat adapter for paid endpoints (which are
  themselves OpenAI-compatible). Same transport, different *policy*: it is built only when the paid
  gate is on (see below).

`selector.build_adapter(entry, ...)` is the single factory that maps an entry to its adapter and
enforces the paid gate.

### Classifier gate — optional front filter (`classifier.py`)

An optional pre-router step that rates a request's complexity on the free local tier and sends
**trivial** work straight to local, letting **frontier** work fall through to the router. It is
**off by default**, toggled by `settings.classifier_gate_enabled` or per-run `--gate` / `--no-gate`,
and **fails safe**: any error or ambiguity routes to frontier, so a hard task is never trapped on
the local tier.

### Router — orchestrator selection (`router.py`)

The router turns one prompt into one answer by choosing among **orchestrator** backends — entries
flagged `can_orchestrate: true`. Orchestrators are **pluggable and config-driven**; the set is
whatever the roster declares.

- **Task-fit selection.** When a `task` hint is given, the router prefers orchestrators whose
  `good_at` matches it, falling back to the full set otherwise.
- **Rotation.** It rotates the orchestrator role across the eligible set for resilience and even
  load. The rotation cursor is persisted across processes at
  `~/.cache/tanglebrain/router-state.json` (override with `TANGLEBRAIN_STATE_DIR`) and only advances
  on success.
- **Failover.** On an `AdapterError` it fails over to the next orchestrator; if all fail it raises
  `RouterError` listing each failure (rate-limit errors are annotated).
- **Paid last resort.** If every orchestrator fails *and* the paid gate is on, the router falls
  through to enabled `tier: api` entries in roster order. This is the genuine last resort; a paid
  success does not advance the orchestrator rotation cursor.

`Router.last_served` surfaces the entry that served a request so the CLI can meter it without
changing `route()`'s return type.

### Delegate — sub-task offload (`mcp_server.py`, `delegate.py`)

`tanglebrain-delegate` is an MCP server an orchestrator registers to offload bulk sub-tasks, then
review the results — a decompose → delegate → review loop that is emergent from the orchestrator
simply *having* the tool (no graph engine required). It reuses the same roster + adapters as the rest
of the system, so endpoints and key references live in exactly one place. MCP is an optional install
extra (`pip install -e ".[delegate]"`). Four tools:

- `delegate_local(prompt, max_tokens?)` — route to the free local tier (the $0 default).
- `delegate(prompt, target?, task?, max_tokens?)` — route to a *configured* backend. Precedence
  `target` > `task` > local:
  - `target` — an explicit roster id flagged `can_delegate: true` (mirrors `can_orchestrate`); the
    model names the backend.
  - `task` — a capability tag; `_select_by_capability` picks the cheapest `can_delegate` target whose
    `good_at` contains it (`TIER_RANK` `local` < `sub`, ties by declared order), mirroring the
    request-level router's task-fit at the sub-task level. **`api` is never auto-selected by `task`**
    (the ratified paid-is-last-resort-never-preferred invariant). No fit raises `NoDelegateFit` — a
    *signal*, not an error: the MCP tool catches it and returns an instruction for the orchestrator to
    handle the sub-task itself.
  - The selected target is built as a **leaf** (`inject_delegate=False`) — no recursive delegation.
    `api` targets named explicitly flow through the same billing gate, so a paid target stays inert
    unless billing is enabled.
- `delegate_many(tasks, max_concurrency?)` — fan several sub-tasks out **concurrently** and collect
  them. `run_delegate_many` runs each item through `run_delegate` on a
  `concurrent.futures.ThreadPoolExecutor` (the calls are sync + I/O-bound — plain Python, no graph
  engine). Per-item routing (each carries its own `target`/`task`), so a batch can mix backends.
  Results are returned **in input order** with per-item `status` (`ok`/`no_fit`/`error`); a failing
  sub-task never sinks the batch. Concurrency is bounded by `_effective_concurrency`: the operator's
  `settings.delegate_max_concurrency` if set, else a system-derived `os.cpu_count()` default, and a
  per-call `max_concurrency` may lower (never raise) it. Dispatch + collect only — synthesis is the
  orchestrator's.
- `delegate_targets()` — the configured menu (`id`, `tier`, `good_at`, `cost`, `kind`), so the
  orchestrator can also route explicitly by fit. The `delegate` tool's description enumerates the
  menu, built once at server startup.

These complete the [scatter-gather roadmap](https://github.com/Jason-Vaughan/TangleBrain/issues/39):
the orchestrator routes a sub-task to any configured backend (by id or capability) and fans batches
out concurrently. The **reduce step is deliberately not TangleBrain's** — the orchestrator synthesises
the `delegate_many` results itself (it holds the original task context that makes for good synthesis),
and offloads the stitch with an ordinary `delegate(task=…)` call when the reduction is mechanical and
large. No dedicated reducer tool: the existing primitives cover it, and frontier-side synthesis is the
better default until usage proves otherwise. Delegated sub-calls are **metered** (see Measurement
below) as a by-backend breakdown, and each is **linked back to the specific top-level task that spawned
it** across the process boundary (the per-parent-task tree, below) — so the roadmap is complete, not
just core-complete.

### Measurement — per-task records (`measurement.py`)

Each routed task is appended as one JSON line to `~/.cache/tanglebrain/usage.jsonl` (honoring
`TANGLEBRAIN_STATE_DIR`): the path taken, the tier and model that served it, estimated token counts,
and a **cloud-equivalent cost figure** — what the same work would have cost on a paid frontier API,
using the reference price in `config/pricing.yaml`. `tanglebrain --stats` rolls those records up.
Tokens are *estimated* with a uniform `chars/4` heuristic over the visible prompt + response (the
authenticated CLIs expose no usable counts), so one consistent approximate methodology applies to
every tier. All measurement I/O is best-effort: logging never breaks routing, and a corrupt record
never breaks the rollup.

Records carry a `kind`: `"task"` for a top-level routed request (what the spend-avoided headline
counts) or `"delegate"` for a sub-call offloaded through `run_delegate` (every delegation, including
each `delegate_many` item, is metered at that single seam). The rollup keeps delegate records **out of
the headline** — the parent task already credits the whole job, so counting the sub-calls again would
double-count the saving — and aggregates them **separately** into a by-backend breakdown (count, est
tokens, informational cloud-equiv) surfaced in `--stats` and the GUI. Concurrent appends (delegate_many
fans out across threads) are serialized by a process-level lock. Each delegation is also linked to its
*specific* parent task across processes (a true tree): the CLI mints a `task_id` per routed task and
injects it as `TANGLEBRAIN_TASK_ID` into the orchestrator's environment, the orchestrator forwards it
to the MCP delegate child, and `run_delegate` reads it back to stamp each delegate record's
`parent_task_id`. The rollup groups delegates `by_parent` (a "Linked to" tree in `--stats` and the
GUI); a sub-call run outside a propagated task is `unlinked`. The orchestrator-forwards-env hop is
verified live (claude), not hermetically — a delegation that loses the env degrades safely to
`unlinked`, never an error.

### Knob GUI — localhost panel (`gui/`)

`tanglebrain-gui` serves a thin, **localhost-only** web panel (stdlib `http.server` + a single
vanilla HTML/CSS/JS page, zero extra runtime dependencies). It **views** the roster, the pricing
reference, and the cost-avoided rollup, and lets you **run a prompt** through the router. The
**pricing** card and a focused set of per-entry **roster** fields (`enabled`, `can_orchestrate`,
`budget_usd_month`, `good_at`) are editable, with strict validation, an atomic write, a timestamped
backup, and comment-preserving write-back. It binds `127.0.0.1` only — running a prompt spends real
backend quota and the panel reads the roster, so it is never network-exposed. Secrets are never
resolved or sent to the browser: a `key_ref` is shown as its reference string only.

`gui/views.py` holds pure, socket-free functions (testable directly); `gui/server.py` wraps them in
a pure `dispatch(method, path, body)` plus a `ThreadingHTTPServer`.

### Settings & paid gate (`settings.py`, `config/settings.yaml`)

A small global settings file holds two boolean switches, both **off by default** and both validated
strictly (a non-bool value can never coincidentally enable a feature):

- `api_billing_enabled` — the paid-API gate. A `tier: api` entry parses and is inspectable at all
  times but is **never routable** until this is on *and* the entry's own `enabled` flag is on (two
  independent gates). The durable rule: *no paid billing without the explicit toggle.*
- `classifier_gate_enabled` — the classifier gate described above.

## Entry points

| Console script | Module | Purpose |
|---|---|---|
| `tanglebrain` | `cli.py` | Route a prompt (default), or `--local` / `--model <id>` / `--gate` / `--stats`. |
| `tanglebrain-gui` | `gui/server.py` | Serve the localhost knob panel. |
| `tanglebrain-delegate` | `mcp_server.py` | Serve the `delegate_local` / `delegate` / `delegate_many` / `delegate_targets` MCP tools over stdio. |

## Design notes

- **Config-driven, not code-driven.** Backends, orchestrators, task-fit hints, and pricing all live
  in editable config; the code is the routing machinery, the config is the policy.
- **Plain Python.** No agent-graph framework — the orchestration loop is emergent from giving an
  orchestrator the delegate tool. A graph library remains an option only if the control flow ever
  grows genuinely branchy.
- **Credentials by reference.** No raw secret ever lives in config or in the repo; `key_ref` points
  at an environment variable or a `0600` file, resolved lazily at call time.
- **Fail-safe defaults.** The classifier gate and the paid gate are both off by default and fail
  toward the safe behavior (frontier routing; never billing).
