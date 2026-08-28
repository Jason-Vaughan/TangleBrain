# Observability

## The honest summary

TangleBrain has **one signal**: an append-only JSONL usage log. No metrics backend, no tracing, no
alerting, no health endpoint.

For a single-operator local tool whose consumer is a human running `--stats`, that is the right
depth — and this document says so rather than filing three absent signals as gaps. What it does
*not* do is pretend the coverage is complete: the log was built to answer "what did routing save
me", and it is now also the only thing available for "why did that go wrong". Those are different
jobs, and the second one is served worse.

## Invariants

- **Observability degrades to less information, never to an error.** A lost correlation id records
  `unlinked`; a corrupt record is skipped; a rollup over a partially-damaged log still produces a
  rollup.

  *Why:* the signal exists to inform a human who is curious, not to gate anything. An observability
  path that can fail the operation it observes has inverted its own priority. The cost of this rule
  is honest and worth naming: failures here are **silent by construction**, which is exactly why the
  unlinked-delegation case below needs a positive signal rather than more error handling.

- **Observability never affects the answer.** Canonical statement, and the scope of the
  `measurement.py` broad-catch waiver, live in
  [`nonfunctional-requirements.md`](nonfunctional-requirements.md).

- **Never log prompt or response text.** Canonical statement and rationale live in
  [`data-model.md`](data-model.md).

## The signal

One JSON line per task or delegation, appended to `~/.cache/tanglebrain/usage.jsonl` (honoring
`TANGLEBRAIN_STATE_DIR`). Fields are specified in [`data-model.md`](data-model.md).

**Record kinds, and why the distinction matters for the rollup:**

- `kind: "task"` — a top-level routed request. **Counts toward the spend-avoided headline.**
- `kind: "delegate"` — a sub-call offloaded through `run_delegate`, metered at that single seam so
  every delegation including each `delegate_many` item is captured. **Held out of the headline.**
- `kind: "failure"` — a task that failed at every backend (#100). **Held out of the headline** —
  nothing was served, so nothing was avoided. Carries the per-backend attempt list in `failures`;
  a task served only after failover carries the same list on its `task` record.

The exclusion is the correct call and worth stating: the parent task already credits the whole job,
so counting sub-calls again would double-count the saving. Delegates aggregate **separately** into a
by-backend breakdown (count, estimated tokens, informational cloud-equivalent) surfaced in `--stats`
and the GUI.

## Correlation

The one genuinely designed piece.

`task_id` is minted by the CLI per routed task → injected as `TANGLEBRAIN_TASK_ID` into the
orchestrator's environment → forwarded by the orchestrator to the MCP delegate child → read back by
`run_delegate` to stamp `parent_task_id`. The rollup groups delegates `by_parent` — a real "linked
to" tree across a process boundary.

`origin` (`cli` / `gui` / `serve`) tags which surface a record came from. On the serve endpoint, the
optional `X-TangleBrain-Parent-Task` header is sanitized into `parent_task_id` — metadata only,
never routed on.

**The limit, stated because it is invisible.** The middle hop runs inside software TangleBrain does
not own. It is verified live against one orchestrator (Claude Code), not hermetically. A delegation
that loses the variable is recorded `unlinked` rather than raising — correct behavior, but it means
a different orchestrator that does not forward environment to its MCP children produces a complete,
correct-*looking* log in which every delegation is silently unparented, with nothing anywhere
reporting that linkage was lost.

If parent attribution ever becomes load-bearing rather than informational, this needs a **positive**
signal — a count of unlinked delegations surfaced in `--stats` would be the cheap version — not more
error handling.

## Sensitive data

Handled **structurally rather than by filtering**: prompt and response text is never persisted at
all. Only derived counts (`chars/4` over the text) and routing metadata are written. `key_ref`
values are references, never secrets, and are never logged or rendered resolved.

This is the strongest property in the whole observability design — a filter can be bypassed by a new
code path, but there is no filter here to bypass because there is nothing to filter.

## Operational model

**Developer-debugging.** The consumer is the operator reading `--stats` or the GUI panel. Not an
SRE, not an automated responder, and there is no paging story because there is nothing to page.

## Measurement honesty

Token counts are **estimated** with a uniform `chars/4` heuristic over the visible prompt and
response, because the authenticated CLIs expose no usable counts. One consistent approximate
methodology applies to every tier — deliberately, so tiers stay comparable even though none is
exact.

`cloud_equiv_usd` and `spend_avoided_usd` are **counterfactuals**: what the same work would have
cost on a paid frontier API at the reference price in `config/pricing.yaml`. They are not bills.
`spend_avoided_usd` is `0.0` for `tier: api`, since real spend avoids nothing.

Documentation consistently calls these estimates. Keep it that way — the number's credibility rests
on not overclaiming it.

## What is deliberately absent

| Signal | Status | Reasoning |
|---|---|---|
| Metrics backend | Absent, correct | One operator, no time series worth scraping. |
| Distributed tracing | Absent, defensible | The parent-task tree already covers the one cross-process relationship. |
| Alerting | Absent, correct | Nothing to alert; nobody on call. |
| Health endpoint | Absent, correct | Not a service. Failure is visible in the response. |
| Structured error log | Folded into the usage log | Failures are `kind: "failure"` records carrying the per-backend attempt list (#100). |

## Gaps

Recorded, not fixed.

1. **No `unlinked` visibility.** Per the correlation section above.
2. **Unbounded log growth**, with no rotation or pruning — the operational cost of append-only. See
   [`operations.md`](operations.md).
3. **Cache-tier placement.** The log is the only record of accumulated spend-avoided and it lives
   where cleanup tools delete things. See [`data-model.md`](data-model.md).

Two former gaps here — no failure record at all, and failover being unobservable — were closed by
[#100](https://github.com/Jason-Vaughan/TangleBrain/issues/100): a task that fails at every backend
is recorded as `kind: "failure"`, a failover success carries the attempts it lost, and `--stats`
surfaces both. Gaps 2–3 are tracked in
[#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101).
