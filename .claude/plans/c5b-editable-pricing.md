# C5b — editable pricing knob + served-entry fix

**Chunk:** C5b (issue **#13**, second slice of plan §10 "C5 — Knob GUI"). **Bump:** minor (panel
gains write capability). Builds directly on C5a's `tanglebrain/gui/` package (now shipped, v0.4.0).

> **Step 0 (project rule):** copy this plan to
> `/Users/jasonvaughan/Documents/Projects/TangleBrain/.claude/plans/c5b-editable-pricing.md`
> and reference that project-local absolute path in memory/handoffs.

## Context

C5a shipped the read-only knob panel. C5b adds the first **editable** knob — the pricing reference
in `tanglebrain/config/pricing.yaml`, which directly drives the spend-avoided figure the PM tunes.
Scope is **pricing only** (PM decision): the roster is dense with load-bearing inline comments that a
web-driven `yaml.dump` would erase, so roster editing waits for a comment-preserving mechanism in a
later chunk. This chunk also folds in the C5a follow-up: kill the `_last_served` log-re-read race by
having `run_once` return the served entry directly.

## Decisions (per Decision Framework)

1. **Pricing only** (PM). Roster knob-editing deferred (comment-preservation unsolved zero-dep).
2. **Preserve pricing.yaml's header comment** by re-emitting a fixed canonical header on every save
   (the file is small and the methodology header is static/known) — so GUI edits never strip the
   doc, with **no new dependency**. Bare `yaml.dump` rejected (would erase the header).
3. **Strict validation on save** (unlike `load_pricing`, which is deliberately lenient for reads): a
   bad value is *rejected with a clear error*, never written. `placeholder` stays an explicit field
   the PM controls (not auto-flipped).
4. **Atomic write + timestamped backup.** Write a temp file then `os.replace` (atomic on POSIX);
   copy the prior file to `<state_dir>/backups/pricing-<ts>.yaml` first (backups go to the cache/
   state dir, NOT the repo config dir, to avoid cluttering tracked files).
5. **Edits target the repo's `tanglebrain/config/pricing.yaml`** (the canonical config) — so a GUI
   edit is git-visible and the PM commits it. Documented in the runbook.
6. **No secret surface:** pricing editing never touches `key_ref`/roster, so C5a's secret-safety is
   unaffected; the save endpoint accepts only the four pricing fields.

## Implementation

### 1. Backend — `tanglebrain/measurement.py`
- `PRICING_HEADER` constant: the canonical comment block currently atop `config/pricing.yaml`
  (methodology / monad-stats anchor note).
- `validate_pricing(data: dict) -> Pricing` — strict: `reference_model` non-empty str;
  `input_per_mtok`/`output_per_mtok` float and `>= 0`; `placeholder` bool. Raises `ValueError`
  (surfaced to the panel as a clean error) on any violation.
- `save_pricing(pricing: Pricing, path=None) -> None` — render `PRICING_HEADER` + the four keys,
  back up the existing file to the state dir, then atomic-write. Reuse the `TANGLEBRAIN_STATE_DIR`
  resolution already used by `default_log_path`.
- Small helpers: `_atomic_write(path, text)` (temp + `os.replace`) and `_backup_dir()` (under the
  state dir). `datetime` for the backup timestamp (normal Python — fine here).

### 2. Backend — `tanglebrain/cli.py` (`_last_served` race fix)
- `run_once(..., return_served: bool = False)`: compute the served entry for all three paths
  (`select_by_id` / `select_local` / `router.last_served`) — already done for metering — and when
  `return_served=True` return `(text, {"path", "tier", "model"} | None)`. Default path returns
  plain `str`, so `cli.main` and all existing call sites/tests are unchanged.

### 3. GUI — `tanglebrain/gui/views.py`
- `save_pricing_view(payload: dict) -> dict` — `validate_pricing(payload)` → `save_pricing(...)`;
  return `{"ok": True, "pricing": view_pricing()}` or `{"ok": False, "error": ...}` on ValueError.
- `run_prompt`: use `run_once(..., return_served=True)` and return that `served` directly; **delete
  `_last_served()`** (now dead — janitor). Kills the threading race (no shared-log re-read).

### 4. GUI — `tanglebrain/gui/server.py`
- Route `POST /api/pricing` → parse JSON object (same bad-JSON/non-object guards as `/api/run`) →
  `save_pricing_view(...)` → `200` if ok else `400`.

### 5. GUI — `tanglebrain/gui/static/index.html`
- Make the pricing card editable: number inputs for input/output $/MTok, a text input for
  `reference_model`, a `placeholder` checkbox, and a **Save** button with a confirm step (it
  overwrites config). POST `/api/pricing`; on success re-render the card + the stats caveat. Show
  validation errors inline. Keep the existing escaping discipline.

### 6. Docs
- `CHANGELOG.md` `### Added` (minor). README "Knob panel" section: note pricing is now editable
  (writes the tracked `config/pricing.yaml`, atomic + backup), roster still read-only (→ later).

## Verification

- `make test` — full hermetic suite green incl. new tests:
  - `save_pricing` round-trips (write → `load_pricing` reads back the values), re-emits the header
    (assert a header line present), creates a backup, leaves no `.tmp`; `validate_pricing` rejects
    negative/non-numeric rates and empty `reference_model`.
  - `run_once(return_served=True)` returns correct `(text, served)` per path (mock adapters/Router);
    plain `run_once` still returns `str` (existing tests unchanged).
  - `run_prompt` uses the returned served (no `read_records` call); `save_pricing_view` happy +
    invalid; `dispatch` `POST /api/pricing` valid → 200, invalid → 400, bad JSON → 400.
- Manual: `tanglebrain-gui` → edit a rate, Save → card updates, `tanglebrain --stats` reflects the
  new pricing, `git diff tanglebrain/config/pricing.yaml` shows only value changes (header intact),
  a backup exists under the state dir; run a prompt and confirm the served tier shows (no log race).

## Wrap

Independent Critic review before merge (focus: write-safety, validation completeness, header
preservation, the run_once refactor not breaking existing callers). Branch `feat/c5b-editable-pricing`
→ PR with What/Why/Test-plan, `Closes #13`. Repo PRIVATE on Free → **merge MANUALLY**
(`gh pr merge <N> --squash --delete-branch`), never `--auto`; feature → PM sign-off. Update
`MEMORY.md` (C5b shipped; remaining build chunk = #2 paid-API tier; roster-editing still deferred).
One chunk — do not start #2.
