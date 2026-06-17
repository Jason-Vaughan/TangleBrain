# TangleBrain

A **cost-tiered LLM router**. TangleBrain routes each task to the cheapest tier that can
actually do it — **free local first → flat-rate subscriptions → paid API only as a last
resort** — to drive ongoing compute cost down.

> **North star:** the optimization target is *not* `$/token`. It's tier-fit plus rate-limit
> spread: use the free local model whenever it suffices, spread orchestration across the
> flat-rate subscription CLIs (Claude / Codex / Gemini) to stay under each one's rate limit,
> and reach for a paid API only when nothing cheaper will do.

Positioning: *"OpenRouter, but we own the routing logic and back it with our own flat-rate
subscriptions instead of paying per-token."*

## Tiers

| Tier | Example | Marginal cost |
|---|---|---|
| **Free local** | `gpt-oss-120b` via LiteLLM on local (over the tailnet) | $0, unlimited |
| **Sub** | `claude -p`, `codex exec`, `gemini -p` | $0 at margin; rate-limit bound |
| **Paid API** | (later, explicit, opt-in) | per-token — last resort only |

## Status

**Frontier-first routing is live end-to-end, and routed tasks now report their "spend avoided."**
`tanglebrain "…"` rotates an orchestrator sub, fails over on rate limits, and offloads grunt to
free local gpt-oss; `tanglebrain --stats` rolls up the cloud-equivalent cost avoided; and a
localhost knob panel (`tanglebrain-gui`) shows it all in the browser and lets you edit the pricing
knob. The paid-API tier is complete — gated, last-resort-routed, and visible in the panel — but ships
**off by default** (issue #2, C6a–C6c). Remaining: roster editing in the panel (later).

- ✅ **C0** — frontier-first decompose spike (shipped in coordinator, verdict KEEP).
- ✅ **C1** *(this repo)* — package skeleton, roster config loader, openai-compat adapter,
  one request → local → text end-to-end.
- ✅ **C2** — CLI adapters for the three subs (claude/codex/gemini) with `ANTHROPIC_API_KEY` scrub.
- ✅ **C2b** — gpt-oss MCP local-delegate: a `delegate_local` MCP tool an orchestrator calls to
  offload grunt to free local gpt-oss.
- ✅ **C3** — frontier-first router (control plane): task-fit orchestrator selection + rotation +
  429 failover across the subs.
- ✅ **C3b** — orchestrators offload grunt to free local via the delegate; **frontier-first is now
  the default** (`tanglebrain "…"`); `--local` forces the direct local tier.
- ✅ **C4** — measurement / "spend avoided" rollup: every routed task logged, `tanglebrain --stats`
  reports cloud-equivalent cost avoided.
- ✅ **C5a** — knob GUI (read-only): `tanglebrain-gui` serves a localhost panel to view the roster,
  pricing, and spend-avoided rollup, and run a prompt through the router.
- ✅ **C5b** — editable pricing in the panel (validated, atomic, comment-preserving write-back to
  `pricing.yaml`). Roster editing still to come.
- ✅ **C6a** — paid-API tier scaffolding: an `api` adapter (LiteLLM-fronted) behind a global
  `api_billing_enabled` gate (**default off**) plus a per-entry `enabled` kill-switch. A `tier: api`
  entry parses but is **never routable** until both are on.
- ✅ **C6b** — last-resort paid-API routing: the router falls through to an enabled `api` entry only
  after every sub has failed **and** the gate is on. Off by default → the router never reaches paid.
- ✅ **C6c** — paid-API visibility: the knob panel surfaces each entry's `enabled`/`budget_usd_month`
  and a **Paid-API billing: ON/OFF** banner, plus a README runbook for minting a LiteLLM virtual key.

Full plan: [`.claude/plans/tanglebrain.md`](.claude/plans/tanglebrain.md). The historical
orchestration contract (frozen, superseded by the plan) lives in [`TANGLEBRAIN.md`](TANGLEBRAIN.md).

## Install

Requires Python ≥ 3.10.

```sh
make venv          # create .venv and install -e . (dev deps included)
```

## Use

The roster of routable models is a plain, editable YAML list
([`tanglebrain/config/roster.yaml`](tanglebrain/config/roster.yaml)) — adding or removing a
model is a config edit, not a code change.

```sh
# Default: frontier-first router. Rotates the orchestrator across the subs (claude/codex/gemini)
# for ~3x rate-limit runway, fails over on 429, and the orchestrator offloads grunt to free local:
.venv/bin/tanglebrain "Refactor this module and add tests."
.venv/bin/tanglebrain --task code "Refactor this function for clarity."   # task-fit hint

# Force the free local tier directly ($0, no orchestration):
.venv/bin/tanglebrain --local "Write a haiku about local inference."

# Force a specific roster entry (explicit override):
.venv/bin/tanglebrain --model gemini "Summarize this long document."

# Opt into the local classifier gate for this run (trivial → free local, else router):
.venv/bin/tanglebrain --gate "What's the capital of France?"

# Show the "spend avoided" rollup across every routed task so far:
.venv/bin/tanglebrain --stats
```

### Classifier gate (optional, off by default)

By default every request goes through the frontier-first router, consuming a sub's rate-limit
budget. When rotation alone isn't enough to stay under the sub limits, you can put a **cheap local
classifier in front** (plan §6): it rates each request's complexity on free local gpt-oss and sends
**trivial** work straight to free local (skipping the subs), while **frontier** work falls through to
the router. Enable it persistently with `classifier_gate_enabled: true` in
[`tanglebrain/config/settings.yaml`](tanglebrain/config/settings.yaml), or per run with `--gate` /
`--no-gate`. It is **off by default** and **fails safe** — any classifier error or ambiguity routes
to frontier, so a hard task is never trapped on the local tier. (Fail-safe covers the
*classification*; a trivial-classified task that then fails to execute on local surfaces that error,
the same as `--local` — it doesn't silently re-route.) Gated runs show up as `gate-local` in
`--stats`. If the gate ever seems to route *everything* to frontier, the classify call is likely
truncating — raise its token budget.

### Spend avoided (measurement)

Every routed task is logged as one JSON line in an append-only usage log
(`~/.cache/tanglebrain/usage.jsonl`, or under `TANGLEBRAIN_STATE_DIR`): path, tier, model,
estimated tokens, and the **cloud-equivalent cost it avoided** — what the work would have cost on a
paid frontier API. `tanglebrain --stats` rolls those records up into a single "spend avoided"
figure, the way the north star (drive ongoing compute cost *down*) becomes visible.

Tokens are *estimated* with a uniform `chars/4` heuristic over the visible prompt + response — the
subscription CLIs expose no usable token counts, so one consistent (if approximate) methodology is
applied to every tier. The reference frontier price lives in
[`tanglebrain/config/pricing.yaml`](tanglebrain/config/pricing.yaml) and mirrors coordinator's
`usage-stats` `costSaved` anchor — Claude Sonnet at $3/$15 per MTok (methodology ratified
2026-06-13) — so both projects value avoided spend identically. A `placeholder` flag makes the
rollup render a PLACEHOLDER caveat if the anchor is ever forked before re-ratifying. Logging is
best-effort and never affects the returned answer.

### Knob panel (`tanglebrain-gui`)

A thin **localhost-only** web panel over the config — TangleClaw-style, zero extra dependencies
(stdlib `http.server` + a single vanilla HTML/CSS/JS page):

```sh
.venv/bin/tanglebrain-gui          # serves http://127.0.0.1:3250/  (Ctrl-C to stop)
.venv/bin/tanglebrain-gui --port 3260   # override the port if 3250 is busy
```

Port **3250** is registered for TangleBrain in TangleClaw PortHub. The panel **views** the roster,
the pricing reference, and the local spend-avoided rollup, and lets you **run a prompt** through the
router (showing which tier/model served it). The **pricing card is editable** — change the rates /
reference label / placeholder flag and Save; it writes the tracked `tanglebrain/config/pricing.yaml`
(strict validation, atomic write, a backup to the state dir, and the methodology header preserved),
so the edit is git-visible for you to commit. The **roster is editable for a focused set of per-entry
fields** — `enabled`, `can_orchestrate`, `budget_usd_month`, and `good_at` (each row has its own
Save). Edits are surgical and **comment-preserving**: only the targeted value on the targeted line
changes, so the curated inline comments and the nested `invoke` block survive byte-for-byte (same
validate → backup → atomic-write safety as pricing; the candidate is re-parsed before any write).
Adding/removing entries and editing the `invoke` block are still hand-edits. The panel binds
`127.0.0.1` only:
running a prompt spends real subscription rate-limit quota and it reads the roster, so it is never
network-exposed. The roster view shows each entry's `key_ref` as the reference string only — secrets
are never resolved or sent to the browser.

The router gives each orchestrator the `delegate_local` tool, so a frontier sub decomposes the
task and offloads grunt to free local gpt-oss at $0, then reviews — the cost lever behind
frontier-first (plan §6). claude and codex are wired per-invocation; **gemini needs a one-time
registration** so it can see the delegate:

```sh
gemini mcp add tanglebrain-delegate -- "$(pwd)/.venv/bin/python" -m tanglebrain.mcp_server
```

The adapter calls the local LiteLLM endpoint directly. It needs the scoped LiteLLM key; by
default it reads `~/.config/tanglebrain/tanglebrain-spike.key` (referenced from the roster entry —
never hardcoded, never committed).

### Local delegate (MCP) — let an orchestrator offload grunt to free local

`tanglebrain-delegate` is an MCP server exposing one tool, `delegate_local(prompt, max_tokens?)`,
that routes to the free local tier (gpt-oss-120b). A frontier orchestrator (claude/codex/gemini)
registers it and offloads bulk sub-tasks at $0 instead of burning its own rate-limited tokens —
the mechanism behind frontier-first decompose (plan §6). It reuses the same roster + adapter as
the CLI above, so the endpoint and key live in one place.

It needs the optional `mcp` dependency:

```sh
pip install -e ".[delegate]"        # or: make venv (installs the extra)
tanglebrain-delegate                # serve over stdio (for a manual smoke test)
```

Register it with an orchestrator CLI (exact flags vary by CLI version — check
`<cli> mcp --help`):

```sh
# Claude Code:
claude mcp add tanglebrain-delegate -- tanglebrain-delegate
# Gemini CLI:
gemini mcp add tanglebrain-delegate tanglebrain-delegate
# Codex: add a stdio MCP server entry pointing at `tanglebrain-delegate` in its MCP config.
```

To point the server at a non-default roster, set `TANGLEBRAIN_ROSTER=/path/to/roster.yaml` in
its environment.

### Paid-API tier (opt-in, off by default)

Paid API is the genuine last resort — it costs real money, so it is **disabled by default** and
gated by a single explicit switch. A `tier: api` roster entry parses and is inspectable at all
times, but it is **never routable** until you turn it on.

The durable rule: *no paid billing without the explicit toggle.* Two independent gates must both be
on for a paid entry to build:

1. **Global gate** — `api_billing_enabled: true` in `tanglebrain/config/settings.yaml` (ships
   `false`).
2. **Per-entry switch** — `enabled: true` on the roster entry (a per-key kill-switch).

Custody is **LiteLLM-fronted**: TangleBrain never holds a raw provider key. You mint a
budget-scoped **virtual key** in LiteLLM on local and reference it via `key_ref` (e.g.
`file:~/.config/tanglebrain/tanglebrain-gpt5.key`); the raw provider key stays in LiteLLM. A paid entry
also records `budget_usd_month` for visibility — in this version the **hard budget cap is enforced
LiteLLM-side** on the virtual key, not by TangleBrain. A commented example entry is at the bottom of
`tanglebrain/config/roster.yaml`.

Once both gates are on, a paid entry runs either when selected explicitly (`--model <id>`) or as the
router's **genuine last resort** — the default `tanglebrain "…"` router falls through to an enabled
`api` entry only after *every* orchestrator sub has failed/exhausted (C6b). It tries paid entries in
roster order and never paid-routes a roster that has no subs to exhaust first.

> **Live status:** the paid tier is **hermetically tested but never run against a real paid
> endpoint** — by design (TangleBrain is deliberately anti-key; we don't mint billable keys just to
> test). The hooks are in place and the routing/gating/visibility are proven; the live
> `router → ApiAdapter → virtual key → provider` round-trip is unverified until an operator wires a
> real key. See [#23](https://github.com/Jason-Vaughan/TangleBrain/issues/23). Treat it as
> hermetically correct but live-unproven, and file a fix if a live provider needs one.

#### Runbook — enabling a paid key (LiteLLM-fronted)

1. **Mint a budget-scoped virtual key in LiteLLM on local.** This is where the hard cap lives —
   TangleBrain never enforces spend itself. On the local LiteLLM host:
   ```sh
   curl -s http://litellm.example:4000/key/generate \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
     -d '{"models": ["gpt-5"], "max_budget": 25, "budget_duration": "30d", "key_alias": "tanglebrain-gpt5"}'
   ```
   This returns a virtual key (`sk-…`) scoped to one model with a hard $25 / 30-day cap enforced
   LiteLLM-side. (Adjust `models` / `max_budget` / `budget_duration` to taste.)
2. **Store the virtual key on this Mac, never in the repo.** Write it `0600` and reference it by
   path — `*.key` is gitignored:
   ```sh
   install -m 600 /dev/stdin ~/.config/tanglebrain/tanglebrain-gpt5.key <<< 'sk-the-returned-virtual-key'
   ```
3. **Add the roster entry** (uncomment/adapt the example at the bottom of
   `tanglebrain/config/roster.yaml`): `tier: api`, `invoke.kind: api`, `base_url` = the LiteLLM
   endpoint, `model` = the alias, `key_ref: file:~/.config/tanglebrain/tanglebrain-gpt5.key`,
   `enabled: true`, and `budget_usd_month: 25` (display-only — must match what you set LiteLLM-side).
4. **Flip the global gate**: set `api_billing_enabled: true` in `tanglebrain/config/settings.yaml`.
5. **Verify** in the knob panel (`tanglebrain-gui`): the roster card shows a **Paid-API billing: ON**
   banner and the entry's `budget: $25.00/mo` note; or run `tanglebrain --model gpt-5 "…"` for an
   explicit paid call. To pause spend without editing keys, set the entry's `enabled: false` (a
   per-key kill-switch) or flip the global gate back to `false`.

## Develop

```sh
make help          # list targets
make lint          # smoke-check every Python file parses
make test          # lint + run the unit test suite (hermetic; HTTP is mocked)
make test-live     # opt-in: hit the real local endpoint end-to-end (needs the scoped key)
```

## License

[MIT](LICENSE).
