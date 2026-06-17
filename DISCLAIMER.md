# Disclaimer

TangleBrain is a **local-first, config-driven router across OpenAI-compatible backends you own.**
Out of the box it ships with a single active backend — a free local model server — and routes only
there. Every other backend is **opt-in**: you enable it by editing your own roster config. This
document describes what you take responsibility for when you do.

## No warranty

TangleBrain is provided under the [MIT License](LICENSE) **"as is", without warranty of any kind.**
You are responsible for how you configure and run it, including which backends you connect and the
costs and obligations those backends carry.

## Subscription / authenticated-CLI adapters are your responsibility

TangleBrain can drive command-line tools you already have installed and logged in (for example
Claude Code, Codex, or the Gemini CLI) through its generic `cli` adapter. These entries ship
**commented out** — they are examples, not defaults. If you uncomment one, you choose to point
TangleBrain at that tool.

**Using those tools through TangleBrain is your responsibility under each provider's Terms of
Service.** TangleBrain does not bundle, authenticate, or speak for any provider; it invokes whatever
command you configure with the credentials already present on your machine. Before enabling a `cli`
adapter, confirm that automating that tool the way you intend is permitted by the provider whose
service it uses. If a provider's terms prohibit a given use, don't configure TangleBrain to do it.

The `cli` adapter is intentionally generic: it runs a configured command as a subprocess (never via
a shell) and parses its output. It has no special knowledge of any particular vendor, and these
docs name specific CLIs only as configuration examples.

## Paid-API tier is bring-your-own-key and off by default

The paid-API tier costs real money, so it is **disabled by default** and gated by two independent
switches that must both be on for a paid backend to be reachable:

1. the global `api_billing_enabled` flag in `config/settings.yaml` (ships `false`), and
2. the per-entry `enabled` flag on the roster entry.

The durable rule is: **no paid billing without the explicit toggle.** A `tier: api` entry parses and
is inspectable at all times but is never routable until you turn both on.

When you do enable it, **you bring your own key.** TangleBrain never holds a raw secret — a
`key_ref` points at an environment variable or a `0600` file, resolved only at call time.
TangleBrain records a `budget_usd_month` for visibility but **does not enforce spend**; cap your
spend at the source (a budget-capped or scoped key, or a gateway that enforces a hard limit). You
are responsible for any charges your key incurs.

## Costs, rate limits, and data

Whatever backend you route to, **you own the relationship with that backend** — its costs, its rate
limits, its data-handling, and its terms. TangleBrain passes your prompts to the backend you
configured and returns the result; it adds no telemetry and sends nothing anywhere you didn't point
it. Review each backend's data and privacy terms before sending it anything sensitive.
