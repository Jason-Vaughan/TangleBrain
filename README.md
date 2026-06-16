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
| **Free local** | `gpt-oss-120b` via LiteLLM on Monad (over the tailnet) | $0, unlimited |
| **Sub** | `claude -p`, `codex exec`, `gemini -p` | $0 at margin; rate-limit bound |
| **Paid API** | (later, explicit, opt-in) | per-token — last resort only |

## Status

**C1 — repo + skeleton + roster loader + the openai-compat adapter to free local gpt-oss.**
One request routes to the local tier end-to-end. This is the foundation; the cost-tiered
router itself (orchestrator selection, rotation, failover) arrives in later chunks.

- ✅ **C0** — frontier-first decompose spike (shipped in Monad-1, verdict KEEP).
- ✅ **C1** *(this repo)* — package skeleton, roster config loader, openai-compat adapter,
  one request → local → text end-to-end.
- ⬜ **C2** — CLI adapters for the three subs (with `ANTHROPIC_API_KEY` scrub).
- ⬜ **C3** — frontier-first router: orchestrator selection, rotation, 429 failover.
- ⬜ **C4** — measurement / "spend avoided" rollup.
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
# Run one request through the free local tier (gpt-oss-120b on Monad):
.venv/bin/tanglebrain "Write a haiku about local inference."
```

The adapter calls the Monad LiteLLM endpoint directly. It needs the scoped LiteLLM key; by
default it reads `~/.config/monad/tanglebrain-spike.key` (referenced from the roster entry —
never hardcoded, never committed).

## Develop

```sh
make help          # list targets
make lint          # smoke-check every Python file parses
make test          # lint + run the unit test suite (hermetic; HTTP is mocked)
make test-live     # opt-in: hit the real Monad endpoint end-to-end (needs the scoped key)
```

## License

[MIT](LICENSE).
