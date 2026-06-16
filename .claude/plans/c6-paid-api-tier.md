# C6 — Paid-API tier (issue #2)

**Status:** PLANNED 2026-06-16. The final build feature; split into C6a → C6b → C6c.
Canonical design: plan `tanglebrain.md` §3/§6/§7/§9.6–9.7/§10, contract invariant #3.
PM decisions ratified this session (2026-06-16):
- **Budget enforcement = LiteLLM-only for v1.** TangleBrain stores/displays `budget_usd_month`
  but does NOT itself meter+block on it; the LiteLLM virtual key is the budget-scoped hard cap.
  → C6c shrinks to a docs + display step (no TB-side spend metering this version).
- **C6a scope = gate + adapter + inert parse ONLY** — never routable. One-chunk discipline on
  the heaviest feature.

Verify issue #2 is still OPEN (`gh issue view 2 --json state -q .state`) before building.

---

## Why this is mostly wiring, not greenfield

Earlier chunks cut the seams deliberately:
- `roster.py`: `VALID_KINDS`/`VALID_TIERS` already include `api`; `tier: api` entries already
  parse (`_parse_invoke` simply has no `api` validation branch yet).
- `selector.py:build_adapter`: the gap is explicit — `api` → `AdapterError(... issue #2)`.
- `tests/test_selector.py:test_api_entry_has_no_adapter_yet`: asserts that gap; flips when built.
- `measurement.py:record_task`: already sets `spend_avoided = 0.0` when `tier == "api"`. No change.
- `resolve_key_ref` (`file:`/`env:`): already the custody primitive for the LiteLLM virtual key.

**Architectural insight:** paid APIs are LiteLLM-fronted (§7, decision #7), and LiteLLM speaks
OpenAI-compat. So the `api` adapter's transport ≈ `OpenAICompatAdapter` (HTTP
`/v1/chat/completions` + Bearer virtual key). The new work is the **gate**, **per-entry
metadata**, and (C6b) **last-resort routing** — not the HTTP call. Reuse openai-compat; do not
duplicate the transport.

**No global-config concept exists yet** — only `roster.yaml` (a bare YAML *list*) and
`pricing.yaml`. `api_billing_enabled` is global, not per-entry. Folding it into the roster would
force roster list → mapping (a breaking parse change touching every test). **Decision: new
`tanglebrain/config/settings.yaml` + `tanglebrain/settings.py` loader**, roster untouched.

---

## C6a — Gate + `api` adapter + inert parse (NEXT BUILD SESSION)

**Goal:** a `tier: api` entry fully parses with its new fields and the `api` adapter exists and
works against LiteLLM — but it is **never routable**: `build_adapter` yields it only when the gate
is on AND the entry is enabled; default-off preserved. Zero router changes. "Parse but never
route" holds by construction.

### Work
1. **`tanglebrain/settings.py` + `config/settings.yaml`**
   - `Settings` dataclass with `api_billing_enabled: bool = False`.
   - `load_settings(path=None)` mirroring `load_roster` conventions (default packaged path,
     `SettingsError(ValueError)`, fault-tolerant: missing file → defaults, not a crash).
   - `config/settings.yaml` shipped with `api_billing_enabled: false` + a comment explaining the
     gate is the durable "no paid billing without the explicit toggle" rule (§9.6).
   - Env override seam (`TANGLEBRAIN_API_BILLING`?) — decide in-session; keep minimal.
2. **Roster `api` validation + new per-entry fields** (`roster.py`)
   - `_parse_invoke`: add an `api` branch requiring `base_url` + `model` + `key_ref`
     (LiteLLM endpoint + virtual-key ref). Keep messages in the existing style.
   - New `Invoke`/`RosterEntry` fields: `enabled: bool = True` (per-key kill-switch),
     `budget_usd_month: float | None = None` (stored/displayed only — not enforced TB-side per the
     v1 decision). Validate types (bool / positive number-or-None).
3. **`tanglebrain/adapters/api.py` — `ApiAdapter`**
   - Reuse the openai-compat transport. Cleanest path: a thin subclass/wrapper of
     `OpenAICompatAdapter` (or factor the shared HTTP into a helper both call) — do NOT copy the
     httpx block. `from_entry` resolves `key_ref` → LiteLLM virtual key as Bearer.
   - Re-export `ApiAdapter` from `adapters/__init__.py`.
4. **Gate `build_adapter`** (`selector.py`)
   - Add the `api` branch, but guarded: build the `ApiAdapter` only when
     `settings.api_billing_enabled` AND `entry.enabled`. Otherwise raise `AdapterError` with a
     clear "paid-API billing is disabled (api_billing_enabled=false) / entry disabled" message.
   - `build_adapter` needs settings — thread a `settings` param (default `load_settings()`),
     keeping the existing `inject_delegate` signature back-compat. Check all call sites
     (`cli.run_once`, `router.py`).
5. **Flip `test_api_entry_has_no_adapter_yet`** → assert gated behavior: raises when gate off,
   builds when gate on + enabled, raises when enabled=false.
6. **Roster example:** add a commented-out `tier: api` example entry to `roster.yaml` showing
   `key_ref` → LiteLLM virtual key, `enabled`, `budget_usd_month` (commented so it stays inert).

### Tests (alongside)
- `test_settings.py`: defaults, parse, missing-file tolerance, bad-type errors.
- roster: `api` entry parses; missing `base_url`/`model`/`key_ref` → `RosterError`; `enabled`/
  `budget_usd_month` type validation.
- `test_api_adapter.py`: hermetic (mock httpx) — Bearer from `key_ref`, response parse, error
  surfacing. Mirror `test_openai_compat.py`.
- selector: the gated matrix above.
- Secret-safety: assert `key_ref` is never logged/printed; the adapter resolves it only at call
  time (mirror C5a's open-patch test).

### Docs (same commit — doc-parity rule)
- CHANGELOG `### Added` (paid-API tier, off by default).
- README: paid-API section — how to enable (settings flag), LiteLLM virtual-key custody, the
  off-by-default safety stance.
- **Bundled: contract §2/§6 reconciliation** (Monad-embedded→Mac, profile-model→cost-tier) —
  PM-assigned to this chunk (memory, 2026-06-16). Update `TANGLEBRAIN.md` invariant #3 wording to
  "softened, not reversed" if not already done.

### Out of scope for C6a
- Any routing to `api` (C6b). Any TB-side budget metering (cut for v1). GUI roster editing
  (separate deferred item, not part of #2).

---

## C6b — Last-resort routing

Wire `api` into `Router` as genuine last resort: only after all `can_orchestrate` subs are
exhausted/failed (and gate-on), fall through to an enabled `api` entry. Preserve failover +
rate-limit annotation. `measurement` already records api spend with `avoided = 0`.
Decision to make in-session: does "last resort" live in `Router.route` (extend the candidate
list with api entries appended last, gate-guarded) or a distinct fallback step? Keep the router a
deterministic control plane — no auto-classification.

## C6c — Budget display + runbook (thin, per v1 decision)

LiteLLM-only enforcement → no TB-side metering. This chunk = surface `budget_usd_month` and
`enabled` in `--stats` / the GUI roster view (read-only), and a runbook documenting how to mint a
budget-scoped LiteLLM virtual key on Monad and reference it via `key_ref`. May merge into C6b if
small.

---

## Carry-forward / guardrails
- Repo is PRIVATE on Free plan → **merge PRs MANUALLY** (`gh pr merge <N> --squash
  --delete-branch`), never `--auto`. No CI gates configured.
- One chunk per session; tests + docs alongside; Critic review after C6a (medium+).
- Cross-session: this is a builder repo; suggestions to the Monad-1 PM go via paste-back, never
  direct commits there.
- Archive this plan to `.claude/plans/archive/` when issue #2 closes.
