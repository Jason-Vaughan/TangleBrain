# C4 — Measurement / "spend avoided" rollup (§8)

**Issue:** Closes #10 · **Chunk:** C4 (plan `tanglebrain.md` §8, §10) · **Bump:** minor (user-visible `--stats`)

> **Step 0 (project rule):** before building, copy this plan to
> `/Users/jasonvaughan/Documents/Projects/TangleBrain/.claude/plans/c4-measurement.md`
> and reference that project-local absolute path in memory/handoffs (global "Keep plans local" rule).

## Context

Frontier-first delegation is now live end-to-end (C3b, v0.2.0): every `tanglebrain "…"` is routed to the
cheapest tier that can do the job (free local → flat-rate subs → paid API last). The north star is driving
ongoing compute cost **down**, but right now that saving is invisible — nothing records which tier served a
task or what it would have cost on a paid frontier API. C4 makes the savings visible: log each routed task and
roll up a "spend avoided" figure (same methodology family as Monad-1's `monad-stats` `costSaved`). This is also
the data the §6 routing-evolution gate (local-classifier) depends on later. **Greenfield logging layer; no
change to routing behavior or adapter return types.**

## Key constraints discovered (exploration)

- Adapters and `Router.route()` all return plain `str`. The local openai-compat response *has* `data["usage"]`
  but drops it (`adapters/openai_compat.py:~177`); **CLI subs expose no usable token counts**. → per the locked
  decision, C4 estimates tokens **uniformly** via a `chars/4` heuristic on visible prompt+response (no adapter
  changes, no behavior change, one consistent methodology across tiers).
- `tanglebrain/router.py` already has the state-file idiom to mirror: `default_state_path()` (honors
  `TANGLEBRAIN_STATE_DIR`, falls back to `~/.cache/tanglebrain/`), plus fault-tolerant `_read_cursor`/
  `_write_cursor` (bad/missing state never crashes). The usage log reuses this exactly.
- `run_once()` (`cli.py:77`) is the single seam covering all three paths (`--model`, `--local`, default router).
  It already knows the entry for the first two; for the router path the served entry must be surfaced.

## Decisions (per Decision Framework)

1. **Tokens:** uniform `chars/4` heuristic on visible text. Alt (real local `usage` + heuristic for subs)
   rejected: inconsistent across tiers and inflated by dropped gpt-oss reasoning tokens.
2. **Pricing:** config-driven in `tanglebrain/config/pricing.yaml`, **seeded from monad-stats `costSaved`
   constants** (reference model + input/output $/Mtok) so the two projects stay aligned. C5's knob GUI tunes it.
3. **Storage:** append-only **JSONL** at `~/.cache/tanglebrain/usage.jsonl` (one line per task). Append-friendly
   for concurrent writes and preserves full history; rollup reads + tolerates malformed lines.
4. **CLI:** add a `--stats` flag (prompt becomes optional); minimal change to the existing single-positional parser.
5. **Scope:** meter **top-level routed tasks only** (the three `run_once` paths). Delegate sub-calls
   (`delegate.run_local_delegate`, invoked *inside* an orchestrator's flat-rate session) are **out of scope** —
   metering them would double-count against the parent sub task. Noted as a possible follow-up.

## ⚠️ Build-time input needed

Pricing decision = "mirror monad-stats constants," which live in the Monad-1 repo (I can't read from here).
**PM to paste** the `monad-stats` `costSaved` reference: model label + input $/Mtok + output $/Mtok. Until
provided, ship `pricing.yaml` with a clearly-labeled **placeholder** and have the rollup print
`pricing: PLACEHOLDER` so no figure is mistaken for canonical.

## Implementation

### 1. New module — `tanglebrain/measurement.py`
Mirror the `router.py` state-file idiom; all I/O fault-tolerant (a logging failure must **never** affect the
returned answer). JSDoc-style docstrings on every function (project rule).

- `default_log_path() -> Path` — `usage.jsonl`, same `TANGLEBRAIN_STATE_DIR`/`~/.cache/tanglebrain` resolution
  as `default_state_path()`.
- `load_pricing() -> Pricing` — read packaged `config/pricing.yaml` (reuse roster's PyYAML import); fall back
  to a labeled placeholder constant if missing. Carries `reference_model`, `input_per_mtok`, `output_per_mtok`,
  `is_placeholder: bool`.
- `estimate_tokens(text: str) -> int` — `max(1, len(text)//4)` when non-empty; documents the approximation.
- `cloud_equiv_usd(in_tok, out_tok, pricing) -> float` — `in_tok/1e6*input + out_tok/1e6*output`.
- `record_task(*, path, entry, prompt, response, log_path=None) -> None` — build the record, append one JSON
  line; wrap everything in `try/except Exception: return` so it can't break routing. `entry` may be `None`
  (router failed to surface) → record with `tier/model = "unknown"`.
  Record shape: `{ts, path, tier, model, in_tokens_est, out_tokens_est, cloud_equiv_usd, spend_avoided_usd,
  pricing_ref}`. `spend_avoided_usd == cloud_equiv_usd` for `local`/`sub` (no paid tier yet); when the `api`
  tier lands (#2), api tasks set avoided `= 0` — the field exists now so the rollup needn't change later.
- `read_records(log_path=None) -> list[dict]` — read JSONL, skip malformed lines (tolerant).
- `rollup(records) -> dict` — totals: task count, by-tier counts, summed est tokens, summed spend avoided.
- `format_rollup(summary, pricing) -> str` — human block; appends `pricing: PLACEHOLDER` caveat when applicable.
- `MeasurementError(Exception)` for parse/IO surfaced to the CLI (caught in `main()` alongside the others).

### 2. Surface the served entry — `tanglebrain/router.py`
Add `self.last_served: RosterEntry | None = None` in `__init__`; set it to the winning `entry` right before the
success `return` in `route()` (~line 185/191). New attribute only — return type and rotation logic unchanged.

### 3. Meter the seam — `tanglebrain/cli.py` `run_once()`
Refactor the three one-liner paths to capture `(entry, text)`, call `record_task(...)`, then return `text`:
`--model` → `select_by_id` entry; `--local` → `select_local` entry; default → `router.last_served` after
`route()`. No change to what `run_once` returns.

### 4. CLI rollup — `tanglebrain/cli.py` `build_parser()` / `main()`
- Add `--stats` (`action="store_true"`); make positional `prompt` `nargs="?"` default `None`.
- In `main()`: if `args.stats` → `print(format_rollup(rollup(read_records()), load_pricing()))`, return 0
  (no prompt required). Else if `prompt is None` → `parser.error("prompt is required (unless --stats)")`.
- Add `MeasurementError` to the existing `except (...)` tuple.

### 5. Config — `tanglebrain/config/pricing.yaml`
Packaged default with `reference_model`, `input_per_mtok`, `output_per_mtok`, and a header comment citing the
monad-stats methodology. Seeded with the placeholder until the PM provides constants. Verify it ships via
`pyproject.toml` package-data (roster.yaml already does — mirror that).

### 6. Tests — `tests/test_measurement.py` (hermetic, temp `log_path`; never touch real `~/.cache`)
`estimate_tokens` boundaries; `cloud_equiv_usd` math with known pricing; `record_task` appends well-formed
JSON and accumulates across calls; **fault-tolerance** (unwritable path → no raise; `entry=None` →
`tier="unknown"`); `read_records` skips a corrupt line; `rollup` totals + by-tier; `format_rollup` renders +
placeholder caveat; `load_pricing` reads packaged file and falls back when absent. Extend `tests/test_cli.py`:
`--stats` prints rollup and returns 0 with no prompt (mock `read_records`); each `run_once` path writes one
record (mock adapters/`Router`, assert against a temp log).

### 7. Docs
`CHANGELOG.md` → `### Added` under `[Unreleased]` (C4 feature → minor). README: document `tanglebrain --stats`
and the methodology/`pricing.yaml`. Mention the delegate-sub-call out-of-scope note.

## Verification

- `make test` — full hermetic suite (lint + unittest) green.
- Manual end-to-end: `tanglebrain --local "ping"` two or three times, then `tanglebrain --stats` shows a non-zero
  task count and a spend-avoided figure (flagged `PLACEHOLDER` until real constants land). Confirm
  `~/.cache/tanglebrain/usage.jsonl` has one line per run and routing output is unchanged.
- `make test-live` unaffected (no new network).

## Wrap

Independent Critic review (medium work) before merge. Branch `feat/c4-measurement` → PR with What/Why/Test-plan,
`Closes #10`. Repo PRIVATE on Free → **merge manually** (`gh pr merge <N> --squash --delete-branch`), never
`--auto`. Update `.tangleclaw/memories/MEMORY.md` (C4 shipped; next = C5). One chunk — do not start C5.
