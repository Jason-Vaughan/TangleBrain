# TangleBrain — Plan (DISCUSS/PLAN phase)

**Status:** **C0 RAN 2026-06-16 → VERDICT: KEEP (see §11). Next session = C1 (repo + skeleton).**
Lives here in Monad-1
temporarily; **moves to its own repo at C1** (different machine, different purpose, one-way
dependency — see "Project home" below). North star + drift history: auto-memory
`project_tanglebrain_north_star.md`.

---

## 1. North star (why this exists)

Drive ongoing compute cost down to justify the ~$30K Monad build, by routing **each task to
the cheapest tier that can actually do it.** Not a local-model picker — a **cost-tiered router**
across local + flat-rate subs + (later, optional) paid APIs.

**Positioning:** "OpenRouter, but we own the routing logic and back it with our own flat-rate
subscriptions instead of paying per-token." Control + sub-backing + cost. OpenRouter itself stays
an *optional* tier we can add, not the engine.

---

## 2. Where it runs (Project home)

**TangleBrain is an app on Cursatory (the Mac), not embedded in Monad.** Reason: it must run
where the auth lives. The OAuth subs (Claude/Codex/Gemini) are logged in on the Mac; a headless
Monad box can't invoke them. The Mac is also the *best* vantage — it can reach **every** tier:
local gpt-oss over the tailnet, the OAuth CLIs locally, any cloud over the internet.

- **Cursatory (Mac):** TangleClaw (sessions) + **TangleBrain (router app)** + the OAuth CLIs.
- **Monad (tailnet):** gpt-oss-120b via LiteLLM = the free local tier TangleBrain calls.
- **Dependency direction:** TangleBrain → Monad (one-way). Monad never depends on TangleBrain.
- This **contradicts the old roadmap** ("Layer 3 on Monad") — that was for local-model routing;
  the cost-tier vision puts the router where the auth is. Fix the roadmap when this locks.

---

## 3. The roster + cost model (verified 2026-06-15)

Probe on Cursatory confirmed what's routable today:

| Tier | Model | Invocation | Auth | Marginal cost |
|---|---|---|---|---|
| **Free local** | gpt-oss-120b | LiteLLM `monad-1.tail123678.ts.net:4000/v1` | none | **$0, unlimited** |
| **Sub** | Claude Code | `claude -p --output-format stream-json` | Max **sub** | $0 at margin; **rate-limit bound** |
| **Sub** | Codex | `codex exec` | Codex **sub** | $0 at margin; rate-limit bound |
| **Sub** | Gemini CLI | `gemini -p --output-format json` | Google OAuth | $0 at margin; rate-limit bound |
| Paid API | (later, explicit) | API key | per-token | real $ — **last resort only** |

**Roster is OPEN/extensible.** Many headless OAuth CLIs exist; adding one is a config edit (§5),
not a code change. We can run a combination of all of them.

**Cost-model insight that drives routing:** subs are **flat-rate** — using them more is ~$0 until
you hit each one's **rate limit.** So the optimization target is NOT $/token; it's:
**free local first → spread across the flat-rate subs to stay under each cap → paid API only as a
genuine last resort.**

---

## 4. Architecture

```
TangleClaw session ─▶ TangleBrain (Cursatory)
                         │  reads roster config (§5)
                         │  decides tier per task (§6)
                         ├─▶ local:  LiteLLM → gpt-oss (free)          [tailnet]
                         ├─▶ sub:    claude -p / codex exec / gemini -p [local subprocess]
                         └─▶ api:    (optional, later)                 [internet]
                         │  loops results back for review / re-delegation
                         ▼
                       final answer ─▶ user
```

- **LangGraph is OPTIONAL.** It's a Python library for stateful, looping, branching agent graphs —
  earn-its-keep only if the flow is genuinely loopy (route → work → review → maybe re-route).
  A first version may be plain Python; adopt LangGraph when the loop complexity justifies it.
  TangleBrain is the product; LangGraph is (maybe) an implementation detail.
- Each tier is a **node/adapter** with one uniform interface (`run(prompt, opts) -> text`),
  so adding/removing a model is local and contained.

---

## 5. Flexible roster config (the modifiability requirement)

A simple, editable list the router reads — **not** a registry subsystem (we're not rebuilding
FleetHub). Each entry:

```yaml
- id: gpt-oss-120b
  tier: local
  invoke: { kind: openai-compat, base_url: "http://monad-1.tail123678.ts.net:4000/v1", model: "gpt-oss-120b" }
  cost: free
  good_at: [grunt, code, tools]
- id: claude
  tier: sub
  invoke: { kind: cli, cmd: ["claude","-p","--output-format","stream-json"], scrub_env: ["ANTHROPIC_API_KEY"] }
  cost: flat-rate          # rate-limit bound
  good_at: [reasoning, decomposition, review]
  can_orchestrate: true    # joins the frontier-first rotation (§6)
- id: codex
  tier: sub
  invoke: { kind: cli, cmd: ["codex","exec"] }
  good_at: [code, agentic-code]
  can_orchestrate: true
- id: gemini
  tier: sub
  invoke: { kind: cli, cmd: ["gemini","-p","--output-format","json"] }
  good_at: [long-context, structured-json]
  can_orchestrate: true
```

**Add/remove/reorganize = edit the list — this is the flexibility requirement, first-class.** A
future model (another OAuth CLI, an OpenRouter model, a paid API) is a new entry; flag it
`can_orchestrate: true` to put it in the §6 rotation, or leave it as a worker-only tier. `invoke.kind`
∈ {openai-compat, cli, api}. `scrub_env` enforces the §7 sub-vs-key safety rule per adapter. The GUI
panel (§9.2) edits exactly this list. **The three subs are the starting roster, never the ceiling.**

---

## 6. Routing decision logic (the heart — LOCKED 2026-06-16)

**Strategy: frontier-first decompose, with multi-orchestrator rotation. Evolve to a gated hybrid
ONLY if request volume forces it.**

A request goes to a **frontier sub as orchestrator** — it decomposes the task, delegates grunt to
free local gpt-oss (via the MCP tool, §10), reviews the results, and either re-delegates or
finalizes. The smart model makes the routing decision (reliable), and grunt work runs free.

**Why this beats a local-first gate now:** the only cost of frontier-first is consuming a sub's
rate limit per request — and we have **three flat-rate subs**, so we **rotate the orchestrator
role** across them (round-robin and/or task-fit) for ~3× the rate-limit runway. With three subs in
rotation, low/interactive volume likely never reaches the point where a gate is needed.

**Orchestrator selection (the logic you control, all config-driven — §5):**
- **Task-fit:** Codex → coding/agentic-code; Claude → reasoning/decomposition/review; Gemini →
  long-context / structured-JSON. Route the orchestrator by what the task needs.
- **Load-spread:** rotate among the orchestrator-capable subs to stay under each one's rate limit;
  on a 429/limit from one, fail over to the next.

**Orchestrators are an EXTENSIBLE set, not hardcoded.** Any roster entry flagged
`can_orchestrate: true` (§5) joins the rotation. Adding a future sub as an orchestrator = a config
edit, same as adding a worker model. The three we have are the starting rotation, not the ceiling.

**Evolution path (only if volume demands):** when measurement (§8) shows we're approaching rate
limits even with rotation, add a **cheap local classifier gate** in front — gpt-oss (or a tiny
model on the spare A400) doing only the narrow *classify* task (trivial→local vs needs-frontier),
NOT self-judging its own ability. That gate is the future "hybrid"; we build it only when the data
calls for it. Nothing here is permanent — measure, then decide.

---

## 7. Auth handling per tier (safety rules)

- **`claude -p` must run with `ANTHROPIC_API_KEY` scrubbed** from the subprocess env, so it rides
  the Max **sub** (flat) not the **API key** (per-token, billed). The env key (108 chars, present
  on Cursatory) would otherwise risk silent billing that defeats the cost goal. `scrub_env` in §5
  enforces this per adapter. (Also: that raw key in plain env is a hygiene item to revisit.)
- Codex/Gemini: rely on their own OAuth login state; no key injection.
- **Paid-API billing is gated by an explicit `api_billing_enabled` flag, default OFF** (decided
  2026-06-16 — §9.6/§9.7, issue #2). When off, `tier: api` entries parse but never route. When on,
  each paid key is a `tier: api` roster entry with a per-key enable toggle + budget cap, **fronted
  through LiteLLM** (TangleBrain references a scoped LiteLLM virtual key — preferred over holding a
  raw provider key, which is not foreclosed but stays behind the toggle). Paid API remains
  last-resort (§6).

---

## 8. Measurement (so the $30K visibly pays itself back)

TangleBrain logs each routed task: tier chosen, tokens, and an **estimated cloud-equivalent cost
avoided** (what this task *would* have cost on a frontier API). Roll up to a "spend avoided" figure
— same methodology family as monad-stats' `costSaved`. This is how you watch the savings accrue and
tune the routing thresholds.

---

## 9. Decisions

**Locked (2026-06-16):**
1. **Routing strategy** → frontier-first decompose + **multi-orchestrator rotation**; gated hybrid
   only if volume forces it (§6).
2. **Engine** → **plain Python first**; LangGraph deferred (adopt only if the loop grows branchy —
   it's a library, not a commitment, and it is NOT a GUI).
   - **9.2 — the "editable parameters" GUI** you want is a thin **web panel over the §5 config**
     (roster, task-fit, thresholds), in the TangleClaw web-UI style — *that's* the knobs surface,
     not LangGraph. Logic in code; knobs in config+GUI for you (the PM) to tune.
3. **`claude -p` auth** → **always scrub `ANTHROPIC_API_KEY`** from the subprocess env (§7);
   deterministic, rides the Max sub, no billed-key surprise.
4. **Repo timing** → create the TangleBrain repo + TC project **when we start coding the first
   chunk** (after this plan is fully ratified); migrate `TANGLEBRAIN.md` + this plan, re-point the
   shared-doc registration.
5. **Extensibility (standing requirement)** → roster + orchestrator set are config-driven and
   open-ended; adding a future model is an entry edit, never a code change (§5/§6).
6. **Paid-API billing gate (resolves former open decision a)** → an explicit global
   `api_billing_enabled` flag, **default off**. When off, `tier: api` entries parse but never
   route. When on, each paid key is a `tier: api` roster entry with a per-key `enabled` toggle and
   budget cap; paid API stays last-resort (§6). Issue #2.
7. **Paid-key custody (resolves former open decision b → contract invariant #3)** → **LiteLLM-
   fronting is the preferred custody**: TangleBrain references a scoped LiteLLM *virtual key*
   (existing `key_ref`), the raw provider key lives in LiteLLM on Monad. Holding raw provider keys
   directly is **not foreclosed** but is never required and stays behind the `api_billing_enabled`
   toggle. Invariant #3 **softens, not reverses** — the durable rule is *no paid billing without
   the explicit toggle*. Issue #2.

**RATIFIED 2026-06-16.** Chunk breakdown (§10) confirmed. **Next session = C0 (the spike):**
generalize `tools/openclaw-monad-mcp/` (coder-32b/chat-14b → gpt-oss), wire Claude/Codex to
delegate grunt to it, and test whether frontier-first decompose feels good + saves work — before
building any router. Repo created at C1 if the spike says "keep."

---

## 10. Build outline (chunked)

- **C0 — Spike (near-zero code): validate frontier-first before building anything.** Point Claude
  at an MCP tool that calls gpt-oss — **generalize the existing `tools/openclaw-monad-mcp/`** (today
  it exposes coder-32b/chat-14b to Codex; repoint to gpt-oss) — and prompt it to offload grunt to
  that tool. Test whether frontier-first decompose actually feels good + saves work. Cheap keep/kill
  gate on the whole approach. (Can use Codex too — `openclaw-monad-mcp` was built for Codex.)
- **C1 — Repo + skeleton + roster config loader (§5)** + the openai-compat adapter (→ local gpt-oss).
  One request routes to local end-to-end.
- **C2 — CLI adapters for the three subs** (claude/codex/gemini) with env-scrub (§7). **SHIPPED
  2026-06-16** (`CliAdapter`, config-driven prompt injection + `invoke.parse` output parsers;
  env-scrub proven live — claude sees `ANTHROPIC_API_KEY` as UNSET). Split per the one-chunk rule:
  the gpt-oss MCP local-delegate (the other half of this line) became **C2b → issue #4**, deferred
  to land near C3 where an orchestrator actually consumes it.
- **C2b — gpt-oss MCP local-delegate** (issue #4). **SHIPPED 2026-06-16** — `tanglebrain-delegate`
  MCP server exposes `delegate_local(prompt, max_tokens?)` → free local gpt-oss, reusing the C1
  roster + adapter; `mcp` is an optional extra. Verified end-to-end over real MCP stdio. Built
  before C3 (not "near" it) because it's the prerequisite that makes frontier-first actually
  offload grunt — the router consumes this tool in C3's decompose→delegate→review loop.
- **C3 — Frontier-first router (§6):** orchestrator selection (task-fit + rotation), 429/limit
  failover to the next sub. **Control plane SHIPPED 2026-06-16** (`tanglebrain/router.py`,
  `tanglebrain --route [--task]`; persisted rotation cursor; failover with rate-limit annotation;
  rotation/failover proven hermetically, a live route confirmed end-to-end). CLI default kept
  local-first on purpose.
- **C3b (issue #7)** — inject the C2b `delegate_local` tool into orchestrator invocations (so the
  sub offloads grunt to local mid-task) and **flip the CLI default to frontier-first**. This is the
  decompose → delegate → review enablement; split from C3 so the router stayed deterministic and we
  don't burn sub rate limits before delegation makes frontier-first worthwhile.
- **C4 — Measurement/logging (§8)** + the savings rollup (so rate-limit pressure becomes visible).
- **C5 — Knob GUI** (thin web panel over the §5 config, TangleClaw-style) + TangleClaw entry
  integration (prompt in → final out) + runbook.
- **Later, only if §8 data shows rate-limit pressure:** add the local classifier gate (§6 evolution).

(One chunk per session, tests + docs alongside, Critic review after medium+ work.)

---

## 11. C0 Findings — RAN 2026-06-16 → **VERDICT: KEEP** (proceed to C1)

**Setup.** Minted scoped LiteLLM key `tanglebrain-spike` (allowlist gpt-oss-120b + chat-1m +
coder-32b), stored 0600 on Cursatory. Generalized `tools/openclaw-monad-mcp/` additively: new
`monad_grunt → gpt-oss-120b` tool (left the two #38 tools intact), fixed a latent drift bug
(`DEFAULT_LITELLM_URL` was the stale raw IP → MagicDNS FQDN). 22 unit tests green. Registered the
server with Claude Code (`✔ Connected`). Acted as the frontier orchestrator: decomposed a real
multi-part task into 3 grunt sub-tasks, delegated each to free local gpt-oss, reviewed.

**Results against the §criteria:**
1. **Reachable / coherent — YES.** gpt-oss-120b answered over the tailnet via the scoped key.
   Its chain-of-thought lands in a separate `reasoning_content` field; `_call_litellm` already
   returns only `content`, so the orchestrator gets clean output.
2. **Delegate without fighting the tool — YES.** Decompose → `monad_grunt` → review felt natural;
   3 sub-tasks ran concurrently in 2–8s each.
3. **Review cost < doing it directly — YES (with one caveat).** Of 3 delegated grunts: a non-trivial
   `parse_duration` parser came back **correct on all 8 cases incl. edge cases I didn't ask for**
   (empirically verified); a dedup refactor came back clean with docstring + type hints, zero rework.
   The pytest-generation grunt was **truncated** — but because the *probe* under-budgeted at
   `max_tokens=600`; gpt-oss's reasoning overhead is exactly why the tool default is **2048**. Not a
   model failure, a budget lesson — and the tool already encodes the fix.

**Decision:** the free-local grunt tier produces review-worthy output and the frontier-first loop
saves real work. **KEEP → C1** (create the TangleBrain repo + skeleton + roster loader + the
openai-compat adapter to local gpt-oss). Carry forward: budget grunt calls generously (≥2048);
reasoning models need the headroom.
