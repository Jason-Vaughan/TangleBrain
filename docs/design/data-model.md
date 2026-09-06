# Data Model

TangleBrain has no database. Every piece of state is a file on local disk, and the model is small
enough to state completely here.

[`ARCHITECTURE.md`](../../ARCHITECTURE.md) is canonical for *how the system is built*; this document
is canonical for *what data exists, where it lives, and whether it survives a crash*.

## Invariants

These bind. Departing from one is a decision to record and justify.

- **No persisted structure ever holds a credential value — only a reference.** `key_ref` holds
  `env:NAME` or `file:PATH`, resolved lazily at call time and discarded.

  *Why:* the repo is public and the roster is the file operators are most likely to paste into an
  issue or a screenshot. Reference-not-value means a leaked config, a shared panel screenshot, or an
  accidental `git add` exposes nothing. A control that survives operator carelessness is worth more
  than one that assumes care.

- **Prompt and response text is never written to disk.** Measurement persists derived counts and
  routing metadata only.

  *Why:* structural beats procedural. A redaction filter can be bypassed by the next code path that
  forgets it; "there is nothing to redact" cannot. This is also what makes the usage log safe to
  keep forever and safe to render in a browser.

- **An upgrade never writes the operator's roster.** It resolves outside the repo
  (`$TANGLEBRAIN_ROSTER` → XDG config → packaged example).

  *Why:* the roster is hand-authored and hand-commented — it is the operator's work, not ours. A
  `git pull` or `pip install -U` that clobbered it would destroy something unversioned and
  unrecoverable. This is a supported guarantee, not a convention.

- **New record fields are additive and optional.** A reader predating a field stays correct; a
  missing field reads as "not applicable", never as an error.

  *Why:* usage records written by every past version are still on disk and still get rolled up.
  There is no migration story for an append-only log, so forward-compatibility has to be a property
  of the format rather than an event. `task_id`, `parent_task_id`, and `origin` were all added under
  this rule.

## Entities

### `RosterEntry` — one routable backend

Defined in `tanglebrain/roster.py`. The config model the whole system is driven by.

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | Stable identifier. Doubles as the `model` value on the serve endpoint and as a delegate `target`, so **renaming one is a breaking change to two public surfaces**. |
| `tier` | `str` | `local` \| `sub` \| `api`. Drives cost ranking (`TIER_RANK`) and the paid gate. |
| `invoke` | `Invoke` | Adapter selection + transport config (below). |
| `cost` | `str \| None` | Human-readable cost hint, surfaced in `delegate_targets()`. |
| `good_at` | `list[str]` | Capability tags. Matched against a `task` hint for task-fit selection. |
| `can_orchestrate` | `bool` | Eligible for the top-level orchestrator rotation. |
| `can_delegate` | `bool` | Eligible as a delegate target. Deliberately independent of `can_orchestrate` — a backend can be good at bulk sub-tasks without being fit to run the whole job. |
| `enabled` | `bool` | Per-entry switch. For `tier: api` this is one of **two** independent gates. |
| `budget_usd_month` | `float \| None` | Operator-declared ceiling. |

### `Invoke` — how a backend is reached

| Field | Type | Notes |
|---|---|---|
| `kind` | `str` | `openai-compat` \| `cli` \| `api`. Selects the adapter. |
| `base_url` | `str \| None` | HTTP kinds. |
| `model` | `str \| None` | Model name passed to the backend. |
| `cmd` | `list[str] \| None` | CLI kind. A **list, never a string** — the subprocess is spawned without a shell, so this is not a place a shell metacharacter can hide. |
| `scrub_env` | `list[str]` | Variables stripped from the child environment, so the call uses the intended credential path. |
| `parse` | `str \| None` | Output parser selector. |
| `delegate_args` | `list[str]` | Arguments that hand the backend the local delegate tool. |
| `key_ref` | `str \| None` | **A reference, never a secret.** `env:NAME` or `file:PATH`. |

### `Settings` — global switches

Defined in `tanglebrain/settings.py`. Strictly bool-validated: a non-bool value can never
coincidentally enable a feature.

| Field | Type | Default | Notes |
|---|---|---|---|
| `api_billing_enabled` | `bool` | `false` | The paid gate. |
| `classifier_gate_enabled` | `bool` | `false` | The classifier gate. |
| `delegate_max_concurrency` | `int \| None` | `None` | `None` → an `os.cpu_count()`-derived default. A per-call value may lower it, never raise it. |

### Usage record — one JSON line per task or delegation

Written by `tanglebrain/measurement.py`. Always-present fields:

`ts` · `kind` (`task` \| `delegate` \| `failure`) · `path` · `tier` · `model` · `in_tokens_est` ·
`out_tokens_est` · `cloud_equiv_usd` · `spend_avoided_usd` · `pricing_ref`

Optional, written only when present: `task_id` · `parent_task_id` · `origin` (`cli` \| `gui` \|
`serve`) · `failures` (#100 — the `[{entry, error}, …]` attempts lost before the outcome: the
failovers behind a served task, or every attempt on a `kind: "failure"` record).

Two things this record deliberately does **not** contain: the prompt and the response. Token counts
are estimated with a uniform `chars/4` heuristic over the text, and the text is then discarded.
There is no redaction step because there is nothing to redact.

`spend_avoided_usd` is `0.0` for `tier: api` (real spend avoids nothing) and for `kind: "failure"`
records (a task no backend served avoided nothing), and equal to `cloud_equiv_usd` otherwise.

### Lifetime totals — `totals.json`, one object beside the log

The measurement store has two halves. This file holds the **lifetime** aggregates permanently; the
log holds a **window** of per-task rows. Only the window can be bounded, so the lifetime claim needs
somewhere the rows can be folded *into* — otherwise capping the log means the spend-avoided headline
shrinks, which is the one thing it must never do.

Fields, each answering a question `--stats` asks:

`tasks` · `failures` · `lost_attempts` · `by_tier` · `by_origin` · `in_tokens_est` ·
`out_tokens_est` · `cloud_equiv_usd` · `spend_avoided_usd` · `pricing_refs` ·
`delegates` (`count`, `by_backend`, `in_tokens_est`, `out_tokens_est`, `cloud_equiv_usd`)

`pricing_refs` is the set of reference-pricing revisions the stored figure was computed under. It is
stored rather than derived because folding rows destroys the per-row `pricing_ref` evidence, and a
caveat that has to outlive its rows must be captured before they go.

**The delegates' `by_parent` tree is deliberately absent.** It carries one key per parent task id,
so its cardinality grows without bound and it cannot live in a file that must stay small. It is
inherently window-scoped, and `--stats` and the GUI panel both label it as such rather than letting
a window count sit unmarked beneath a lifetime headline.

**There is deliberately no schema-version field.** The forward-compatibility contract is the same
one the usage record honours — an unknown key is ignored, a missing key reads as zero — and that is
what makes a version number unnecessary: a version advertises that a breaking revision is possible,
and additive-only exists so that one never has to be.

**Reading is total.** An absent, truncated, or corrupt file reads as all-zeros, so a log that has
never been compacted rolls up exactly as it did before this file existed, and a damaged one yields a
smaller number rather than an error.

## Persistence boundaries

The question the architecture documents do not answer outright: **what survives a crash, and what
does not.**

| Data | Location | Durability | If lost |
|---|---|---|---|
| Roster | `$TANGLEBRAIN_ROSTER` → `~/.config/tanglebrain/roster.yaml` → packaged example | **Durable, operator-owned** | Falls back to the packaged example — one active local entry. Degrades to the safe default rather than failing. |
| Settings | `config/settings.yaml` | Durable | Both gates read as off. Fails safe. |
| Pricing reference | `config/pricing.yaml` | Durable, packaged | Cost figures unavailable; routing unaffected. |
| Rotation cursor | `<state root>/router-state.json` | **Durable, data-tier** | Rotation restarts from the beginning. Harmless — it is a fairness hint, not correctness. |
| Usage log (row window) | `<state root>/usage.jsonl` | **Durable, data-tier** | **Today: the entire lifetime spend-avoided figure, permanently.** Nothing folds rows into `totals.json` yet, so the rows still carry the whole figure. Once the fold lands ([#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101)) this becomes recent per-task detail only. See below. |
| Lifetime totals | `<state root>/totals.json` | **Durable, data-tier** | The lifetime figure falls back to whatever the surviving rows sum to — a smaller number, never an error. |
| Config backups | `<state root>/backups/` | **Durable, data-tier** | The only copy of a hand-edited roster or pricing file the GUI replaced. |
| In-flight request | memory | None | No retry, no queue, no journal. A crash mid-route loses the request and the caller sees a failure. Deliberate — this is a router, not a job system. |

**The state root** resolves `TANGLEBRAIN_STATE_DIR` → `$XDG_DATA_HOME/tanglebrain` →
`~/.local/share/tanglebrain`. It is the XDG **data** tier, not the cache tier: nothing under it is
a cache. The usage log is the only record of accumulated spend-avoided and is never
reconstructible — prompt and response text is never persisted, by design — and a config backup is
the only copy of something the operator hand-edited. `~/.cache` is *defined* as a directory any
cleanup tool may clear at will, so both were one `brew cleanup` from gone.

An install that predates the move is migrated on first run: every entry in `~/.cache/tanglebrain/`
is copied forward, **the originals are left in place** so a downgrade still finds its history, and
one notice on stderr names both directories. The copy is per-entry and skips what is already
there, so an interrupted migration completes on the next run.

**Still open:** the log is unbounded. The permanent total the rows can be folded into now exists
and `--stats` already reads it, but nothing folds into it and nothing prunes the log, so it still
grows without limit ([#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101)).

## Concurrency

- Appends to `usage.jsonl` are serialized by a **process-level** lock (`measurement._LOG_LOCK`),
  which covers `delegate_many`'s thread fan-out — the actual concurrency this product creates.
- It does **not** cover two TangleBrain *processes* writing at once (a CLI run and a serve request).
  In practice each write is a single short `write()` of one line under a few KB, which POSIX appends
  atomically in the common case; the format is line-oriented and the reader skips unparseable lines,
  so the worst realistic outcome is one corrupt record rather than a corrupt file. Stated because it
  is a real gap in the guarantee, not because it is currently hurting anything.

## Validation

- Roster and settings parse strictly. A malformed entry is an error at load, not a surprise at call
  time.
- The GUI's roster write-back validates each field, writes atomically, keeps a timestamped backup,
  and preserves comments — the operator's file is hand-authored and hand-commented, so a write that
  ate comments would be a data-loss bug.
