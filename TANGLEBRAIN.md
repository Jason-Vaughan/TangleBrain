> # ⚠️ SUPERSEDED — FROZEN PENDING RECONCILIATION
> 
> **This contract predates the cost-tier pivot and is NOT authoritative.** It is migrated
> here verbatim (TangleBrain C1, 2026-06-16) for historical fidelity and to preserve the
> still-open design questions — *not* as the spec to build against.
> 
> **Superseded by the canonical plan** ([`.claude/plans/tanglebrain.md`](.claude/plans/tanglebrain.md)):
> - **§2 — Where it runs.** This doc frames TangleBrain as a *Monad-embedded* intelligent
>   orchestrator (Layers 1–3 on Monad, classifier on the RTX A400). The plan moves it to
>   **Cursatory (the Mac), where the OAuth auth lives** — a one-way `TangleBrain → Monad`
>   dependency. The "multi-local-model routing / GPU-2 classifier" framing below is exactly
>   the drift the plan re-anchors away from.
> - **§6 — Routing.** This doc's `direct` / `smart-fallback` / `semantic-route` profile model
>   is replaced by **cost-tiered frontier-first routing with multi-orchestrator rotation**
>   (free local → flat-rate subs → paid API last; optimize tier-fit + rate-limit spread,
>   NOT $/token).
> 
> **Reconciliation trigger — RESOLVED 2026-06-16 (PM decision; see issue #2):**
> 1. ✅ The "no new API-key billing" rule is replaced by an explicit `api_billing_enabled`
>    flag, **default off** — a deliberate opt-in, not "always OK."
> 2. ✅ **Invariant #3 is reconciled inline below**: the hard ban becomes an explicit
>    `api_billing_enabled` toggle (**default off**). LiteLLM-fronted virtual keys are the
>    *preferred* custody; holding raw keys later is not foreclosed. The invariant **softens,
>    not reverses** — the durable rule is *no paid billing without the explicit toggle*.
> 
> The §2 (where it runs) and §6 (routing model) supersessions above remain historical — those
> are still superseded by the plan. Per "annotate, don't delete," the body is preserved.

# TangleBrain — Monad-1 intelligent orchestrator (contract)

TangleBrain is the **decision layer above raw inference** — the thing that takes a
request and points it at the right brain (local model, model-group with fallback, or,
later, a classifier-driven route). This doc is its **contract**: the stable interface
that clients and TangleClaw build against, *independent of which engine implements it*.

Injected as a TangleClaw shared doc into the `AI Inference` group so every project in
that group sees the orchestration contract automatically.

- **Implementation today** lives in [`LITELLM.md`](LITELLM.md) — the LiteLLM gateway is
  the *current* engine behind the contract. TangleBrain is the interface; LiteLLM is one
  implementation of it.
- **Federation** (cross-Monad) lives in the **TangleWeb** project (Layer 4). TangleBrain
  is the orchestrator on *one* Monad (Layers 1–3); TangleWeb is the network across them.
- **Canonical scope/roadmap:** [`.claude/plans/intelligent-routing-roadmap.md`](.claude/plans/intelligent-routing-roadmap.md).

> **Honest current state:** the "intelligence" ramps over time. Today the orchestration
> is mostly alias selection + (soon) fallback — not yet a classifier. The contract below
> is designed so intelligence can grow (Layer 1 → 2 → 3) *behind a fixed client edge*,
> without clients or TangleClaw rewiring. Naming reflects where it's going.

---

## The contract (three invariants)

Everything that builds on TangleBrain can rely on these three things, regardless of which
engine is behind the endpoint:

1. **OpenAI-compat at the client edge.** Every orchestration endpoint speaks
   `POST /v1/chat/completions`. A client cannot tell whether LiteLLM or a future
   LangGraph classifier is behind it — that's what makes the brain swappable.
2. **A project binds to a *profile*, not a model.** The bind target is a named
   orchestration profile that resolves to `(base_url, key_ref)` — never a hardcoded model
   id or URL. Swapping the engine behind a profile is a config change, not a rewrite.
3. **Cloud escalation signals up, never brokers down.** A local endpoint may *emit* a
   "needs frontier" signal; the **top orchestrator** (Claude Code / the CLI) handles the
   cloud hop on its own OAuth subscription. TangleBrain never holds cloud API keys and
   never proxies cloud calls. (Hard rule — no new API-key billing.)

> **↳ Reconciled 2026-06-16 (PM decision; plan §7 / §9.6–9.7, issue #2).** The "no API-key
> billing" hard rule is **replaced by an explicit gate, not an absolute ban**: paid-API billing is
> enabled only via an explicit `api_billing_enabled` flag (**default off** — not the intended
> default mode, but the feature is wanted: cheap keys found later, or other operators of this
> product). The **preferred custody is LiteLLM-fronted** — TangleBrain references a scoped LiteLLM
> virtual key (its existing `key_ref` mechanism), keeping the raw provider key in LiteLLM on Monad.
> Holding raw provider keys directly is **not foreclosed**, but it is never required and always
> stays behind the same toggle. The durable invariant: *no paid-API billing without the explicit
> toggle.* Paid API remains a last-resort tier (§6).

---

## Orchestration profiles

| Profile | Means | Engine today | Status |
|---|---|---|---|
| `direct` | Client picks a specific model alias; no routing | LiteLLM alias (e.g. `qwen2.5-coder-32b-fp16`) | **Live** (Layer 1) |
| `smart-fallback` | Pick a model-group; fall back to a backup on local error/timeout | LiteLLM model-groups (`smart-code` / `smart-chat`) | **Planned** — Layer 2, Monad-1 #33 |
| `semantic-route` | Classify the task, then route to the right model/chain | LangGraph classifier on the RTX A400, in front of LiteLLM aliases | **Planned, gated** — Layer 3, Monad-1 #35; built last, only if router-evals prove it beats plain `smart-fallback` |

All three speak OpenAI-compat at the edge. A profile that isn't built yet simply isn't
selectable; adding one later = pointing the profile at a new base_url, no client change.

---

## Escalation signal (marker LOCKED 2026-06-15)

Invariant #3 in concrete form. When a local endpoint determines a request needs frontier
capability, it emits a **top-level `tanglebrain` object** on the chat-completion response —
*not* a custom `finish_reason` (standard SDKs validate that against a fixed enum). Standard
OpenAI clients ignore the unknown key; a TangleBrain-aware harness matches on it.

Non-streaming → on the response object. Streaming → on the **terminal chunk** (the one
carrying `finish_reason`), before `data: [DONE]`. `finish_reason` stays a valid value.

```json
{
  "choices": [{ "message": {"role":"assistant","content":""}, "finish_reason": "stop" }],
  "tanglebrain": { "escalate": true, "reason": "needs_frontier",
                   "detail": "local classifier: task exceeds local tier",
                   "suggested_tier": "frontier" }
}
```

- **Field name is LOCKED to `tanglebrain`** (chosen over `x_tanglebrain`) — agreed
  Monad-1 ↔ TangleClaw 2026-06-15. The Layer-3 emitter (Monad-1 #35) MUST emit exactly this
  key; the harness recognizer (TangleClaw #358) matches `response.tanglebrain?.escalate === true`
  (incl. the last SSE chunk).
- **No emitter today** — LiteLLM only proxies. The emitter is the Layer-3 classifier (#35);
  until it lands the recognizer is a stub (recognize → surface/log, no frontier routing).

---

## Key-ref (no credential brokering)

TangleClaw binds a project with a **key reference**, never a raw secret it proxies:

- `key_ref: env:NAME` — the key is in the launched session's environment.
- `key_ref: file:PATH` — the key is a `0600` file the client reads.
- `key_ref: none` — open endpoint.

Mint a **scoped virtual key** per consumer via LiteLLM `POST /key/generate` (model
allowlist + TPM/RPM caps + budget); reference it. **Never inject the LiteLLM master key**
into a client — that's the footgun being retired (the master key stays on Monad-1, in
`/etc/litellm/litellm.env`).

---

## The bind contract (what TangleClaw does at launch)

TangleClaw is the launch-time binder. At session launch it resolves, per project:

```
project → orchestration profile → (base_url, key_ref)
```

and injects it into the client (e.g. Aider) via the engine profile's `launch.args` +
`launch.env` — set and forget. The client then talks OpenAI-compat to that endpoint for
the rest of the session. TangleBrain is **not** in TangleClaw's request path; it's the
endpoint the client is pointed at.

```
Lord JSON / Claude Code (top orchestrator, OAuth cloud) ◄── "needs frontier" signal ──┐
        │ delegates grunt work                                                         │
        ▼                                                                              │
TangleClaw launch-binder ── points the project at ONE endpoint (base_url + key_ref)   │
        ▼                                                                              │
TangleBrain endpoint ── speaks OpenAI-compat ── [ direct | smart-fallback | semantic ] ┘
        ▼
LiteLLM (Monad-1:4000) → Ollama (Monad-1:11434) → local model
```

---

## Addressing

Endpoints are addressed by **Tailscale MagicDNS FQDN** (e.g.
`http://monad-1.tail123678.ts.net:4000/v1`), **never a raw tailnet IP** — the hostname is
stable across IP changes, so a renumbered node is a zero-edit event. A raw `100.x` IP is a
**fallback only**, for a consumer that can't resolve MagicDNS (e.g. a Docker container not
using the host resolver); verify `getent hosts <fqdn>` *on the consumer* before cutover,
and cut consumers over one at a time. See [`LITELLM.md`](LITELLM.md) for the live endpoint.

---

## Status (2026-06-15)

- **Layer 1 / `direct`** — live (LiteLLM aliases, see `LITELLM.md`).
- **Layer 2 / `smart-fallback`** — not built (config-only change to LiteLLM; Monad-1 #33).
- **Layer 3 / `semantic-route`** — not started; gated on a router-eval surface (Monad-1 #35).
- **Replacement for FleetHub:** there is no model-axis registry. The model axis lives
  *behind* this endpoint on Monad; TangleClaw only needs one URL + one key per profile.
  (TangleClaw#332 / FleetHub was dropped 2026-06-15.)
