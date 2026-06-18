<p align="center">
  <img src="https://raw.githubusercontent.com/Jason-Vaughan/project-assets/main/tanglebrain-logo-512.png" alt="TangleBrain logo" width="180">
</p>

# TangleBrain

[![CI](https://github.com/Jason-Vaughan/TangleBrain/actions/workflows/ci.yml/badge.svg)](https://github.com/Jason-Vaughan/TangleBrain/actions/workflows/ci.yml)

A **local-first, config-driven router across OpenAI-compatible backends you own.**

## The Problem: Cloud-by-Default Routing

Most AI tooling sends **every** request to a paid cloud API by default — even the trivial ones, even
when you already run capable models on hardware you own. The spend accrues invisibly, you're coupled
to a single provider's endpoint, and the moment you want to blend a local model, an authenticated CLI
you already pay for, and a bring-your-own-key API, you end up hand-wiring glue and editing source just
to change *where* a request goes. There's no single, plain place to declare "here are the backends I
have — route across them, in this order," and no measurement of what you're actually spending versus
avoiding.

**This is routing debt: vendor lock-in, invisible spend, and routing logic that lives in code instead
of config.**

## The Solution: A Local-First Router You Own

TangleBrain keeps the whole roster of backends in one editable YAML file and routes each request to
the backend you've configured — a free local model server by default. It favors credentials you
already hold: your **local models** and your **authenticated, OAuth-logged-in tools** come first,
while **raw API keys stay a separate, explicitly-gated opt-in** rather than the default (it never
injects a key into a CLI — your tool uses its own session). An optional classifier can **read each
request and route by its complexity** — sending the grunt work to your free local model where it's
cheap, and reserving heavier backends for what actually needs them. And every routed task is logged
with an **estimated cloud-equivalent cost**, so you can see what you're spending versus avoiding.
Adding or removing a backend is a config edit, not a code change.

## Standalone, or part of the Tangle family

TangleBrain runs entirely on its own — clone it, point it at your backends, and go. It's MIT-licensed
and open to contributors: forks and pull requests are welcome. It's also designed to drop in
seamlessly alongside [TangleClaw](https://github.com/Jason-Vaughan/TangleClaw) and the wider **Tangle
family** of tools, so it works the same whether you run it solo or as part of that ecosystem.

**Status:** v0.10.0 — first public release.

## What it does

- **Local-first routing** — ships pointing at a free local model server; nothing leaves your machine
  unless you configure a backend that does.
- **OAuth- and local-first credentials** — prefers your local models and your authenticated
  (OAuth-logged-in) tool sessions; it never injects an API key into a CLI. The raw-API-key tier is a
  deliberate opt-in, off by default behind explicit gates.
- **Prompt-aware routing** — an optional classifier reads each request and sends trivial / grunt work
  straight to your free local model where it's cheap, reserving heavier backends for what actually
  needs them (off by default, fails safe).
- **Config-driven roster** — every routable backend is one entry in a plain YAML list; add, remove,
  or reorganize backends by editing config.
- **Pluggable CLI-backed orchestration** — drive authenticated command-line tools as orchestrators,
  with rotation and failover across them for resilience.
- **Local sub-task delegation** — an orchestrator can offload bulk sub-tasks to the local backend
  through an MCP tool, then review the results.
- **Cost measurement** — every routed task is logged with an estimated cloud-equivalent cost;
  `tanglebrain --stats` rolls up what you've spent versus avoided.
- **Knob GUI** — a localhost panel to view the roster, pricing, and rollup, edit a focused set of
  config knobs, and run a prompt.
- **Gated paid-API tier** — bring-your-own-key overflow, off by default behind two independent
  switches.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the pieces fit together, [`CHANGELOG.md`](CHANGELOG.md)
for development history, and [`DISCLAIMER.md`](DISCLAIMER.md) for the opt-in / bring-your-own-key
posture.

## Tiers

| Tier | Example | Default |
|---|---|---|
| **Free local** | a local model via Ollama / any OpenAI-compatible server you run | **active** |
| **Subscription / authenticated CLI** | command-line tools you've installed and logged in (e.g. `claude -p`, `codex exec`, `gemini -p`) | opt-in (commented) |
| **Paid API** | bring-your-own-key overflow (any OpenAI-compatible endpoint you hold a key for) | opt-in, off by default |

> **Opt-in adapters & your responsibility.** The subscription / authenticated-CLI tier and the
> paid-API tier are **opt-in** — you enable them by editing your own roster. Driving an authenticated
> CLI is your responsibility under that provider's Terms of Service, and the paid tier is
> bring-your-own-key. Read [`DISCLAIMER.md`](DISCLAIMER.md) before enabling either.

## Install

Requires Python ≥ 3.10.

```sh
make venv          # create .venv and install -e . (dev deps included)
```

## Use

The roster of routable backends is a plain, editable YAML list — adding or removing a backend is a
config edit, not a code change. The shipped
[`tanglebrain/config/roster.yaml`](tanglebrain/config/roster.yaml) is only a **generic example** with
a single active entry (a local Ollama backend); keep your real roster **outside the repo** so updates
never clobber it. It's auto-discovered in order: `$TANGLEBRAIN_ROSTER` →
`~/.config/tanglebrain/roster.yaml` → the packaged example. Copy the example to
`~/.config/tanglebrain/roster.yaml` and edit it there (or pass `--roster <path>`).

```sh
# Route to the free local backend directly — works out of the box once a local server is running:
.venv/bin/tanglebrain --local "Write a haiku about local inference."

# Show the cost-avoided rollup across every routed task so far:
.venv/bin/tanglebrain --stats
```

The default `tanglebrain "…"` (no `--local`) uses the **orchestrator router**. Since the packaged
roster ships with no active orchestrators, that path needs at least one opt-in backend enabled first
— see below. Until then, use `--local` for the local backend.

### Orchestrator routing (opt-in)

Enable one or more orchestrator backends by uncommenting an entry in your roster (subscription /
authenticated-CLI examples are provided, commented out, in the shipped roster) and reading
[`DISCLAIMER.md`](DISCLAIMER.md) first. With at least one orchestrator active:

```sh
# Default: route through an orchestrator. Rotates across the configured orchestrators and fails over
# on error; an orchestrator can offload sub-tasks to a configured backend (see "Delegate (MCP)"):
.venv/bin/tanglebrain "Refactor this module and add tests."
.venv/bin/tanglebrain --task code "Refactor this function for clarity."   # task-fit hint

# Force a specific roster entry (explicit override, bypasses the router):
.venv/bin/tanglebrain --model my-backend "Summarize this long document."

# Opt into the local classifier gate for this run (trivial → local backend, else router):
.venv/bin/tanglebrain --gate "What's the capital of France?"
```

An orchestrator is any roster entry flagged `can_orchestrate: true`. The router prefers an
orchestrator whose `good_at` matches the `--task` hint, rotates across the eligible set for
resilience, and on an error fails over to the next; if all fail it reports each failure.

### Classifier gate (optional, off by default)

By default every (non-`--local`) request goes through the router. You can put a **cheap local
classifier in front**: it rates each request's complexity on the local backend and sends **trivial**
work straight to the local backend, while **frontier** work falls through to the router. Enable it
persistently with `classifier_gate_enabled: true` in
[`tanglebrain/config/settings.yaml`](tanglebrain/config/settings.yaml), or per run with `--gate` /
`--no-gate`. It is **off by default** and **fails safe** — any classifier error or ambiguity routes
to frontier, so a hard task is never trapped on the local tier. (Fail-safe covers the
*classification*; a trivial-classified task that then fails to execute on local surfaces that error,
the same as `--local` — it doesn't silently re-route.) Gated runs show up as `gate-local` in
`--stats`. If the gate ever seems to route *everything* to frontier, the classify call is likely
truncating — raise its token budget.

### Cost avoided (measurement)

Every routed task is logged as one JSON line in an append-only usage log
(`~/.cache/tanglebrain/usage.jsonl`, or under `TANGLEBRAIN_STATE_DIR`): path, tier, model,
estimated tokens, and the **cloud-equivalent cost it avoided** — what the work would have cost on a
paid frontier API. `tanglebrain --stats` rolls those records up into a single figure.

Tokens are *estimated* with a uniform `chars/4` heuristic over the visible prompt + response — the
authenticated CLIs expose no usable token counts, so one consistent (if approximate) methodology is
applied to every tier. The reference frontier price lives in
[`tanglebrain/config/pricing.yaml`](tanglebrain/config/pricing.yaml) — tune it to whatever frontier
model you want to compare against. A `placeholder` flag makes the rollup render a PLACEHOLDER caveat
when the rates are rough. Logging is best-effort and never affects the returned answer.

### Knob panel (`tanglebrain-gui`)

A thin **localhost-only** web panel over the config — zero extra dependencies (stdlib `http.server`
+ a single vanilla HTML/CSS/JS page):

```sh
.venv/bin/tanglebrain-gui          # serves http://127.0.0.1:3250/  (Ctrl-C to stop)
.venv/bin/tanglebrain-gui --port 3260   # override the port if 3250 is busy
```

The panel **views** the roster, the pricing reference, and the cost-avoided rollup, and lets you
**run a prompt** through the router (showing which tier/model served it). The **pricing card is
editable** — change the rates / reference label / placeholder flag and Save; it writes the tracked
`tanglebrain/config/pricing.yaml` (strict validation, atomic write, a backup to the state dir, and
the methodology header preserved), so the edit is git-visible for you to commit. The **roster is
editable for a focused set of per-entry fields** — `enabled`, `can_orchestrate`, `budget_usd_month`,
and `good_at` (each row has its own Save). Edits are surgical and **comment-preserving**: only the
targeted value on the targeted line changes, so the curated inline comments and the nested `invoke`
block survive byte-for-byte (same validate → backup → atomic-write safety as pricing; the candidate
is re-parsed before any write). Adding/removing entries and editing the `invoke` block are still
hand-edits. The panel binds `127.0.0.1` only: running a prompt spends real backend quota and it reads
the roster, so it is never network-exposed. The roster view shows each entry's `key_ref` as the
reference string only — secrets are never resolved or sent to the browser.

### Delegate (MCP) — let an orchestrator offload sub-tasks to a configured backend

`tanglebrain-delegate` is an MCP server that lets an orchestrator offload bulk sub-tasks instead of
running them itself, then review the results — a decompose → delegate → review loop that is emergent
from the orchestrator simply having the tool (no graph engine required). It reuses the same roster +
adapters as the CLI above, so endpoints and keys live in one place. It exposes four tools:

- **`delegate_local(prompt, max_tokens?)`** — route a sub-task to the free local tier (the $0
  default).
- **`delegate(prompt, target?, task?, max_tokens?)`** — route a sub-task to a *configured* backend,
  two ways (precedence: `target` > `task` > local):
  - **`target`** — an explicit roster id flagged `can_delegate: true`. The orchestrator names the
    exact backend.
  - **`task`** — a capability tag (a `good_at` value, e.g. `code`). TangleBrain picks the **cheapest
    `can_delegate` backend** good_at it (`local` before `sub`); the orchestrator just says *what kind
    of work it is* and doesn't need to know ids. **Paid `api` backends are never auto-selected by
    `task`** (reach one only by naming it as `target`). If nothing fits, the tool hands the sub-task
    **back to the orchestrator to do itself** — not an error, just a signal that it's the most capable
    backend available.

  A target is invoked as a leaf (it never gets its own delegate tool — no recursion); `api` targets
  named explicitly still obey the billing gate, so a paid target raises rather than spending while
  billing is off.
- **`delegate_many(tasks, max_concurrency?)`** — fan **several sub-tasks out concurrently** in one
  call and collect them, instead of delegating one at a time. Each item is `{prompt, target?, task?,
  max_tokens?}` (same routing as `delegate`), so a batch can mix backends. Returns a JSON array, one
  entry per task **in input order**, each `{index, status}` — `ok` (+`text`), `no_fit` (+`message`),
  or `error` (+`error`); a failing sub-task never sinks the others. Concurrency is bounded
  automatically from the host (`os.cpu_count()`), overridable by the operator
  (`delegate_max_concurrency` in `settings.yaml` — pin it to your backend's real parallelism, e.g.
  `OLLAMA_NUM_PARALLEL`) and lowerable per call. Dispatch + collect only — the orchestrator
  synthesises the results.
- **`delegate_targets()`** — list the configured targets (`id`, `tier`, `good_at`, `cost`, `kind`)
  so the orchestrator can decide based on what's available. The `delegate` tool's description also
  enumerates them (built at server startup; the tool reflects the live roster).

Make a backend a delegate target by flagging its roster entry `can_delegate: true` (mirrors
`can_orchestrate`). The shipped example flags the local tier, so the menu is non-empty out of the
box. **Note:** non-local delegate spend is **not metered** in this version — orchestration-tree
observability is on the [scatter-gather roadmap](https://github.com/Jason-Vaughan/TangleBrain/issues/39).
Any non-local target is opt-in and your responsibility under that provider's terms — see
[DISCLAIMER.md](DISCLAIMER.md).

**Synthesising fan-out results.** The full pattern is decompose → fan out (`delegate_many`) →
**reduce** → answer. TangleBrain ships the dispatch primitives but deliberately does *not* own the
reduce step: the orchestrator gets the results array back and combines it itself, because it holds
the original task context that makes for good synthesis — something a fresh reduce backend lacks. If
the reduction is instead **mechanical and large** (concatenating generated files, merging many
summaries into one list — where the original intent doesn't matter), offload that stitch too with a
normal `delegate(prompt="Combine these results: …", task="summarization")` call, keeping the heavy
formatting off your frontier budget. No separate "reduce" tool is needed — the existing `delegate`
covers it.

It needs the optional `mcp` dependency:

```sh
pip install -e ".[delegate]"        # or: make venv (installs the extra)
tanglebrain-delegate                # serve over stdio (for a manual smoke test)
```

Register it with an orchestrator CLI (exact flags vary by CLI version — check `<cli> mcp --help`):

```sh
# Claude Code:
claude mcp add tanglebrain-delegate -- tanglebrain-delegate
# Gemini CLI:
gemini mcp add tanglebrain-delegate tanglebrain-delegate
# Codex: add a stdio MCP server entry pointing at `tanglebrain-delegate` in its MCP config.
```

To point the server at a non-default roster, set `TANGLEBRAIN_ROSTER=/path/to/roster.yaml` in its
environment.

### Paid-API tier (opt-in, off by default)

Paid API is the genuine last resort — it costs real money, so it is **disabled by default** and
gated by a single explicit switch. A `tier: api` roster entry parses and is inspectable at all
times, but it is **never routable** until you turn it on. See [`DISCLAIMER.md`](DISCLAIMER.md) for
the bring-your-own-key posture.

The durable rule: *no paid billing without the explicit toggle.* Two independent gates must both be
on for a paid entry to build:

1. **Global gate** — `api_billing_enabled: true` in `tanglebrain/config/settings.yaml` (ships
   `false`).
2. **Per-entry switch** — `enabled: true` on the roster entry (a per-key kill-switch).

Custody is **by reference, never embedding**: TangleBrain never holds a raw key in config —
`key_ref` points at an env var (`env:OPENAI_API_KEY`) or a `0600` key file
(`file:~/.config/tanglebrain/keys/paid.key`). Prefer fronting paid APIs through a budget-capped
gateway or a scoped key so spend is bounded **at the source**. A paid entry also records
`budget_usd_month` for visibility — TangleBrain does **not** enforce spend; cap it at your
gateway/provider. A commented example entry is at the bottom of `tanglebrain/config/roster.yaml`.

Once both gates are on, a paid entry runs either when selected explicitly (`--model <id>`) or as the
router's **genuine last resort** — the default `tanglebrain "…"` router falls through to an enabled
`api` entry only after *every* orchestrator has failed/exhausted. It tries paid entries in roster
order and never paid-routes a roster that has no orchestrators to exhaust first.

> **Live status:** the paid tier is **hermetically tested but never run against a real paid
> endpoint** — by design (TangleBrain is deliberately bring-your-own-key; we don't mint billable keys
> just to test). The hooks are in place and the routing/gating/visibility are proven; the live
> `router → ApiAdapter → key → provider` round-trip is unverified until an operator wires a real key.
> See [#23](https://github.com/Jason-Vaughan/TangleBrain/issues/23). Treat it as hermetically correct
> but live-unproven, and file a fix if a live provider needs one.

#### Runbook — enabling a paid key

1. **Get a key for any OpenAI-compatible endpoint you control** — a provider directly, OpenRouter, or
   a self-hosted gateway (e.g. LiteLLM). Prefer a **budget-capped / scoped** key so spend is bounded
   at the source; TangleBrain doesn't enforce spend itself.
2. **Store it outside the repo.** Reference an env var (`key_ref: env:OPENAI_API_KEY`) or a `0600`
   file (`*.key` is gitignored):
   ```sh
   install -m 600 /dev/stdin ~/.config/tanglebrain/keys/paid.key <<< 'sk-your-key'
   ```
3. **Add the roster entry** (uncomment/adapt the example at the bottom of your roster): `tier: api`,
   `invoke.kind: api`, `base_url` = your endpoint, `model` = the model id it exposes,
   `key_ref` = the env/file reference above, `enabled: true`, and `budget_usd_month: 25`
   (display-only — match what you capped at the source).
4. **Flip the global gate**: set `api_billing_enabled: true` in `tanglebrain/config/settings.yaml`.
5. **Verify** in the knob panel (`tanglebrain-gui`): the roster card shows a **Paid-API billing: ON**
   banner and the entry's `budget: $25.00/mo` note; or run `tanglebrain --model <id> "…"` for an
   explicit paid call. To pause spend without editing keys, set the entry's `enabled: false` (a
   per-key kill-switch) or flip the global gate back to `false`.

## Develop

```sh
make help          # list targets
make lint          # smoke-check every Python file parses
make test          # lint + run the unit test suite (hermetic; HTTP is mocked)
make test-live     # opt-in: hit the real local endpoint your roster points at, end-to-end
```

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, branch/PR
conventions, and good first contributions (adding a backend is usually a config edit, not a code
change). All participation is governed by our [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), and the
opt-in / bring-your-own-key posture is in [`DISCLAIMER.md`](DISCLAIMER.md).

## License

[MIT](LICENSE).
