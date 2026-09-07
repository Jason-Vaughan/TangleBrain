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
  `linkage_lost: true`; a corrupt record is skipped; a rollup over a partially-damaged log still
  produces a rollup.

  *Why:* the signal exists to inform a human who is curious, not to gate anything. An observability
  path that can fail the operation it observes has inverted its own priority. The cost of this rule
  is honest and worth naming: swallowing means failures here are **silent by default**, which is
  exactly why the unlinked-delegation case below needs a positive signal rather than more error
  handling. A lost usage-log append is swallowed as ever, and also said once per process on
  stderr, because a headline summed from a log with a hole in it understates while still labelled
  *lifetime*.

- **Observability never affects the answer.** Canonical statement, and the scope of the
  `measurement.py` broad-catch waiver, live in
  [`nonfunctional-requirements.md`](nonfunctional-requirements.md).

- **Never log prompt or response text.** Canonical statement and rationale live in
  [`data-model.md`](data-model.md).

## The signal

One JSON line per task or delegation, appended to `usage.jsonl` under the state root
(`TANGLEBRAIN_STATE_DIR` → `$XDG_DATA_HOME/tanglebrain` → `~/.local/share/tanglebrain`).
Fields are specified in [`data-model.md`](data-model.md).

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

`by_parent` is the **one window-scoped figure** in an otherwise lifetime rollup: it carries a key
per parent task id, so it cannot be folded into `totals.json` the way every other aggregate is, and
both `--stats` and the GUI panel label it as covering the current row window. An unlabelled window
count sitting beneath a lifetime headline reads as a lifetime count, which is the specific way a
two-part store goes wrong.

`origin` (`cli` / `gui` / `serve`) tags which surface a record came from. On the serve endpoint, the
optional `X-TangleBrain-Parent-Task` header is sanitized into `parent_task_id` — metadata only,
never routed on.

The middle hop runs inside software TangleBrain does not own. It is verified live against one
orchestrator (Claude Code), not hermetically. The delegate measurement seam knows a parent hop was
expected, so absence of `TANGLEBRAIN_TASK_ID` records the additive field `linkage_lost: true`.
`--stats` and the GUI surface its lifetime count separately from the window-scoped parent tree.
A top-level `kind: task` record with no parent is an ordinary root and never increments that count.
Older parentless delegate rows predate the field but roll up as lost linkage by their `kind`, so the
signal covers history and survives compaction without changing old records.

## Sensitive data

Handled **structurally rather than by filtering**: prompt and response text is never persisted at
all. Only derived counts (`chars/4` over the text) and routing metadata are written. `key_ref`
values are references, never secrets, and are never logged or rendered resolved.

This is the strongest property in the whole observability design — a filter can be bypassed by a new
code path, but there is no filter here to bypass because there is nothing to filter.

That holds without qualification for the measured text. It is narrower on the error path: a failed
attempt persists the diagnostic that explains it, so the claim is structural at every adapter site
that reproduces a completion — those describe shape rather than content — and a judgement at the
provider- and CLI-produced strings kept verbatim so the commonest setup failures stay diagnosable.
[`security-model.md`](security-model.md) § Known gaps enumerates them.

## Operational model

**Developer-debugging.** The consumer is the operator reading `--stats` or the GUI panel. Not an
SRE, not an automated responder, and there is no paging story because there is nothing to page.

## Measurement honesty

Token counts are **estimated** with a uniform `chars/4` heuristic over the visible prompt and
response, because the authenticated CLIs expose no usable counts. One consistent approximate
methodology applies to every tier — deliberately, so tiers stay comparable even though none is
exact.

`cloud_equiv_usd` and `spend_avoided_usd` are **counterfactuals**: what the same work would have
cost on a paid frontier API at the reference price that was in force **when the task ran**. They are
not bills. `spend_avoided_usd` is `0.0` for `tier: api`, since real spend avoids nothing.

Each record is priced once, at write time, and editing `config/pricing.yaml` never restates it —
otherwise a self-reported saving could be inflated retroactively by editing a config file, which is
what makes such a figure worthless. A lifetime figure can therefore span revisions, and `--stats`
says so instead of labelling it with whichever one is configured today.

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

No open measurement gap is currently recorded here.

Five former gaps here are closed. *Lost delegate linkage being indistinguishable from a top-level
task* went with [#123](https://github.com/Jason-Vaughan/TangleBrain/issues/123). *No failure record at
all* and *failover being unobservable* went
with [#100](https://github.com/Jason-Vaughan/TangleBrain/issues/100): a task that fails at every
backend is recorded as `kind: "failure"`, a failover success carries the attempts it lost, and
`--stats` surfaces both. *Unbounded log growth* and *cache-tier placement* went with
[#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101): the state root is the XDG data
tier, and the row window is capped by size, folding its oldest rows into `totals.json` — see
[`data-model.md`](data-model.md) and [`operations.md`](operations.md).
