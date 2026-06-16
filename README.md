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
free local gpt-oss; `tanglebrain --stats` rolls up the cloud-equivalent cost avoided. Remaining:
the knob GUI (C5) and the gated paid-API tier (issue #2).

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
- ⬜ **C5** — knob GUI (thin web panel over the roster config).

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

# Show the "spend avoided" rollup across every routed task so far:
.venv/bin/tanglebrain --stats
```

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

## Develop

```sh
make help          # list targets
make lint          # smoke-check every Python file parses
make test          # lint + run the unit test suite (hermetic; HTTP is mocked)
make test-live     # opt-in: hit the real local endpoint end-to-end (needs the scoped key)
```

## License

[MIT](LICENSE).
