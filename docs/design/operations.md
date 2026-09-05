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

**A fresh install is inert and free.** The packaged roster has exactly one active entry — the free
local tier — with the subscription-CLI and paid-API tiers present as commented opt-in examples.
Nothing contacts a paid or remote service until the operator configures it to.

## Configuration

| What | Where | Notes |
|---|---|---|
| Roster | `$TANGLEBRAIN_ROSTER` → `$XDG_CONFIG_HOME/tanglebrain/roster.yaml` → packaged example | First hit wins. A bad `$TANGLEBRAIN_ROSTER` errors clearly rather than silently falling back. |
| Settings | `config/settings.yaml` | Both gates default off. |
| Pricing reference | `config/pricing.yaml` | Feeds the cloud-equivalent figure. |
| State directory | `~/.cache/tanglebrain/`, override `TANGLEBRAIN_STATE_DIR` | Rotation cursor + usage log. |

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
The `TANGLEBRAIN_TASK_ID` environment hop was not forwarded by the orchestrator. Records show
`unlinked`. This degrades silently by design and is not recoverable after the fact — see
[`architecture.md`](architecture.md).

**"The delegate server offers a target that does not exist."**
The tool description enumerating the target menu is built **once at server startup**. A roster edit
is invisible to a running server. Restart it.

**"Stats look wrong / spend-avoided dropped."**
Most likely the usage log was deleted. It lives under `~/.cache/`, which any cleanup tool may clear.
It is **not reconstructible** ([#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101)).

## Backup and recovery

| Asset | Backup | Recovery |
|---|---|---|
| Roster | **Operator's responsibility — nothing does this automatically.** The GUI writes a timestamped backup on *its* edits only; a hand-edit is unprotected. | Rewrite by hand, or fall back to the packaged example. |
| Settings | Same | Recreate; defaults are safe (both gates off). |
| Usage log | **None** | **None.** Historical spend-avoided is permanently lost. |
| Rotation cursor | None needed | Regenerates; rotation restarts. |

**Stated plainly:** the two assets that matter — the operator's hand-authored roster and the
accumulated usage history — have no automatic backup, and one of them is irreplaceable and stored in
a directory conventionally treated as disposable.

## Maintenance

- **The usage log grows without bound.** No rotation, no cap, no pruning. Currently manual: truncate
  or archive it. Tracked with the cache-tier placement question in
  [#101](https://github.com/Jason-Vaughan/TangleBrain/issues/101) — same owner, probably the same
  answer.
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
