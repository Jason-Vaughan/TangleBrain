# Operations

How TangleBrain is installed, run, configured, and recovered.

## Deployment model

There is no deployment. It is a Python package on the operator's own machine:

```sh
pip install tanglebrain                # base
pip install "tanglebrain[delegate]"    # + the MCP delegate server
```

`requires-python = ">=3.10"`. Four console scripts: `tanglebrain`, `tanglebrain-gui`,
`tanglebrain-serve`, `tanglebrain-delegate`. The delegate server additionally ships as a Claude Code
plugin — the repo doubles as its own plugin marketplace (`.claude-plugin/marketplace.json` →
`plugins/tanglebrain-delegate/`), which registers the console script declaratively without vendoring
it.

That manifest also publishes an **`installReference`**: the correct marketplace entry as data, so a
monitor or a fresh machine can read the install contract instead of parsing prose. It exists
because `/plugin marketplace add` writes an entry with no `ref` and no `autoUpdate` — a shape that
reads as governed, resolves to nothing wherever the plugin is not already cached, and let a local
checkout drift four releases behind in silence. `tests/test_plugin_manifest.py` derives every field
of it from the thing it describes, since a confidently wrong install reference is worse than none:
a reader who trusts it stops looking for a better source.

Those tests prove the reference is self-consistent, not that the loader accepts it — a distinction
this project has been bitten by before, when a green suite said nothing about what a real install
resolved to. Checked against the consumer with `claude plugin validate .claude-plugin/marketplace.json`:
validation passes, warning `Unknown field 'installReference'. Claude Code ignores it at load time.`
Ignored is the intended outcome — the key is for monitors and fresh machines, not for the loader —
and re-running that command is how to confirm it stays true.

**A fresh install is inert and free.** The packaged roster has exactly one active entry — the free
local tier — with the subscription-CLI and paid-API tiers present as commented opt-in examples.
Nothing contacts a paid or remote service until the operator configures it to.

## Configuration

| What | Where | Notes |
|---|---|---|
| Roster | `$TANGLEBRAIN_ROSTER` → `$XDG_CONFIG_HOME/tanglebrain/roster.yaml` → packaged example | First hit wins. A bad `$TANGLEBRAIN_ROSTER` errors clearly rather than silently falling back. |
| Settings | `config/settings.yaml` | Both gates default off. |
| Pricing reference | `config/pricing.yaml` | Feeds the cloud-equivalent figure. |
| State root | `TANGLEBRAIN_STATE_DIR` → `$XDG_DATA_HOME/tanglebrain` → `~/.local/share/tanglebrain` | Rotation cursor, usage log, config backups. Data tier, not cache — see `data-model.md`. A legacy cache-tier root at `~/.cache/tanglebrain/` is copied forward on first run, originals left in place. |

**The operator's real roster lives outside the repo on purpose** — `git pull` and `pip install -U`
cannot clobber it. This is a supported guarantee, not a convention.

**Credentials** are referenced, never embedded: `key_ref` is `env:NAME` or `file:PATH`, resolved
lazily at call time. The intended posture for a key file is mode `0600`. Resolution stats the file
and **warns** on stderr when it is group- or world-readable — it does not refuse, because failing
would break a working setup over a condition the operator may have accepted. The warning fires once
per file per process. POSIX only; Windows mode-bit semantics differ and the check is a no-op there.

## Enabling a paid backend

The one genuinely consequential operation, so it is spelled out:

1. Set `settings.api_billing_enabled: true`.
2. Set the specific entry's `enabled: true`.
3. Confirm the entry's `key_ref` resolves.

Both gates are required. Both are strictly bool-validated. Until both are true the entry parses and
is inspectable but is **never routable**. Reversing either step disables billing immediately — no
cached state keeps it live.

`api` is never auto-selected by capability, so even fully enabled it is reached only as a genuine
last resort (all orchestrators failed) or when named explicitly.

## Running

| Task | Command |
|---|---|
| Route a prompt | `tanglebrain "…"` |
| Force free local | `tanglebrain --local "…"` |
| Pin a backend | `tanglebrain --model <id> "…"` |
| Task-fit hint | `tanglebrain --task code "…"` |
| See what routing saved | `tanglebrain --stats` |
| Knob panel | `tanglebrain-gui` |
| OpenAI-compatible endpoint | `tanglebrain-serve` |
| MCP delegate server | registered by the orchestrator; not launched by hand |

## The spend-avoided figure is per-machine

`--stats` reports what **this machine** routed. Each machine keeps its own `usage.jsonl` and
`totals.json` under its own state root, and nothing merges them. A second laptop starts at zero and
stays independent of the first — a small number there is the design working, not a history that
went missing.

**Merging is a decided non-goal, not an omission.** A combined figure needs three things this tool
does not have and should not grow: a stable machine identity, de-duplication of task ids across
hosts, and a conflict rule for compaction running independently on each machine. That is a
distributed-systems problem inside a router whose whole premise is local-first and single-operator,
and the sync layer would end up larger than the thing it measures. The scope is bounded on purpose
— see **one operator, one machine** in
[`nonfunctional-requirements.md`](nonfunctional-requirements.md).

**A combined view is still available to anyone who wants one, and the format is that way so it
is.** The log is one JSON object per line, so `cat a/usage.jsonl b/usage.jsonl` produces a file the
rollup reads. How complete that is depends on whether either machine has compacted: a log too young
to have crossed the size cap *is* that machine's lifetime, so concatenating two of them loses
nothing; once a machine has folded rows away, the folded part lives in its `totals.json` — one
object, which does not concatenate — and is absent from the combined file.

**Read it under a throwaway state root**, not by writing it back over a machine's own log — that
would make that machine's `--stats` claim the other machine's work from then on:

```sh
mkdir -p /tmp/merged
cat machine-a/usage.jsonl machine-b/usage.jsonl > /tmp/merged/usage.jsonl
TANGLEBRAIN_STATE_DIR=/tmp/merged tanglebrain --stats
```

**It must be `TANGLEBRAIN_STATE_DIR`, not one of the other two ways to move the state root.** Two
independent mechanisms have to hold, and only this override satisfies both. Compaction is gated on
*recording*: `--stats` returns before anything routes, so nothing is recorded and the fold cannot
fire and prune the merged file. The first-run migration is gated on the two roots *collapsing*:
every entry point migrates a pre-0.21 cache-tier root forward before it reads anything, `--stats`
included, and `legacy_state_root()` honours `TANGLEBRAIN_STATE_DIR` exactly as `state_root()` does
— so source and destination resolve to the same directory and the migration is a no-op.

`XDG_DATA_HOME` satisfies only the first. The migration does not read it, so pointing it at a
scratch root leaves source and destination different and copies `~/.cache/tanglebrain` in: on an
empty scratch root the "merged" view is then silently a third machine's history, and on a populated
one you get a stray `router-state.json` and a migration notice that reads as though something moved.
The merged `usage.jsonl` itself survives — the migration skips a destination that already
exists — so the failure is quiet rather than loud, which is why it is named here rather than left
to be discovered.

## Runbook — diagnosing common failures

**"It routed to the wrong backend."**
Check which roster is actually in play first — `default_roster_path()` resolution means it may not
be the file you are editing. `--help` states this resolution order. Then check `enabled`,
`can_orchestrate`, and `good_at` on the entries,
and whether the classifier gate diverted the request.

**"Everything failed."**
`RouterError` lists every attempt with its failure, and annotates rate-limit errors. Read the list —
a uniform failure across backends usually means a local config or network problem, not a backend
problem.

**"A hard task went to the local model."**
The classifier gate misjudged it. It fails toward frontier by design, so this means it classified
confidently and wrongly rather than erroring. Use `--no-gate` to bypass for a run; disable
`classifier_gate_enabled` if it keeps happening.

**"Delegations are not linked to their parent task."**
The `TANGLEBRAIN_TASK_ID` environment hop was not forwarded by the orchestrator. Those records carry
`linkage_lost: true`, and `--stats` and the GUI panel both show the lifetime count on a
"Linkage lost" line — so the condition is diagnosable even though it still degrades rather than
raising. The linkage itself is not recoverable after the fact — see
[`architecture.md`](architecture.md).

**"The delegate server offers a target that does not exist."**
The tool description enumerating the target menu is built **once at server startup**. A roster edit
is invisible to a running server. Restart it.

**"Stats look wrong / spend-avoided dropped."**
**Check stderr from the last run first** — a task that could not be recorded says so, naming the
error and which way the figure moves. That notice fires once per process, so one line can stand
for any number of lost tasks, and a run whose log was never writable understates by however much
it dropped. Nothing is recoverable after the fact; what the notice buys is knowing the figure is
short rather than believing it.

Failing that, most likely one of the two measurement files was deleted; neither is
**reconstructible**. The figure is `totals.json` plus the rows in `usage.jsonl`, so losing either
shrinks it — losing the totals discards everything already folded, losing the log discards
everything not yet folded. Check the state root above, and check stderr for the other notice that
lands there: a migration that could not copy the log forward says so and names both paths.

**"Spend avoided jumped, or `--stats` refuses to compact."**
A compaction interrupted between its two writes leaves its rows counted in both `totals.json` and
the log, so the figure reads high until the log is next compacted past them. Nothing records which
rows were folded, so the inflation cannot be attributed or undone automatically — if the number
matters more than the history, delete `totals.json` and accept a figure of just the surviving rows.
A compaction that *refuses* to run is reporting a `totals.json` that exists but cannot be read back
as an object — unparseable, or unreadable at all. The rows are all still there and nothing was lost,
but the log stops pruning until the file is dealt with. Move the damaged file aside and the next
compaction folds from zero, or repair it by hand if you can read it.

**Only if that notice appeared and the new log is absent or empty:** the pre-move
`~/.cache/tanglebrain/usage.jsonl` is still there and can be copied across by hand. **Never copy it
over a log that already has rows** — the legacy file is frozen at migration time, so overwriting
discards everything recorded since. Append instead (`cat old >> new`), and only after checking the
two do not overlap.

## Backup and recovery

| Asset | Backup | Recovery |
|---|---|---|
| Roster | **Operator's responsibility — nothing does this automatically.** The GUI writes a timestamped backup on *its* edits only; a hand-edit is unprotected. | Rewrite by hand, or fall back to the packaged example. |
| Settings | Same | Recreate; defaults are safe (both gates off). |
| Usage log | **None** | **Partial, and only as far as the log has been compacted.** Whatever has been folded into `totals.json` survives; every row still in the window is permanently lost. Compaction runs on the size cap, so on a mature install that is recent per-task detail — but on one too young to have crossed the cap, losing this file still loses the whole lifetime figure. |
| Rotation cursor | None needed | Regenerates; rotation restarts. |

**Stated plainly:** the two assets that matter — the operator's hand-authored roster and the
accumulated usage history — have no automatic backup, and one of them is irreplaceable and stored in
a directory conventionally treated as disposable.

## Maintenance

- **The usage log prunes itself, by size.** Recording a task checks the log; crossing the cap folds
  the oldest rows into `totals.json` and drops them, so the file stays a bounded window and needs
  no operator maintenance. **Truncating the log by hand still discards every row not yet folded** —
  on a mature install that is the current window rather than the lifetime figure, but on one too
  young to have crossed the cap it is everything. A compaction that *refuses* stops the pruning
  until its damaged `totals.json` is repaired or moved aside.
- **Dependency drift is the demonstrated operational risk.** v0.20.1 was a hotfix for a live
  breakage of the *published* package: mcp 2.0.0 removed `mcp.server.fastmcp`, and an open-ended
  `mcp >= 1.0` meant `pip install "tanglebrain[delegate]"` installed a server that could not import.
  It was caught by a user-facing break, not by CI.
- **CI runs weekly on a `schedule:` trigger as well as on push and pull request.** The scheduled
  run resolves dependencies fresh — no lockfile, no cache — so an upstream release that breaks the
  published package surfaces on its own rather than waiting for someone to push. This is the half
  of the problem a version cap cannot solve: a cap prevents a known breakage, but only something
  that *runs* catches a compatible-range release that changes behavior. `workflow_dispatch` is
  enabled alongside it, so the canary can be exercised without waiting a week.

## Release

Semver, [`CHANGELOG.md`](../../CHANGELOG.md) in Keep a Changelog format, a GitHub Release per tag,
publish on release via `.github/workflows/publish.yml`.

**Verify from a clean venv against real PyPI, not from the working tree.** The v0.20.1 class of bug
— a broken optional extra — is invisible to a source checkout where the dependency is already
installed and importable. That is how it reached users in the first place.
