# API Contract

TangleBrain exposes **four** contract surfaces. All four are consumed from outside this repo — the
package is on PyPI — so none of them can be changed in lockstep with its consumers.

Mechanism lives in [`ARCHITECTURE.md`](../../ARCHITECTURE.md). This document records what is
**promised**, what is **not**, and what breaks whom.

## Invariants

These bind. Departing from one is a decision to record and justify.

- **An unknown routing directive errors; it never falls back.** An unrecognized `model` on the HTTP
  surface is `404 model_not_found`; an unknown delegate `target` does not silently become something
  else.

  *Why:* a silent fallback spends a *different* backend's quota while returning what looks like
  success. The caller gets a plausible answer from a backend they did not choose, at a cost they
  did not authorize, with nothing anywhere recording that a substitution happened. An error is
  recoverable; a wrong-but-confident answer is not.

- **A "no fit" outcome is a signal, not an error.** `NoDelegateFit` surfaces to the orchestrator as
  an instruction to handle the sub-task itself; a failing item in `delegate_many` gets a per-item
  `status` and never sinks the batch.

  *Why:* the consumer is a model that cannot ask a clarifying question. Raising at it converts a
  routable situation into a dead end, when the honest information — "no configured backend fits
  this, do it yourself" — is something it can act on immediately. Distinguishing the two is what
  keeps a partial failure partial.

- **A roster `id` is public API in two namespaces at once.** It is the `model` value on the HTTP
  surface and the `target` value on the MCP surface.

  *Why:* renaming one breaks both, and neither consumer can be migrated in lockstep — one is an
  arbitrary OpenAI client, the other is a model reading a tool description. Treating ids as internal
  labels is the easiest way to ship a breaking change without noticing you did.

- **New fields are additive and optional across every surface.** Canonical statement and rationale
  live in [`data-model.md`](data-model.md). **One bounded exception**, ruled 2026-09-10: the
  localhost-only GUI endpoint may also *drop* a field its own panel does not render, because its
  one consumer ships in the same wheel and nothing persists the payload. The conditions, and why
  they do not generalize, are in
  [`deprecation-policy.md`](deprecation-policy.md) § HTTP surfaces.

## Surface 1 — HTTP, OpenAI-compatible (`tanglebrain-serve`)

The widest surface: any OpenAI client is a potential consumer, and it will never read TangleBrain's
documentation.

**Endpoints**

- `POST /v1/chat/completions`
- `GET /v1/models` — the `auto` alias plus every roster id.

**The `model` field is a routing directive**, which is the one place TangleBrain overloads OpenAI
semantics:

- `auto` → the full router (classifier gate honored per settings)
- a roster id → pins that entry
- anything else → `404` with code `model_not_found`. Never a fallback.

**Requests.** Messages are flattened into a role-tagged transcript. Non-text parts (images, audio)
are rejected explicitly rather than silently dropped — dropping them would answer a different
question than the one asked.

**Streaming** (`stream: true`) is genuinely incremental where the backend supports it
(`openai-compat` / `api` kinds via the adapters' optional `run_stream`). Backends that cannot stream
(`cli` kinds) deliver the completed text as one chunk — a valid stream, not an error. The first
delta is pulled **before headers commit**, so a connect-time failure is a plain JSON error rather
than a half-open stream.

**Errors** — OpenAI-shaped `{"error": {"message", "type", "code"}}`, deliberately not RFC-7807,
because compatibility includes error handling:

| Status | Type | When |
|---|---|---|
| 400 | `invalid_request_error` | malformed payload, non-text message part |
| 404 | `invalid_request_error` + code `model_not_found` | unknown `model` |
| 502 | `upstream_error` | `AdapterError` / `RouterError`; stream ended before content |
| — | `server_error` | nonconforming adapter or internal bug |

**Mid-stream failure contract:** one `{"error": ...}` SSE event and **no `[DONE]`**. A client can
always distinguish failure from clean close.

**Authentication: none, by design.** The `Authorization` header is never read. The loopback bind is
the entire access control model — see [`security-model.md`](security-model.md).

**Extension:** `X-TangleBrain-Parent-Task` is sanitized and recorded as `parent_task_id`. Metadata
only — **never routed on**. That restraint is the contract: a header cannot influence backend
selection or spend.

## Surface 2 — MCP tools (`tanglebrain-delegate`)

Consumed by an orchestrator **model**, which is the hardest consumer to serve: it reads tool
descriptions rather than docs, cannot ask a clarifying question, and cannot be migrated when a
signature changes.

| Tool | Signature | Contract |
|---|---|---|
| `delegate_local` | `(prompt, max_tokens?)` | Free local tier. The $0 default. |
| `delegate` | `(prompt, target?, task?, max_tokens?)` | Precedence **`target` > `task` > local**. |
| `delegate_many` | `(tasks, max_concurrency?)` | Concurrent fan-out, per-item routing. |
| `delegate_targets` | `()` | The configured menu: `id`, `tier`, `good_at`, `cost`, `kind`. |

**Selection rules for `delegate`:**

- `target` — an explicit roster id flagged `can_delegate: true`.
- `task` — a capability tag; picks the **cheapest** `can_delegate` entry whose `good_at` contains it
  (`TIER_RANK` `local` < `sub`, ties by declared order).
- **`api` is never auto-selected by `task`.** Paid is a last resort, never a preference. A paid
  target must be named explicitly and still passes the billing gate.
- No fit → `NoDelegateFit`, which the tool converts into an *instruction* for the orchestrator to
  handle the sub-task itself.
- The target is built as a **leaf** (`inject_delegate=False`) — delegation never recurses.

Capability routing currently ranks by cost only, so it can route *down* but never *up*: there is no
way to say "this sub-task is hard, send it somewhere better" without naming an id. Tracked in
[#97](https://github.com/Jason-Vaughan/TangleBrain/issues/97).

**`delegate_many` guarantees:** results in **input order**, each with `status`
(`ok` / `no_fit` / `error`); a failing item never sinks the batch; concurrency bounded by
`_effective_concurrency`, where a per-call value may lower but never raise the operator's bound.
Dispatch and collect only — **synthesis is deliberately the orchestrator's**, and there is no
reducer tool by design.

**The `delegate` tool description enumerates the target menu and is built once at server startup** —
so a roster edit is not visible to an already-running server. Restart is required. Recorded because
a model reading a stale menu will confidently route to a target that no longer exists.

## Surface 3 — CLI (`tanglebrain`)

| Flag | Contract |
|---|---|
| `prompt` (positional) | Optional only with `--stats`. |
| `--version` | Prints version, exits. |
| `--roster PATH` | Explicit roster path. |
| `--model ID` | Pin a roster entry. Explicit override of routing. |
| `--local` | Force the free local tier. |
| `--task TAG` | Task-fit hint (a `good_at` tag). |
| `--gate` / `--no-gate` | Force the classifier gate on/off for this run. |
| `--max-tokens N` | Override the completion cap (adapter default 2048). |
| `--stats` | Print the spend-avoided rollup and exit. |
| `--route` | **Deprecated no-op**, kept for back-compat. |

**`--route` is the deprecation policy's worked example:** a superseded flag is kept as an accepted
no-op rather than removed, so an existing script keeps working. It was a precedent one flag deep
until [`deprecation-policy.md`](deprecation-policy.md) wrote the rule down and cited it by name.

`--model` pins **which** backend serves a request; it does not decide **whether** that backend may
delegate. An orchestrator-capable entry keeps its delegate tool on the pinned path exactly as it has
it on the router path — the answer is derived from the entry's own `can_orchestrate` flag inside
`build_adapter`, not restated by each caller.

## Surface 4 — GUI panel (`tanglebrain-gui`)

Localhost-only, internal. Views the roster, pricing, and rollup; runs a prompt; edits pricing plus a
**focused subset** of per-entry roster fields (`enabled`, `can_orchestrate`, `budget_usd_month`,
`good_at`).

**That the editable set is a fixed allow-list is the mass-assignment control** — the panel cannot be
talked into writing `invoke`, `key_ref`, or `tier`. Writes are validated, atomic, backed up with a
timestamp, and comment-preserving.

**Secrets are never resolved or sent to the browser** — a `key_ref` renders as its reference string.

**`/api/stats` returns a named projection, not the measurement rollup.** The endpoint declares the
fields it sends; a field added to `rollup()` for the CLI's benefit does not reach the browser until
someone puts it there. This surface is the one place in TangleBrain where a *narrowing* change is
admissible — it is localhost-only, and the panel that consumes it ships in the same package — so
the projection is free to reshape as well as to select.

The declared set, in full. Top level: `summary`, `pricing_ref`, `is_placeholder`, `health`. Inside
`summary`: `tasks`, `spend_avoided_usd`, `by_tier`, `by_origin`, `in_tokens_est`, `out_tokens_est`,
`by_model`, `by_day`, `by_day_since`, `spend_avoided_outside_days_usd`, and `delegates`
(`count`, `linkage_lost`, `in_tokens_est`, `out_tokens_est`, `cloud_equiv_usd`, `by_backend`,
`linked_parents`). Nothing else in `rollup()`'s dict is sent — **`cloud_equiv_usd` at top level,
`pricing_refs`, `failures`, `lost_attempts`, the per-entry token counts inside both breakdowns, and
the delegates' `by_parent` tree were all removed here**, none of them rendered by the panel.

What the reshaped and added members mean:

- `by_model` — a list of `{id, count, spend_avoided_usd}`, ranked by spend. The store holds a map;
  the ranking is the table, and a JSON object's key order is not a contract.
- `by_day` — a list of `{day, spend_avoided_usd}`, ascending, capped at the **90 days** the panel's
  widest window draws, with idle days inside the covered range materialized as `0.0` and days
  before the earliest surviving bucket simply absent. In the store's map those two cases are
  indistinguishable without knowing the rule; as a series the distinction is structural.
- `by_day_since` — when per-day recording began. The caption's *wording*, never the chart's left
  edge: after the first eviction it is older than anything left, so drawing from it would paint the
  evicted span as $0.
- `spend_avoided_outside_days_usd` — lifetime spend minus every retained day bucket. The panel
  cannot compute it, and without it "this window is narrower" and "the per-day data starts later"
  are indistinguishable from the browser.
- `delegates.linked_parents` replaces the `by_parent` tree — one key per parent task id is
  unbounded, and the panel renders one number from it.

## API design review

Assessed against the OWASP API risk categories, because these surfaces carry credentials and spend
money.

- **Broken object-level authorization (BOLA)** — *not applicable*. No objects, no users, no
  per-object ownership. Single operator.
- **Broken authentication** — *accepted, by design*. There is none; the loopback bind substitutes.
  Sound only while the bind holds, which is why off-loopback exposure is recorded as prohibited
  rather than discouraged.
- **Mass assignment** — *mitigated* by the GUI's fixed editable allow-list.
- **Excessive data exposure** — *mitigated structurally*. `key_ref` is never resolved outward;
  prompt and response text is never persisted, so no endpoint can leak it. The persistence half is
  structural wherever a model completion is reproduced and a judgement at the error diagnostics
  enumerated in [`security-model.md`](security-model.md) § Known gaps — an endpoint rendering a
  failure record inherits that qualification.
- **Lack of rate limiting** — *accepted*. A local caller can exhaust backend quota. The mitigation
  is that only local callers exist.
- **Security misconfiguration** — *the live risk*. The entire posture depends on two settings
  staying false and two servers staying on loopback.

## Compatibility

Versioned by package semver plus [`CHANGELOG.md`](../../CHANGELOG.md); breaking changes to the MCP
or CLI surfaces are major bumps. The HTTP `/v1` path is OpenAI's, not TangleBrain's — its
compatibility is defined upstream.

**The deprecation policy is written down** — [`deprecation-policy.md`](deprecation-policy.md). It
states what each surface promises, how a break is announced, and the rule for dependency floors —
the clause that first bit, when the `mcp >= 2` floor shipped as a breaking change for installs
even though no TangleBrain code changed
([#90](https://github.com/Jason-Vaughan/TangleBrain/issues/90)).

Two rules from it are worth repeating here, because this document is where a caller looks first: a
removed CLI flag becomes an accepted no-op rather than an error, and a usage-log field is never
removed, renamed, or given a new meaning.
