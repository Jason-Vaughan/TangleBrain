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

  Degradation is also recorded (#100): lost failover attempts land on the served task's usage
  record, and a task that fails at every backend writes a `kind: "failure"` record.

- **A side-effect never breaks the main path.** Measurement, logging, and state persistence are all
  subordinate to returning the answer.

  *Why:* the answer has already been paid for — in local compute, in subscription quota, or in real
  money. Losing it to a logging bug destroys something valuable to record something incidental. This
  is the rationale that licenses the broad exception handlers on the measurement path — the one
  inside `record_task` and the one in `delegate.py` that holds the caller to the same guarantee
  independently. The waiver is scoped to this rule and does not generalize: every broad catch in the
  codebase carries a `# noqa: BLE001` naming the boundary it exists for, ruff fails the build on one
  that does not, and `RUF100` fails it on one that is no longer needed. `grep -rn "noqa: BLE001"`
  enumerates them, which is why no count is written here.

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
| Measurement raises | Swallowed; answer still returns | `tests/test_measurement.py`, `tests/test_delegate.py` |
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
- **The `mcp` requirement pins a major at both ends (`>= 2, < 3`), and that is an NFR.**
  `mcp_server.py` imports `mcp.server.mcpserver`, which exists in 2.x and no earlier major, so the
  floor is load-bearing rather than aspirational; the ceiling stops the next major landing
  unannounced, as for every other dependency. `tests/test_packaging.py` asserts both ends. Moving
  either is a breaking change for installs even though no TangleBrain API moves — announced under
  [`deprecation-policy.md`](deprecation-policy.md), "Dependency floors". Users needing mcp 1.x
  install TangleBrain 0.20.1, the last release that allowed it.
- **Every declared dependency carries an upper bound**, core and extras alike, asserted by
  `tests/test_packaging.py` over `project.dependencies` and every `optional-dependencies` extra
  rather than over one named requirement. Bounds sit at the major boundary: tighter caps create
  resolution conflicts for anyone installing TangleBrain alongside other packages, a real cost paid
  to prevent a break semver already announces. A weekly scheduled CI run covers what a cap cannot —
  a compatible-range release that changes behavior rather than API.

## Maintainability

- Pure logic is separated from I/O so it is directly testable: `gui/views.py` and `serve/views.py`
  are socket-free, with `server.py` wrapping each in a pure `dispatch(method, path, body)` plus a
  `ThreadingHTTPServer`.
- Adding a backend type is contained to one adapter file.
- Adding a backend is a config edit, not a code change — the product's central claim, and the thing
  to protect in review.

### Code quality gates

`make lint` runs **ruff** and **mypy**; `make test` depends on it and CI runs `make test`, so a gate
cannot be green locally and absent in CI. Both tools live in the `dev` extra: nothing there is
imported at runtime, so the minimal-dependency posture — which is about what a *user* installs — is
untouched, and `tests/test_packaging.py` holds the extra to the same upper-bound rule as every other.

What is gated, and what is deliberately not, is a decision rather than a default. Each half was
measured against this codebase before it was made:

| Tool | Ruling | Why |
|---|---|---|
| `ruff check` | **Adopted** | Selects for defects — unused names, late-binding closures, unchained re-raises, blind excepts, unused `noqa`. On adoption it found a latent closure bug in the suite, a `zip` that could truncate past its own assertion, and two adapters coercing caller-supplied values past their documented error contract. |
| `ruff format` | **Declined** | Would rewrite 41 of 45 files (~1,400 lines at the ~100-column width this code is actually written to) and catch nothing. The codebase is already internally consistent, so a formatter would impose a *different* consistent style rather than fix an inconsistency. |
| `mypy` (default) | **Adopted** over `tanglebrain/` | 11 errors, every one a real Optional-handling gap — `str \| None` reaching a `str` parameter, `RosterEntry \| None` assigned to `RosterEntry`. This is what makes the annotations load-bearing rather than decorative. |
| `mypy --strict` | **Declined** | 62 errors against default mode's 11, and 39 of the 62 were `type-arg` ceremony over bare `dict`/`list` — a large annotation campaign for little beyond what default mode already surfaces. |

The two declines are recorded so the question stops being reopened, not because it can never be
answered differently. **What would change them:** a second regular contributor, at which point hand-
maintained style starts costing review time that a formatter buys back; or a defect that default-mode
mypy structurally cannot see, which is the case `--strict` has to make for itself.

**The cost, stated rather than implied.** `make lint` now blocks `make test`, so a dev-tool release
can turn an unrelated contributor's PR red with no commit to blame — the same failure this project
already paid for once with an unbounded `mcp`. The dev pins are therefore bounded at each tool's real
breaking-change unit rather than at the next major by reflex: ruff is pre-1.0 and breaks at a minor,
and mypy adds *checks* at a minor, which reds untouched code just as hard as an API break. The
canary argument that justifies a wide range for a runtime dependency does not transfer, because no
user resolves a dev tool. When a tool release does red the gate, the gate is fixed, not un-gated.

**What the rule selection says about the whole gate.** Style families — import order, quote shape,
modernization rewrites — are absent on purpose. They are the formatter question under another name,
and answering it differently in the linter would have been a decision made by accident.

The adoption surfaced a pattern worth naming, because it is the same defect one level up from the one
[#113](https://github.com/Jason-Vaughan/TangleBrain/issues/113) was filed about. This code already
carried twelve `# noqa` directives and a `# type: ignore`, written as though these tools were
running — and every one was inert, suppressing a rule nothing had selected or naming the wrong error
code. A suppression that documents a decision without enforcing it is exactly an annotation that
documents a type without verifying it. `RUF100` is in the rule set to keep that from recurring: a
waiver that stops being needed now fails the build.

## Accessibility

Baseline, proportionate to a localhost knob panel: the primary surface is a CLI (terminal
accessibility inherited), and the GUI uses semantic HTML with real labelled form controls — achieved
largely by not reaching for custom widgets.

**The panel's colour layer meets WCAG 2.1 AA**, and that claim is asserted rather than audited once:
`tests/test_gui_contrast.py` parses the palette out of `index.html` and checks every foreground /
background pair a reader sees against the ratio its content type requires — 4.5:1 for normal text
(SC 1.4.3), 3:1 for large text and for the visual information that identifies a control
(SC 1.4.11). Changing a colour reds the suite and names the pair that broke.

Two structural assertions keep that table from falling behind the stylesheet, which is how a
hand-written list of pairs normally rots: every token declared in `:root` must be measured or
explicitly exempt, and no colour literal may appear outside `:root`.

**What the measurement found**, since "we checked" is not a result. Nine of twenty-seven pairs
failed, and they were the ones [#115](https://github.com/Jason-Vaughan/TangleBrain/issues/115)
predicted — muted text and control outlines:

- `--text-muted` cleared 4.5:1 on the page background but not on the two panel surfaces it is
  actually used on (4.34:1 on `--card-bg`, 3.89:1 on `--elevated-bg`). Measuring against the page
  alone would have declared it conformant. Raised `#777` → `#888`.
- Form controls were outlined in `--border` at **1.09:1** against their own fill, so the field
  boundary carried essentially no contrast. Split out as `--field-border` at `#666` (3.03:1), left
  separate from the decorative `--border` on purpose.
- That fix then broke a different criterion, which is the part worth carrying: against the brighter
  rest state the focus outline fell to **1.40:1**, satisfying 1.4.11 while quietly failing
  **2.4.7 Focus Visible**. The focus colour moved to `--primary-bright` (3.07:1 against rest, 9.31:1
  against the fill). A pass on one SC is not a pass.

**Recorded exemptions**, stated rather than left as silent gaps:

- **Decorative borders** — card edges, table rules, pill outlines — are not held to 3:1. SC 1.4.11
  governs what is required to *identify a component or its state*; these identify nothing, and the
  content they enclose carries its own contrast.
- **Disabled controls** are exempt by the explicit carve-out in both SC 1.4.3 and 1.4.11. Noted
  because #115 called disabled states "the usual excuse": ours passes anyway — the disabled label
  uses `--text-muted`, which clears 4.5:1 on every surface it appears on.

**Still not claimed:** full WCAG conformance. This is the colour layer, measured. Keyboard traversal,
screen-reader semantics beyond native controls, motion, and zoom/reflow are unaudited, and a
conformance claim covering them would be the dishonest kind. Tracked as
[#131](https://github.com/Jason-Vaughan/TangleBrain/issues/131) — a ratified scope boundary is a
legitimate answer there, but it has to be *decided* rather than left as prose with nothing behind it.
