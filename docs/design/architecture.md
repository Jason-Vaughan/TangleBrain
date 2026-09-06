# Runtime Architecture

[`ARCHITECTURE.md`](../../ARCHITECTURE.md) at the repo root is canonical for **system structure** —
what the router, adapters, classifier, delegate, measurement, GUI, and serve endpoint are, and why
each is shaped that way. Read it first; this document does not restate it.

What follows is the part a component description does not cover: TangleBrain runs as several
processes, **one of which it does not own**, with state shared through the filesystem and
correlation carried through an environment variable across a boundary it does not control. Those
are the parts that break in ways the component view will not predict.

## Process topology

Four process shapes at runtime:

1. **The entry process** — `tanglebrain`, `tanglebrain-gui`, or `tanglebrain-serve`.
   Operator-launched. Owns routing, measurement, and the request.
2. **CLI adapter children** — authenticated third-party CLIs spawned as subprocesses, without a
   shell. Short-lived, one per call.
3. **The delegate MCP server** — `tanglebrain-delegate`, over stdio. **Spawned and owned by the
   orchestrator, not by TangleBrain.** This is the important one: its lifecycle, environment, and
   restart behavior belong to a process TangleBrain does not control and cannot introspect.
4. **Worker threads** — `delegate_many`'s `ThreadPoolExecutor` inside whichever process is serving.
   Not separate processes, but the only real concurrency the product creates.

The topology is a **tree, not a mesh**. Nothing calls back upward, and delegate targets are built as
leaves (`inject_delegate=False`) so a sub-task can never spawn its own sub-tasks. That single flag
is what bounds fan-out; without it, a model could drive unbounded recursive delegation with no depth
limit and no cost ceiling.

## Communication channels

| Channel | Between | Carries | Failure mode |
|---|---|---|---|
| HTTP | entry process → openai-compat / api backends | prompt, completion | `AdapterError` → failover |
| stdio (MCP) | orchestrator → delegate server | tool calls | orchestrator's to handle; TangleBrain sees a dead pipe |
| subprocess argv + stdout | entry process → CLI backends | prompt, parsed output | `AdapterError` → failover |
| **environment variable** | entry process → orchestrator → delegate child | `TANGLEBRAIN_TASK_ID` | **silent degradation to `unlinked`** |
| filesystem | all processes | rotation cursor, usage log | best-effort; never breaks routing |

### The environment-variable hop deserves its own paragraph

`TANGLEBRAIN_TASK_ID` is minted by the CLI, injected into the orchestrator's environment, forwarded
by the orchestrator to the delegate child, and read back by `run_delegate` to stamp
`parent_task_id`. The middle hop is performed by software TangleBrain does not own and cannot test
hermetically — it is verified live against one orchestrator (Claude Code), which is honest but is
not a guarantee.

The design response is the right one: a delegation that loses the variable is recorded `unlinked`
rather than raising. **The consequence worth holding in mind is that this failure is invisible.** A
different orchestrator that does not forward environment to its MCP children produces a complete,
correct-*looking* usage log in which every delegation is silently unparented, and nothing anywhere
reports that linkage was lost.

If parent-task attribution ever becomes load-bearing rather than informational, this needs a
*positive* signal — not more error handling. Tracked in
[#100](https://github.com/Jason-Vaughan/TangleBrain/issues/100).

## Concurrency model

- **One request is single-threaded** end to end. The router, classifier, and adapters are plain
  synchronous code.
- **`delegate_many` is the only fan-out.** Synchronous I/O-bound calls on a `ThreadPoolExecutor` —
  the workload is network-wait, so threads are the right primitive and no async runtime is
  warranted.
- **Bounded by `_effective_concurrency`**: the operator's `settings.delegate_max_concurrency` if
  set, else an `os.cpu_count()`-derived default. A per-call `max_concurrency` may **lower** but
  never **raise** it. The "never raise" direction is the safety property — a model calling the tool
  cannot talk the system into more parallelism than the operator allowed.
- **Results are returned in input order** with per-item `status`, so concurrency is not observable
  in the result contract. A failing item never sinks the batch.
- **Shared mutable state across threads** is the measurement store — the usage log and, since
  compaction, `totals.json` — both serialized by the one `measurement._LOG_LOCK`. A compaction holds
  it across read-fold-truncate, so a second writer to *either* file belongs inside that lock. It is
  a plain `Lock`, not an `RLock`, so nesting an acquisition inside one deadlocks rather than raising.

## Persistence boundaries

Fully specified in [`data-model.md`](data-model.md) — see its "Persistence boundaries" table, which
is canonical. The one-line summary: **nothing in-flight is durable**, and everything that is durable
lives under one state root in the XDG *data* tier — the rotation cursor, the usage log, the lifetime
totals, and config backups. None of them is a cache, and `usage.jsonl` plus `totals.json` hold
history that is not reconstructible from anything else.

## Failure and degradation

The system degrades along a deliberate ladder rather than failing:

1. Classifier error or ambiguity → route to frontier (never trap a hard task on the local tier).
2. Adapter error → next orchestrator in rotation.
3. All orchestrators failed **and** the paid gate is on → paid tier, in roster order, as a genuine
   last resort. A paid success does **not** advance the rotation cursor.
4. All paths failed → `RouterError` listing every failure, rate-limits annotated.
5. Measurement failure at any point → swallowed; the answer still returns.

Step 5 is `record_task`'s own broad exception handler, and it is load-bearing: the alternative is a
logging bug that eats a successful, already-paid-for answer. It is not the only broad catch in the
codebase — `grep -rn "noqa: BLE001" tanglebrain/` enumerates them, and ruff fails the build on one
that is unmarked or on a waiver no longer needed. What is singular is the *rationale*: only the
side-effect norm licenses swallowing, and it does not generalize.

Per-failure verification is tabulated in
[`nonfunctional-requirements.md`](nonfunctional-requirements.md).

## Where this could stop being true

The topology assumptions worth re-checking when the system changes:

- **If delegation ever recurses**, the fan-out bound disappears and the cost ceiling with it.
  `inject_delegate=False` is the guard; treat any change to it as a security-surface change.
- **If a second writer of the roster appears** (a second GUI, a config API), the
  atomic-write-plus-backup story becomes a concurrent-write story it was not designed for.
- **If anything binds off-loopback**, the whole authorization model
  ([`security-model.md`](security-model.md)) is void, not weakened.
