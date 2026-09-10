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

  *Scope:* structural on the success path and at every adapter raise site that reproduces a model
  completion — those report shape, not content. It is a judgement at the error-diagnostic strings a
  provider or a CLI produces, which are kept verbatim on purpose;
  [`security-model.md`](security-model.md) § Known gaps enumerates them and is the authoritative
  accounting.

- **An upgrade never writes the operator's roster.** It resolves outside the repo
  (`$TANGLEBRAIN_ROSTER` → XDG config → packaged example).

  *Why:* the roster is hand-authored and hand-commented — it is the operator's work, not ours. A
  `git pull` or `pip install -U` that clobbered it would destroy something unversioned and
  unrecoverable. This is a supported guarantee, not a convention.

- **New record fields are additive and optional.** A reader predating a field stays correct; a
  missing field reads as "not applicable", never as an error.

  *Why:* usage records written by every past version are still on disk and still get rolled up.
  There is no migration story for an append-only log, so forward-compatibility has to be a property
  of the format rather than an event. `task_id`, `parent_task_id`, `linkage_lost`, and `origin` were
  all added under this rule.

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

Optional, written only when present: `task_id` · `parent_task_id` · `linkage_lost` (`true` only,
when a delegate reaches measurement without its expected parent id) · `origin` (`cli` \| `gui` \|
`serve`) · `failures` (#100 — the `[{entry, error}, …]` attempts lost before the outcome: the
failovers behind a served task, or every attempt on a `kind: "failure"` record).

Two things this record deliberately does **not** contain: the prompt and the response. Token counts
are estimated with a uniform `chars/4` heuristic over the text, and the text is then discarded.
There is no redaction step because there is nothing to redact.

The one path that can carry text into a record is `failures[].error`. Every adapter message that
could reproduce a completion reports its shape instead; the provider- and CLI-produced diagnostics
kept verbatim are enumerated in [`security-model.md`](security-model.md) § Known gaps.

`spend_avoided_usd` is `0.0` for `tier: api` (real spend avoids nothing) and for `kind: "failure"`
records (a task no backend served avoided nothing), and equal to `cloud_equiv_usd` otherwise.

### Lifetime totals — `totals.json`, one object beside the log

The measurement store has two halves. This file holds the **lifetime** aggregates permanently; the
log holds a **window** of per-task rows. Only the window can be bounded, so the lifetime claim needs
somewhere the rows can be folded *into* — otherwise capping the log means the spend-avoided headline
shrinks, which is the one thing it must never do.

Fields, each answering a question `--stats` asks:

`tasks` · `failures` · `lost_attempts` · `by_tier` · `by_origin` · `in_tokens_est` ·
`out_tokens_est` · `cloud_equiv_usd` · `spend_avoided_usd` · `pricing_refs` · `by_model` ·
`by_day` · `by_day_since` ·
`delegates` (`count`, `by_backend`, `in_tokens_est`, `out_tokens_est`, `cloud_equiv_usd`)

#### `by_model` and `by_day` — two bounded slices of the headline

Both carry `{count, in_tokens_est, out_tokens_est, cloud_equiv_usd, spend_avoided_usd}` per key, and
both are populated from **task records only** — delegate and failure rows are already held out of
`spend_avoided_usd`, so including them would leave a breakdown that does not add up to the figure it
breaks down. **The parts sum to the headline**, and that is the property that makes them worth
trusting rather than merely present.

`by_model` is keyed on the **roster `id`**, matching `delegates.by_backend`. That is the only
per-model identifier a record carries — the record's `model` field holds the roster id despite its
name — so keying on a backend's own model string would require a new record field, not a choice
between two things already stored. *Accepted limitation:* renaming a roster entry orphans its
accumulated history under the old key, exactly as `by_backend` does today.

`by_day` is keyed `YYYY-MM-DD` (UTC, from the record's `ts`). A record whose timestamp is missing or
unreadable still reaches `by_model` and the headline but contributes **no** day bucket — a day cannot
be invented, and attributing old spend to today's bucket would bend the very chart the field exists
for.

**`by_day` is the first deliberately window-scoped field in the lifetime half, and it does not sum to
`spend_avoided_usd`.** It retains the newest **400** days and the oldest are evicted when the totals
are written. The cap is what makes the field admissible at all: one key per day grows without bound,
which is the same property that keeps `delegates.by_parent` out of this file entirely. Eviction runs
only where the totals become a file, never in the shared summation — applying it on the read path
would shrink a figure the reader still has rows on disk for.

The retention number is **policy, not format**: changing it breaks nothing on disk. It is only
usefully changed in one direction, though — narrowing works, while **widening recovers nothing**,
because the evicted days are gone. That asymmetry is why it is set well above the 90 days the first
consumer needs.

#### `by_day_since` — and what it is *not*

The day per-day recording began, stamped once and never moved, eviction included.

**It is not the boundary a renderer may draw from.** Once eviction bites it is *older* than the
oldest surviving bucket, so drawing from it would paint the evicted span as **$0** — asserting no
activity across days that were merely dropped, and doing so only on long-lived stores where it is
least visible. The drawable boundary is `min(by_day)`, which needs no field: before the earliest key
absence means *unknown*; between keys, absence means a genuine zero-activity day.

What it answers instead is the question `by_day` cannot: **do these per-day figures cover the whole
life of the store?** Read against the lifetime `spend_avoided_usd`, it is what lets a reader be told
the chart starts later than the headline does — and nothing else survives the first eviction to say
so.

`pricing_refs` is the set of reference-pricing revisions the stored figure was computed under. It is
stored rather than derived because folding rows destroys the per-row `pricing_ref` evidence, and a
caveat that has to outlive its rows must be captured before they go. `--stats` reads it: a figure
spanning one revision is labelled with that revision, and one spanning several says how many and why
that is expected — history is priced when it happens and an edit never restates it.

**A span witnesses an edit, not a rate change.** `pricing_ref` carries the reference-model *label*,
which is all of a pricing revision a record holds, so relabelling raises the caveat over a figure
nothing moved underneath, and editing the rates while keeping the label leaves a real span
undetected. Widening the record to carry the rates themselves would catch both and is an
**accepted limit** rather than open work: the caveat exists to stop one label being asserted over a
mixed history, which it does, and the label is the part of a pricing revision an operator recognises.

**The delegates' `by_parent` tree is deliberately absent.** It carries one key per parent task id,
so its cardinality grows without bound and it cannot live in a file that must stay small. It is
inherently window-scoped, and `--stats` and the GUI panel both label it as such rather than letting
a window count sit unmarked beneath a lifetime headline.

`delegates.linkage_lost` is present in `totals.json` because it is a bounded scalar. It counts
parentless `kind: delegate` rows, including legacy rows written before `linkage_lost` existed, and
therefore survives compaction. Parentless `kind: task` rows are top-level roots and do not count.

**There is deliberately no schema-version field.** The forward-compatibility contract is the same
one the usage record honours — an unknown key is ignored, a missing key reads as zero — and that is
what makes a version number unnecessary: a version advertises that a breaking revision is possible,
and additive-only exists so that one never has to be.

**Reading is total.** An absent, truncated, or corrupt file reads as all-zeros, so a log that has
never been compacted rolls up exactly as it did before this file existed, and a damaged one yields a
smaller number rather than an error.

**Writing is non-destructive.** Ignoring an unknown key on read would delete it on write, so the
writer carries every field it does not recognise straight through from the file it is replacing, at
any depth. Without that, the first compaction performed by an older TangleBrain would permanently
destroy fields a newer one wrote — and "every version of TangleBrain that shares the file" is a
named consumer of this format ([`boundaries.md`](boundaries.md)). *Carrying a field is not
maintaining it:* a version that does not know a field cannot add the folded rows' contribution to
it, so the value goes stale rather than being lost. Stale and recoverable beats absent, which is
why the round-trip preserves rather than drops.

**Two exceptions, and both are the same distinction.** Carry-through preserves a key *this version
does not know*; it must not preserve a key *this version deliberately removed*, and nothing in the
file itself tells the two apart. `delegates.by_parent` is never stored at all. **`by_day` is stored
but trimmed**, so at that one path an absent key means **evicted**, not unknown, and is dropped
rather than carried. Without that, every day the cap evicted would be read straight back out of the
file it was replacing and put back — the cap would hold in memory and never on disk, and the file
would grow without bound.

**The exception is narrower than it sounds, and the narrowness is the point:** a day present in
*both* still merges, so a field a newer TangleBrain added inside a **retained** day survives exactly
as any other unknown field does. Only the keys this writer removed are treated as removed.

**The cost, stated because this is the one place the compatibility story bends:** an older
TangleBrain that folds a newer one's file applies *its own* retention, so day buckets the newer
version was keeping can be dropped by the older one. Days are lossy by construction — that is what
the cap means — and the lifetime figures they roll into are not, so the loss is bounded to the
per-day view. It is a real narrowing of "an older version cannot delete what a newer one wrote", and
it applies to `by_day` alone.

**Writing is atomic, and durable.** The file is staged beside itself and renamed over, so a crash
mid-write leaves the previous totals whole and a reader never sees half an object. The staging file
is fsynced before the rename and the containing directory after it — atomicity alone orders the two
compaction writes only against a killed *process*, and the ordering has to survive a power loss too.
The directory sync is POSIX-only and skipped elsewhere; the file sync is not platform-specific.

### Compaction — how a row becomes a total

Compaction reads the oldest rows, adds them into `totals.json`, and only then removes them from the
log. It runs on a **size cap**: every recorded task checks the log, and crossing `MAX_LOG_BYTES`
(5 MiB, roughly 15,000 records) folds the oldest rows away until what remains fits inside
`KEEP_RECENT_BYTES` (~1 MiB, roughly 3,000). The retention budget is strictly under the cap, so a
fold cannot leave the file still over it and the next fold is a whole window away.

**Size, not age.** An age cap is regressive — a light user loses a whole history to the calendar
while a heavy user loses nothing — and disk footprint is the cost a cap exists to bound.

**The order of those two writes is the whole guarantee.** Interrupted between them, the rows are
counted in both halves and the figure reads **too large**. The opposite order drops rows before
anything records them — a smaller figure, no evidence, nothing left to recompute from.
Over-counting is a bug; under-counting is the loss of the only claim this product makes about
itself, so the writes are ordered rather than merely both performed.

**What the ordering does not buy.** A folded row is byte-identical to an unfolded one, and nothing
is persisted to distinguish them — no watermark, no fold count, no timestamp. So an inflated figure
is *not* attributable to particular rows and does not correct itself on a later read; the guarantee
is only that no row is destroyed before something records it.

**That residue is an accepted limit, taken deliberately once the fold became automatic.** A
persisted watermark was considered and rejected. Every form of one has to answer *"are the rows in
front of me already counted"*, and each way of answering fails toward **under**-counting: `ts` is
second-resolution and shared by rows on both sides of a cut, `task_id` is optional and absent from
most rows, and a digest of the folded prefix races the appends it would be compared against. That
trades a vanishing event — a power loss inside the microseconds between two fsynced writes — for a
permanent hazard on every read, in the one direction the lifetime figure cannot survive.

**What keeps it to a single batch is a rollback, not the odds.** A fold whose log rewrite *fails*
puts the totals back, so the operation is a no-op rather than a half-applied one. That is what an
automatic trigger requires: the log stays over its cap after a failure, so without the rollback a
failure that repeats — a full disk fails the megabyte-scale log rewrite while the few-hundred-byte
totals write still succeeds — would re-fold the same rows on every recorded task and inflate the
figure without limit.

**Two states still leave rows counted twice, and they are not the same size.** A **crash** runs no
code, so nothing rolls back — but the next run's fold completes and truncates, which caps the
damage at one batch. A **rollback that itself fails** leaves the totals inflated and the log over
its cap, and that is the unbounded case again: every following task re-folds. It is far less likely
than the write it follows, because putting a few hundred bytes back asks much less of a failing
disk than rewriting a megabyte of log — but it is not impossible, and a signal that surfaces
stalled pruning has to cover it as well as the refusal case.

The fold runs the **same summation** the read path runs, over the same rows in the same order, so a
figure cannot change merely because rows moved across the seam. Rows that stay are **copied through
unparsed**, so neither a field added by a newer version nor a line left torn by an interrupted
append is lost to the rewrite that keeps it.

**A damaged totals file is not folded onto.** Reading a corrupt file as zeros is right for a rollup,
which only renders; it is wrong for a writer, which destroys. Folding onto zeros would replace the
damaged bytes and *then* delete the rows that could have reconciled them, turning a recoverable
state into a permanent under-count with no crash involved — so compaction refuses instead, leaving
both halves on disk. The log grows meanwhile, which is the smaller problem.

**Concurrency is bounded, not solved.** A process-level lock covers the whole read-fold-truncate,
so no thread of the compacting process can append into the gap. It cannot cover a *second*
TangleBrain process, in two ways: a row appended during those milliseconds is written to the file
being replaced and is lost, and two compactions that overlap read the same stored totals, so the
later write discards the earlier fold entirely. Accepted for a single-operator local tool, and
written down rather than implied — an advisory file lock would hold on POSIX only, and a guarantee
that silently does not hold on one supported platform is worse than a stated limitation.

**The size cap raised the frequency of that race, not its width.** Compaction went from a
deliberate call to a check on every recorded task in every process, and the in-process guard
serializes threads only. Two TangleBrain processes crossing the cap together is therefore reachable
where it used to take an operator running two maintenance calls at once. The window is still the
milliseconds of one fold and the tool is still single-operator, so the trade stands — but it stands
on the higher frequency, not the one it was first weighed against.

## Persistence boundaries

The question the architecture documents do not answer outright: **what survives a crash, and what
does not.**

| Data | Location | Durability | If lost |
|---|---|---|---|
| Roster | `$TANGLEBRAIN_ROSTER` → `~/.config/tanglebrain/roster.yaml` → packaged example | **Durable, operator-owned** | Falls back to the packaged example — one active local entry. Degrades to the safe default rather than failing. |
| Settings | `config/settings.yaml` | Durable | Both gates read as off. Fails safe. |
| Pricing reference | `config/pricing.yaml` | Durable, packaged | Cost figures unavailable; routing unaffected. |
| Rotation cursor | `<state root>/router-state.json` | **Durable, data-tier** | Rotation restarts from the beginning. Harmless — it is a fairness hint, not correctness. |
| Usage log (row window) | `<state root>/usage.jsonl` | **Durable, data-tier** | Every row not yet folded into `totals.json`, permanently. Because compaction runs on the size cap, that is recent per-task detail — the lifetime figure itself has already moved into `totals.json`. On an install too young to have crossed the cap, it is still the whole figure. See below. |
| Lifetime totals | `<state root>/totals.json` | **Durable, data-tier** | The lifetime figure falls back to whatever the surviving rows sum to — a smaller number, never an error. |
| Config backups | `<state root>/backups/` | **Durable, data-tier** | The only copy of a hand-edited roster or pricing file the GUI replaced. |
| In-flight request | memory | None | No retry, no queue, no journal. A crash mid-route loses the request and the caller sees a failure. Deliberate — this is a router, not a job system. |

**The state root** resolves `TANGLEBRAIN_STATE_DIR` → `$XDG_DATA_HOME/tanglebrain` →
`~/.local/share/tanglebrain`. It is the XDG **data** tier, not the cache tier: nothing under it is
a cache. The usage log is the only record of accumulated spend-avoided and is never
reconstructible — prompt and response text is never persisted, by design (see the Invariants above
for where that is structural and where it is a judgement) — and a config backup is
the only copy of something the operator hand-edited. `~/.cache` is *defined* as a directory any
cleanup tool may clear at will, so both were one `brew cleanup` from gone.

An install that predates the move is migrated on first run: every entry in `~/.cache/tanglebrain/`
is copied forward, **the originals are left in place** so a downgrade still finds its history, and
one notice on stderr names both directories. The copy is per-entry and skips what is already
there, so an interrupted migration completes on the next run.

**Bounded.** The log is a window, not a ledger: the size cap folds its oldest rows into
`totals.json` and drops them, so the file stops growing while the figure it feeds does not move.
Both halves are bounded — `totals.json` is one object with no per-task keys, which is why the
delegates' per-parent tree is never folded into it.

## Concurrency

- Appends to `usage.jsonl` are serialized by a **process-level** lock (`measurement._LOG_LOCK`),
  which covers `delegate_many`'s thread fan-out — the actual concurrency this product creates.
- It does **not** cover two TangleBrain *processes* writing at once (a CLI run and a serve request).
  In practice each write is a single short `write()` of one line under a few KB, which POSIX appends
  atomically in the common case; the format is line-oriented and the reader skips unparseable lines,
  so the worst realistic outcome is one corrupt record rather than a corrupt file. Stated because it
  is a real gap in the guarantee, not because it is currently hurting anything.
- The same lock covers a **compaction** end to end, so no thread of the compacting process appends
  into the gap between reading the rows and rewriting the file. Across processes the gap is real: a
  row appended by another TangleBrain during a compaction goes to the file being replaced and is
  lost. Accepted for a single-operator tool — see "Compaction" above for why an advisory lock was
  not the answer.

## Validation

- Roster and settings parse strictly. A malformed entry is an error at load, not a surprise at call
  time.
- The GUI's roster write-back validates each field, writes atomically, keeps a timestamped backup,
  and preserves comments — the operator's file is hand-authored and hand-commented, so a write that
  ate comments would be a data-loss bug.
