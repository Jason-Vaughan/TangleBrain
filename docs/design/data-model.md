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

## Persistence boundaries

The question the architecture documents do not answer outright: **what survives a crash, and what
does not.**

| Data | Location | Durability | If lost |
|---|---|---|---|
| Roster | `$TANGLEBRAIN_ROSTER` → `~/.config/tanglebrain/roster.yaml` → packaged example | **Durable, operator-owned** | Falls back to the packaged example — one active local entry. Degrades to the safe default rather than failing. |
| Settings | `config/settings.yaml` | Durable | Both gates read as off. Fails safe. |
| Pricing reference | `config/pricing.yaml` | Durable, packaged | Cost figures unavailable; routing unaffected. |
| Rotation cursor | `~/.cache/tanglebrain/router-state.json` | **Ephemeral, cache-tier** | Rotation restarts from the beginning. Harmless — it is a fairness hint, not correctness. |
| Usage log | `~/.cache/tanglebrain/usage.jsonl` | **Ephemeral, cache-tier** | Every historical spend-avoided figure is gone permanently. See below. |
| In-flight request | memory | None | No retry, no queue, no journal. A crash mid-route loses the request and the caller sees a failure. Deliberate — this is a router, not a job system. |

Both mutable paths honor `TANGLEBRAIN_STATE_DIR`.

**Recorded risk, not a defect.** Both mutable state files live under `~/.cache/`, which by XDG
convention is the tier a user or a cleanup tool may delete at any time. For `router-state.json` that
is exactly right. For `usage.jsonl` it is a genuine mismatch: the log is the *only* record of
accumulated spend-avoided, it is never reconstructible, and it accrues value over months — so
anything that clears the cache silently destroys the one number the product exists to show you.

Moving it to `~/.local/share/` would match its real durability class, but that is a migration
affecting existing installs, so it is recorded as an open decision rather than changed unilaterally.
Tracked with the unbounded-growth question in
[#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101) — same owner, probably the same
answer.

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
