# Nonfunctional Requirements

Calibrated to what TangleBrain is: a published, single-operator local tool that handles credentials
and can spend real money.

Each requirement below states how it is **verified**, because an NFR nobody can check is a wish.

## Invariants

These bind. Departing from one is a decision to record and justify.

- **The default install costs $0 and reaches nothing off-machine.** A fresh install's packaged
  roster has one active entry — the free local tier.

  *Why:* this is the product's premise, not a performance target. "Local-first" is the claim the
  README makes and the reason someone chooses this over a hosted router; a default that contacted a
  paid or remote service would make the claim false. Any change here is a breaking change to what
  TangleBrain *is*, not a tuning decision.

- **Degrade, don't fail.** Every dependency is assumed unavailable sometimes; the design question is
  always what happens next, and the answer is never "raise and stop" while another path exists.

  *Why:* a router's whole value is having somewhere else to send the work. Failing on the first
  backend error would make it strictly worse than calling the backend directly. This is what the
  failover ladder, the classifier's fail-to-frontier direction, and per-item batch status all
  implement.

  > The system degrades correctly but records nothing when it does, so failover is currently
  > invisible. That is a missing signal, not a violation —
  > [#100](https://github.com/Jason-Vaughan/TangleBrain/issues/100).

- **A side-effect never breaks the main path.** Measurement, logging, and state persistence are all
  subordinate to returning the answer.

  *Why:* the answer has already been paid for — in local compute, in subscription quota, or in real
  money. Losing it to a logging bug destroys something valuable to record something incidental. This
  is the rationale that licenses the codebase's one deliberately broad exception handler
  (`measurement.py:376-378`); the waiver is scoped to this rule and does not generalize.

## Performance

| Requirement | Target | Verification |
|---|---|---|
| Routing overhead | Negligible against backend latency, which dominates by orders of magnitude | Not measured. Accepted on structural grounds: routing is a few dict lookups and a file read against a multi-second network call. |
| `delegate_many` wall-clock | Meaningfully better than sequential | `ThreadPoolExecutor` fan-out; the workload is network-wait, so threads are the right primitive. |
| Streaming latency | First token reaches the client as soon as the backend produces it | The serve endpoint writes one flushed SSE event per delta rather than buffering. |
| Startup | Fast enough to be invisible in a CLI | No heavy imports at module load; MCP is an optional extra so the base install stays thin. |

**Non-goal:** throughput. There is no request-per-second target because there is one operator.
Optimizing for concurrency here would be optimizing for a user who does not exist.

## Scalability

Explicitly bounded, and the bounds are the design:

- **One operator, one machine.** No multi-tenancy, no shared instance, no horizontal scaling.
- **The only concurrency dimension is sub-task fan-out**, bounded by
  `settings.delegate_max_concurrency` or an `os.cpu_count()`-derived default. A per-call value may
  **lower** but never **raise** it — a model calling the tool cannot talk the system into more
  parallelism than the operator permitted.
- **Roster size** is expected in the tens. Selection is a linear scan and that is appropriate;
  anything cleverer would be unjustified.

**Known unbounded quantity:** `usage.jsonl` grows forever. No rotation, no cap, no pruning.
Slow-moving for one operator, but it is the one place the system has no scaling story at all —
[#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101).

## Reliability

There is no uptime target — this is not a service. What replaces it is a specified degradation
ladder:

| Failure | Behavior | Verification |
|---|---|---|
| Classifier errors or is ambiguous | Route to **frontier** | Fail-safe direction: a hard task is never trapped on the local tier. `tests/test_classifier.py` |
| A backend fails | Fail over to the next orchestrator | `tests/test_router.py` |
| All orchestrators fail, paid gate **off** | `RouterError` listing every failure, rate-limits annotated | `tests/test_router.py` |
| All orchestrators fail, paid gate **on** | Fall through to enabled `api` entries in roster order; a paid success does **not** advance the rotation cursor | `tests/test_router.py` |
| Measurement raises | Swallowed; answer still returns | `measurement.py:376-378`, `tests/test_measurement.py` |
| A usage record is corrupt | Skipped; rollup still produced | `tests/test_measurement.py` |
| Roster missing | Falls back to the packaged local-only example | `tests/test_roster.py` |
| A `delegate_many` item fails | Per-item `status`; batch completes | `tests/test_delegate.py` |
| Rotation cursor lost | Rotation restarts from the beginning | Harmless — a fairness hint, not correctness. |

## Cost

**Hard constraint:** the default configuration costs exactly $0 and contacts nothing off-machine.

- Paid backends require **two** independent gates, both defaulting false, both strictly
  bool-validated so a stray `"yes"` or `1` cannot enable billing.
- `api` is never auto-selected by capability — paid is a last resort, never a preference.
- `budget_usd_month` per entry is the operator's declared ceiling.
- Verified by `tests/test_selector.py` and `tests/test_settings.py`.

**Honest limit:** `budget_usd_month` is a *declaration*, not an enforcement — nothing stops spend
when the figure is reached, and `--stats` reports cloud-*equivalent* (avoided) cost rather than
actual billed spend. An operator who reads it as a hard cap is misreading it. Worth either enforcing
or renaming; recorded, not decided.

## Security

Fully specified in [`security-model.md`](security-model.md). The NFR-level statements:

- No secret is ever persisted, logged, or rendered — only `key_ref` references.
- No prompt or response text is ever written to disk.
- No shell is ever invoked; `cmd` is `list[str]`.
- Nothing binds off-loopback.

## Compatibility

- **Python:** as declared in `pyproject.toml` and enforced by the CI matrix.
- **Dependencies:** deliberately minimal — stdlib GUI and serve, no agent framework. Every
  dependency avoided is one that cannot break a published package.
- **The `mcp < 2` cap is an NFR, not an oversight.** mcp 2.0.0 removed `mcp.server.fastmcp`, so an
  unbounded `mcp >= 1.0` shipped an extra that could not import. `tests/test_packaging.py` encodes
  that constraint — **when [#90](https://github.com/Jason-Vaughan/TangleBrain/issues/90) lifts the
  cap, that test gets updated deliberately, never deleted to green a build.**
- **Open risk:** `httpx >= 0.27` and `PyYAML >= 6.0` carry the same unbounded shape, and no
  scheduled CI run exists to catch an upstream break without a push
  ([#92](https://github.com/Jason-Vaughan/TangleBrain/issues/92)). The v0.20.1 incident is the proof
  that this class of risk is real here, not theoretical.

## Maintainability

- Pure logic is separated from I/O so it is directly testable: `gui/views.py` and `serve/views.py`
  are socket-free, with `server.py` wrapping each in a pure `dispatch(method, path, body)` plus a
  `ThreadingHTTPServer`.
- Adding a backend type is contained to one adapter file.
- Adding a backend is a config edit, not a code change — the product's central claim, and the thing
  to protect in review.

## Accessibility

Baseline, proportionate to a localhost knob panel: the primary surface is a CLI (terminal
accessibility inherited), and the GUI uses semantic HTML with real labelled form controls — achieved
largely by not reaching for custom widgets.

**No WCAG conformance is claimed**, and claiming one without an audit would be dishonest. **Gap:**
contrast ratios in the panel CSS have never been measured. Cheap to check; not yet checked.
