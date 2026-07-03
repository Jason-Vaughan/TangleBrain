# tanglebrain-delegate (Claude Code plugin)

Wires [TangleBrain](https://github.com/Jason-Vaughan/TangleBrain)'s `tanglebrain-delegate` stdio
MCP server into Claude Code, so the orchestrator can offload self-contained sub-tasks — code
generation, drafting, extraction, summarization, bulk transforms — to a **free local model** (or
any configured `can_delegate` backend) and keep its own budget for decomposition and review.

Exposes four tools: `delegate_local`, `delegate`, `delegate_many`, `delegate_targets`. See the
[project README](https://github.com/Jason-Vaughan/TangleBrain#delegate-mcp--let-an-orchestrator-offload-sub-tasks-to-a-configured-backend)
for the full tool semantics and routing rules.

## Prerequisite

The plugin registers the server declaratively — it does **not** vendor the Python code. Install
TangleBrain with the delegate extra so the `tanglebrain-delegate` command is on your `PATH`:

```sh
pip install "tanglebrain[delegate]"
```

You also need a configured roster (`~/.config/tanglebrain/roster.yaml`) with a reachable local
backend; set `TANGLEBRAIN_ROSTER=/path/to/roster.yaml` to point elsewhere.

## Install

```
/plugin marketplace add Jason-Vaughan/TangleBrain
/plugin install tanglebrain-delegate@tanglebrain
```

Equivalent manual registration (no plugin): `claude mcp add tanglebrain-delegate -- tanglebrain-delegate`.
